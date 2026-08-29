"""The JSON API's half of the quota contract (#162). Hermetic.

The defect this module exists for: `POST /api/keys` with
`{"daily_request_limit": 100}` returned 201 and created an **unlimited** key.
Pydantic's default is to ignore unknown fields, so the request looked accepted,
the operator believed a ceiling existed, and nothing enforced one. That is the
"silently never enforces" failure the whole change is built to prevent,
arriving through the one surface nobody was watching — the panel form was
correct throughout.

Two things are pinned here, and the second matters more than the first:

1. The field round-trips: it is validated, persisted, and echoed back.
2. **`extra="forbid"`**, so the *next* control somebody adds to the form and
   forgets here is a loud 422 rather than another silent no-op. Fixing only the
   one field would leave the class of bug open.

The models are exercised directly rather than over a `TestClient`, because what
is under test is the request contract and the persistence, and standing up the
whole app would drag in the session middleware, CSRF and a database for no
extra coverage. The ownership predicate and the shared enable-reset helper are
covered where they run for real — `tests/integration/test_issue_162_quotas_pg.py`.
"""
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import asyncio  # noqa: E402

import pytest  # noqa: E402
from pydantic import ValidationError  # noqa: E402

import src.api.routes as api  # noqa: E402
from src.models.db import (  # noqa: E402
    DAILY_REQUEST_LIMIT_MAX,
    DAILY_REQUEST_LIMIT_MIN,
    APIKey,
)


class _FakeUser:
    id = 1
    is_admin = True
    is_active = True
    username = "max"


class _FakeSession:
    """Captures what `create_key` persists, without a database."""

    def __init__(self):
        self.added = []
        self.committed = 0

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed += 1

    async def refresh(self, obj):
        # The real refresh reads back the server-assigned id.
        if getattr(obj, "id", None) is None:
            obj.id = 99


def _http_request():
    """A real `starlette.requests.Request`.

    `create_key` carries slowapi's `@limiter.limit`, which reaches into the
    request for the client address and refuses anything that is not the real
    type — so a `SimpleNamespace` will not do.
    """
    from starlette.requests import Request

    return Request({
        "type": "http",
        "method": "POST",
        "path": "/api/keys",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 1234),
        "state": {},
        "app": None,
    })


def _create(**payload):
    req = api.CreateKeyRequest(name="my-key", **payload)
    session = _FakeSession()
    response = asyncio.run(
        api.create_key(
            request=_http_request(), req=req, session=session, user=_FakeUser()
        )
    )
    return response, session.added[0]


# --------------------------------------------------------------------------
# 1. the field is no longer ignored
# --------------------------------------------------------------------------


def test_a_limit_sent_to_the_json_api_is_actually_persisted():
    """The regression. Before the fix this returned 201 with an unlimited key."""
    response, key = _create(daily_request_limit=100)

    assert key.daily_request_limit == 100, "the API dropped the limit again"
    assert response.daily_request_limit == 100, (
        "the response did not echo the limit, which is how the drop stayed "
        "invisible to the caller"
    )


def test_an_omitted_limit_still_means_unlimited():
    response, key = _create()
    assert key.daily_request_limit is None
    assert response.daily_request_limit is None


def test_an_explicit_null_means_unlimited():
    response, key = _create(daily_request_limit=None)
    assert key.daily_request_limit is None
    assert response.daily_request_limit is None


# --------------------------------------------------------------------------
# 2. the class of bug is closed, not just the instance
# --------------------------------------------------------------------------


def test_an_unknown_field_is_rejected_rather_than_ignored():
    """`extra="forbid"`. The next control added to the form and forgotten here
    must be a 422, not another silent no-op."""
    with pytest.raises(ValidationError) as exc:
        api.CreateKeyRequest(name="k", some_future_control=True)
    assert "some_future_control" in str(exc.value)


def test_a_misspelled_limit_field_is_rejected():
    """The realistic shape of the same mistake: a client that types the field
    name slightly wrong used to get an unlimited key and a 201."""
    with pytest.raises(ValidationError):
        api.CreateKeyRequest(name="k", daily_request_limits=100)
    with pytest.raises(ValidationError):
        api.CreateKeyRequest(name="k", dailyRequestLimit=100)


def test_the_limit_endpoint_also_forbids_extras():
    with pytest.raises(ValidationError):
        api.SetKeyLimitRequest(daily_request_limit=5, permission="readwrite")


# --------------------------------------------------------------------------
# 3. the domain matches the column's CHECK, at the edge
# --------------------------------------------------------------------------


@pytest.mark.parametrize("bad", [0, -1, -5, DAILY_REQUEST_LIMIT_MAX + 1, 99999999])
def test_out_of_domain_limits_are_rejected_by_the_request_model(bad):
    """A 422 naming the field, not a 500 out of the database's CHECK."""
    with pytest.raises(ValidationError):
        api.CreateKeyRequest(name="k", daily_request_limit=bad)
    with pytest.raises(ValidationError):
        api.SetKeyLimitRequest(daily_request_limit=bad)


@pytest.mark.parametrize(
    "good", [DAILY_REQUEST_LIMIT_MIN, 2, 100, DAILY_REQUEST_LIMIT_MAX]
)
def test_in_domain_limits_are_accepted(good):
    assert api.CreateKeyRequest(name="k", daily_request_limit=good).daily_request_limit == good
    assert api.SetKeyLimitRequest(daily_request_limit=good).daily_request_limit == good


def test_the_api_bounds_are_the_columns_bounds():
    """Read from the same constants the CHECK constraint is built from, so the
    two layers cannot drift into disagreeing about what is legal."""
    schema = api.CreateKeyRequest.model_json_schema()["properties"][
        "daily_request_limit"
    ]
    bounds = [s for s in schema.get("anyOf", [schema]) if "minimum" in s]
    assert bounds and bounds[0]["minimum"] == DAILY_REQUEST_LIMIT_MIN
    assert bounds[0]["maximum"] == DAILY_REQUEST_LIMIT_MAX


# --------------------------------------------------------------------------
# 4. the limit endpoint shares the panel's transaction, it does not reimplement
# --------------------------------------------------------------------------


def test_the_api_endpoint_uses_the_shared_enable_reset_helper():
    """Two copies of "did this go NULL to a value" is how the two surfaces
    start disagreeing about whether an operator is charged for traffic that was
    unlimited when it happened — invisibly, because both look like they work.

    The PG module proves the helper's behaviour through *both* routes; this
    pins that the API calls it at all rather than open-coding the transition.
    """
    import inspect

    from src.services import quotas

    source = inspect.getsource(api.set_key_limit)
    assert "apply_daily_request_limit" in source
    assert "DELETE FROM quota_counters" not in source, (
        "the API open-coded the counter reset instead of sharing it"
    )
    # And the panel's route calls the same one.
    import src.control_panel.routes as panel

    assert "apply_daily_request_limit" in inspect.getsource(panel.set_key_limit_form)
    assert callable(quotas.apply_daily_request_limit)


def test_the_limit_endpoint_reuses_the_panels_ownership_predicate():
    """Not a second inline `if not user.is_admin and ...` that can drift."""
    import inspect

    source = inspect.getsource(api.set_key_limit)
    assert "_assert_key_owner" in source


def test_the_key_listing_reports_the_limit():
    """A list that omitted the field is how an operator confirms a ceiling they
    do not have."""
    assert "daily_request_limit" in api.KeyInfo.model_fields
    assert "daily_request_limit" in api.CreateKeyResponse.model_fields


# --------------------------------------------------------------------------
# 5. omission is not a way to say "unlimited" on the update endpoint
# --------------------------------------------------------------------------
#
# The second-round defect, and the mirror image of the first: with a `None`
# *default* on the update model, `PUT /api/keys/{id}/limit` with `{}` was a 200
# that silently CLEARED an existing ceiling. A request that named nothing
# removed the operator's quota and reported success.
#
# The two models differ deliberately, and the difference is the whole point:
#
#   * **create** — omission means unlimited, because that is what creating a
#     key without a limit means, and it matches the panel form's empty box.
#     Nothing is being taken away; the key did not exist a moment ago.
#   * **update** — omission is a 422, because the field is the entire content
#     of the request. Clearing a quota is destructive to an invariant an
#     operator set on purpose, so it has to be typed: `null`, explicitly.


def test_an_empty_update_body_is_rejected_rather_than_clearing_the_limit():
    """The regression. `{}` used to clear the ceiling and return 200."""
    with pytest.raises(ValidationError) as exc:
        api.SetKeyLimitRequest()
    assert exc.value.errors()[0]["type"] == "missing"
    assert "daily_request_limit" in str(exc.value)


def test_an_explicit_null_is_still_the_documented_way_to_clear():
    """Required-but-nullable: omission 422s, `null` clears. The clearing
    operation is unchanged — only the accidental one is gone."""
    assert api.SetKeyLimitRequest(daily_request_limit=None).daily_request_limit is None


def test_the_update_model_is_required_but_nullable_not_merely_optional():
    """Pinned on the schema rather than on behaviour alone, because the
    difference between the two is one character in a `Field(...)` call."""
    schema = api.SetKeyLimitRequest.model_json_schema()
    assert schema.get("required") == ["daily_request_limit"], schema
    # Still nullable: `null` must remain a legal value.
    field = schema["properties"]["daily_request_limit"]
    types = {s.get("type") for s in field.get("anyOf", [field])}
    assert "null" in types, field


def test_create_omission_meaning_unlimited_is_deliberate_not_the_same_bug():
    """Asserted so the asymmetry reads as a decision rather than an oversight.

    Creating a key with no limit is the documented default and the panel's
    empty box; there is no prior ceiling for an omission to destroy. Only the
    *update* endpoint treats omission as an error.
    """
    assert "daily_request_limit" not in (
        api.CreateKeyRequest.model_json_schema().get("required") or []
    )
    assert api.CreateKeyRequest(name="k").daily_request_limit is None
    # And the two models really do disagree, on purpose.
    assert api.SetKeyLimitRequest.model_json_schema()["required"] == [
        "daily_request_limit"
    ]
