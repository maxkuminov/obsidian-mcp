"""The operator surface for a vault-root quarantine, and E4 (#199).

Slice 6 of `vault-root-overlap-guard`. What is pinned here:

* **The panel reads the published snapshot and computes nothing.** A page
  render must not open, stat or resolve a vault root, and two independent
  computations of "do these roots overlap" is how the panel and the enforcement
  come to disagree. `_quarantine_view` takes no session and no path — it is
  handed `is_admin` and reads one module attribute — and the tests below assert
  that by making every relevant syscall raise.

* **The recorded facts, not a render-time resolution.** The operator's first
  move on reading "vault root overlaps <peer>" is to edit or delete one of the
  two accounts. A surface that resolved names against `users` would show a
  changed path — or a blank where the deleted peer was — beside a condition that
  is still in force, so the snapshot carries the usernames, the canonical
  assignments and the moment it looked, and the surfaces label them as at the
  last check.

* **The two reasons are worded apart.** An overlap names the peer and the
  relation; an unexaminable root names the cause and says explicitly that no
  peer was observed. Describing the latter as an overlap sends an administrator
  hunting for a second account that does not exist.

* **The tri-state survives to the page.** Empty renders nothing (silence is the
  healthy state, not an all-clear badge); never-published renders its own note,
  because "not checked" is a refusal and must not read as "checked, clean".

* **Administrators only.** The condition names another account and another
  account's vault path — exactly what the tool-facing refusal withholds, because
  its reader is a tenant's agent.

* **E4.** `_reindex_background` publishes a snapshot *before* it takes
  `index_pass_lock`. Reindex Now, re-embed and reset embeddings all land there,
  and it is a separate entry point from the indexer loop: a detection installed
  in the loop is not installed here.

Hermetic: no database, no container, no filesystem.
"""
import asyncio
import contextlib
import datetime
import os
import tempfile

import pytest

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

from src.control_panel import routes  # noqa: E402
from src.services import vault_overlap  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES_DIR = os.path.join(HERE, "..", "src", "control_panel", "templates")

DETECTED_AT = datetime.datetime(2026, 9, 5, 11, 30, tzinfo=datetime.timezone.utc)


# --------------------------------------------------------------------------
# Fixtures and builders
# --------------------------------------------------------------------------


def _overlap_pair():
    """The shape production would produce: `/vaults/team/private` inside
    `/vaults/team`, so both accounts are named and each names the other."""
    alice = vault_overlap.QuarantineEntry(
        user_id=1,
        username="alice",
        assignment="/vaults/team",
        reason=vault_overlap.Overlap(
            peer_user_id=2,
            peer_username="bob",
            peer_assignment="/vaults/team/private",
            relation=vault_overlap.RELATION_CONTAINS,
        ),
        detected_at=DETECTED_AT,
    )
    bob = vault_overlap.QuarantineEntry(
        user_id=2,
        username="bob",
        assignment="/vaults/team/private",
        reason=vault_overlap.Overlap(
            peer_user_id=1,
            peer_username="alice",
            peer_assignment="/vaults/team",
            relation=vault_overlap.RELATION_CONTAINED_BY,
        ),
        detected_at=DETECTED_AT,
    )
    return alice, bob


def _unexaminable(cause=None):
    return vault_overlap.QuarantineEntry(
        user_id=3,
        username="carol",
        assignment="/vaults/carol",
        reason=vault_overlap.RootUnexaminable(
            cause=2 if cause is None else cause  # ENOENT
        ),
        detected_at=DETECTED_AT,
    )


class _Request:
    def __init__(self):
        self.session = {}
        self.scope = {}
        self.query_params = {}


class _User:
    def __init__(self, is_admin=True, user_id=1, username="alice"):
        self.id = user_id
        self.username = username
        self.is_admin = is_admin
        self.is_active = True


class _NoFilesystem:
    """Make every way of touching a vault root raise.

    The claim under test is structural — `_quarantine_view` is handed a bool and
    reads one module attribute — and the cheapest way to hold it to that is to
    remove the floor: if the panel ever grows an `open`, a directory read or a
    `realpath` on a page render, this fails rather than getting slower.

    `os.stat` is deliberately left alone. It is what `linecache` calls while
    pytest is *formatting a failure*, so poisoning it turns any assertion in
    these tests into an unreadable interpreter-level traceback — the three
    below are the calls a root observation actually makes.
    """

    def __init__(self, monkeypatch):
        def _boom(*_a, **_k):
            raise AssertionError("the panel touched the filesystem on a render")

        for target in ("open", "scandir", "listdir"):
            monkeypatch.setattr(os, target, _boom)
        monkeypatch.setattr(os.path, "realpath", _boom)
        monkeypatch.setattr(vault_overlap, "observe_root_blocking", _boom)

        async def _no_detection(*_a, **_k):
            raise AssertionError("the panel ran a detection on a render")

        monkeypatch.setattr(vault_overlap, "detect_and_publish", _no_detection)


# --------------------------------------------------------------------------
# 1. The view: what the surfaces are handed
# --------------------------------------------------------------------------


def test_the_view_names_every_affected_account_its_root_and_its_reason():
    alice, bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice, bob], detected_at=DETECTED_AT)

    view = routes._quarantine_view(is_admin=True)

    assert view["checked"] is True
    assert view["detected_at_iso"] == DETECTED_AT.isoformat()
    assert [a["username"] for a in view["accounts"]] == ["alice", "bob"]
    assert [a["assignment"] for a in view["accounts"]] == [
        "/vaults/team",
        "/vaults/team/private",
    ]
    assert all(a["overlap"] is True for a in view["accounts"])
    # Each line names the subject, its root, the relation, the peer and the
    # peer's root — the one operator wording, shared with the log line and the
    # `indexer_runs` row rather than composed a second time here.
    alice_text = view["accounts"][0]["text"]
    assert "alice" in alice_text and "/vaults/team" in alice_text
    assert "bob" in alice_text and "/vaults/team/private" in alice_text
    assert "contains" in alice_text
    assert "/vaults/team/private" in view["accounts"][1]["text"]
    assert "is inside" in view["accounts"][1]["text"]


def test_an_unexaminable_root_names_no_peer_and_is_not_called_an_overlap():
    """Two reasons, two fixes. Calling this one an overlap sends the operator
    looking for a second account that does not exist."""
    vault_overlap.publish_synthetic_snapshot([_unexaminable()], detected_at=DETECTED_AT)

    account = routes._quarantine_view(is_admin=True)["accounts"][0]

    assert account["overlap"] is False
    assert account["username"] == "carol"
    assert "/vaults/carol" in account["text"]
    assert "ENOENT" in account["text"]
    assert "No peer was observed" in account["text"]
    assert "overlap" not in account["text"].replace(
        "no overlap could be ruled out", ""
    )


def test_a_timed_out_observation_is_worded_as_a_hung_mount():
    """`timeout` is its own cause, distinct from an errno: "the mount is not
    answering" and "the directory is gone" are different problems."""
    vault_overlap.publish_synthetic_snapshot(
        [_unexaminable(cause=vault_overlap.CAUSE_TIMEOUT)], detected_at=DETECTED_AT
    )
    text = routes._quarantine_view(is_admin=True)["accounts"][0]["text"]
    assert "not answering" in text
    assert "No peer was observed" in text


def test_a_non_admin_is_handed_nothing_at_all():
    """Not an empty section — absent. The condition names another account and
    another account's vault path, which is the detail the tool-facing refusal
    deliberately withholds."""
    alice, bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice, bob], detected_at=DETECTED_AT)

    assert routes._quarantine_view(is_admin=False) is None


def test_an_empty_snapshot_is_checked_and_names_nobody():
    vault_overlap.publish_synthetic_snapshot([])
    view = routes._quarantine_view(is_admin=True)
    assert view["checked"] is True
    assert view["accounts"] == []


def test_the_never_published_state_is_not_an_all_clear(
    unpublished_vault_root_snapshot,
):
    view = routes._quarantine_view(is_admin=True)
    assert view["checked"] is False
    assert view["accounts"] == []


def test_the_view_opens_nothing_and_runs_no_detection(monkeypatch):
    alice, bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice, bob], detected_at=DETECTED_AT)
    _NoFilesystem(monkeypatch)

    view = routes._quarantine_view(is_admin=True)

    assert len(view["accounts"]) == 2


def test_the_pair_is_still_named_after_the_peer_is_corrected_or_deleted():
    """The facts are the snapshot's, taken when it looked.

    There is no `users` row for either account here — no session is passed and
    none could be — which is the structural form of the requirement: the pair
    stays nameable after the operator edits the peer's assignment or deletes the
    peer outright, and the surfaces present it as observed at the last check
    rather than as the state of those accounts now.
    """
    alice, bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice, bob], detected_at=DETECTED_AT)

    # The operator's remedy happens here: bob's assignment is corrected and the
    # row is deleted. Nothing republishes, because no detection has run yet.
    view = routes._quarantine_view(is_admin=True)

    assert view["accounts"][0]["text"].count("bob") >= 1
    assert "/vaults/team/private" in view["accounts"][0]["text"]
    assert view["detected_at_iso"] == DETECTED_AT.isoformat()


# --------------------------------------------------------------------------
# 2. The dashboard strip
# --------------------------------------------------------------------------


def _env():
    from jinja2 import (
        ChainableUndefined,
        ChoiceLoader,
        DictLoader,
        Environment,
        FileSystemLoader,
    )

    return Environment(
        loader=ChoiceLoader([
            DictLoader({
                "base.html":
                    "{% block title %}{% endblock %}{% block content %}{% endblock %}"
            }),
            FileSystemLoader(TEMPLATES_DIR),
        ]),
        undefined=ChainableUndefined,
        autoescape=True,
    )


def _render_strip(**health):
    ctx = dict(
        is_admin=True, username="max", multi_user_mode=True, csrf_token="csrf",
        stats={"notes_indexed": 1, "notes_with_embeddings": 1,
               "embedding_pct": 100, "active_keys": 1, "requests_today": 0},
        recent_usage=[], reindexed_24h=0, last_indexed_iso=None,
        last_indexed_rel="never", last_run_iso=None, last_run_rel="never",
        last_run_ok=True, index_interval=300, graph={},
        graph_backfill_running=False,
        health=dict({
            "show_ops": True, "last_run": None, "backup": None,
            "error_count": 0, "errors_capped": False,
            "observing_since_iso": "2026-09-05T08:00:00+00:00",
            "observing_since_rel": "2 hours ago",
            "stale_after_days": 7,
            "quarantine": None,
        }, **health),
    )
    return _env().get_template("dashboard.html").render(**ctx)


def _strip_for(entries, is_admin=True):
    vault_overlap.publish_synthetic_snapshot(entries, detected_at=DETECTED_AT)
    return _render_strip(quarantine=routes._quarantine_view(is_admin))


def test_the_strip_names_both_accounts_both_roots_and_the_relation():
    html = _strip_for(_overlap_pair())
    assert "vault-root quarantine" in html
    assert "alice" in html and "bob" in html
    assert "/vaults/team" in html and "/vaults/team/private" in html
    assert "contains" in html and "is inside" in html
    # The staleness is stated, not implied: these are recorded facts.
    assert "as at the last check" in html
    assert DETECTED_AT.isoformat() in html


def test_the_strip_does_not_describe_an_unexaminable_root_as_an_overlap():
    html = _strip_for([_unexaminable()])
    assert "root unexaminable" in html
    assert "ENOENT" in html
    assert "No peer was observed" in html
    assert "carol" in html and "/vaults/carol" in html


def test_the_strip_is_not_a_flash_and_offers_no_dismissal():
    """It stands while the condition stands and clears itself when a later
    snapshot stops naming the account. Nothing to acknowledge, nothing to
    remember to un-hide."""
    html = _strip_for(_overlap_pair())
    assert "dismiss" not in html.lower()
    assert "clears it" in html


def test_an_empty_snapshot_renders_no_condition_and_no_all_clear_badge():
    html = _strip_for([])
    assert "vault-root quarantine" not in html
    assert "not yet checked" not in html
    # And the rest of the strip is untouched.
    assert "Last pass" in html


def test_the_never_published_state_renders_its_own_note(
    unpublished_vault_root_snapshot,
):
    html = _render_strip(quarantine=routes._quarantine_view(True))
    assert "not yet checked" in html
    assert "have not been checked in this process yet" in html
    assert "vault-root quarantine" not in html


def test_a_non_admin_strip_names_no_account_and_no_path():
    alice, bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice, bob], detected_at=DETECTED_AT)
    html = _render_strip(
        show_ops=False, quarantine=routes._quarantine_view(is_admin=False)
    )
    assert "alice" not in html and "bob" not in html
    assert "/vaults/" not in html
    assert "vault-root quarantine" not in html


def test_a_degraded_strip_still_renders_and_shows_no_condition():
    """`_health_strip_or_degraded` returns no `quarantine` key when its own
    queries fail; the template must treat the absence as nothing to show rather
    than raising in the middle of the page it exists to save."""
    html = _render_strip(unavailable=True)
    assert "Health summary unavailable" in html
    assert "vault-root quarantine" not in html


# --------------------------------------------------------------------------
# 3. The health page
# --------------------------------------------------------------------------


def _render_health(quarantine, **overrides):
    ctx = dict(
        active="health",
        runs=[],
        runs_limit=50,
        show_ops=True,
        backup=None,
        stale_after_days=7,
        errors=[],
        error_count=0,
        errors_capped=False,
        error_buffer_size=100,
        observing_since_iso="2026-09-05T08:00:00+00:00",
        observing_since_rel="2 hours ago",
        quarantine=quarantine,
    )
    ctx.update(overrides)
    return _env().get_template("health.html").render(**ctx)


def _health_for(entries, is_admin=True):
    vault_overlap.publish_synthetic_snapshot(entries, detected_at=DETECTED_AT)
    return _render_health(routes._quarantine_view(is_admin))


def test_the_health_page_carries_the_same_condition_beside_the_other_sections():
    html = _health_for(_overlap_pair())
    assert "Vault-root quarantine" in html
    assert "alice" in html and "bob" in html
    assert "/vaults/team/private" in html
    assert "contains" in html
    assert "as at the last check" in html
    # Beside, not instead of: the page still renders its three sections.
    assert "Last backup" in html
    assert "Recent errors" in html
    assert "Index passes" in html


def test_the_health_page_words_the_unexaminable_reason_apart():
    html = _health_for([_unexaminable()])
    assert "root unexaminable" in html
    assert "ENOENT" in html
    assert "No peer was observed" in html
    assert "restoring that mount" in html


def test_the_health_page_renders_nothing_for_an_empty_snapshot():
    html = _health_for([])
    assert "Vault-root quarantine" not in html
    assert "Index passes" in html


def test_the_health_page_says_the_roots_have_not_been_checked(
    unpublished_vault_root_snapshot,
):
    html = _render_health(routes._quarantine_view(True))
    assert "not yet checked" in html
    assert "not an all-clear" in html


def test_the_health_page_shows_a_non_admin_no_account_and_no_path():
    alice, bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice, bob], detected_at=DETECTED_AT)
    html = _render_health(
        routes._quarantine_view(is_admin=False), show_ops=False, backup=None, errors=[]
    )
    assert "alice" not in html and "bob" not in html
    assert "/vaults/" not in html
    assert "shown to administrators only" in html


# --------------------------------------------------------------------------
# 4. The handlers
# --------------------------------------------------------------------------


class _Result:
    def __init__(self, rows=(), scalar=0):
        self._rows = list(rows)
        self._scalar = scalar

    def scalar(self):
        return self._scalar

    def all(self):
        return self._rows

    def fetchall(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def mappings(self):
        return self


class _EmptySession:
    """A fresh install: every aggregate zero, every list empty, no timestamps.

    `max(...)` answers None rather than 0 — the handlers humanize those into
    "never", and an int there is a `tzinfo` AttributeError rather than a
    meaningful failure.
    """

    async def execute(self, stmt, *_a, **_k):
        if "max(" in str(stmt).lower():
            return _Result(scalar=None)
        return _Result()

    async def rollback(self):
        return None

    async def close(self):
        return None


def _capture_context(monkeypatch, handler, user):
    captured = {}

    def _fake_response(request, name, context):
        captured["name"] = name
        captured["context"] = context
        return "rendered"

    monkeypatch.setattr(routes.templates, "TemplateResponse", _fake_response)
    monkeypatch.setattr(routes, "generate_csrf_token", lambda _r: "csrf")
    asyncio.run(handler(request=_Request(), session=_EmptySession(), user=user))
    return captured["context"]


def test_the_health_handler_hands_the_condition_to_an_admin_only(monkeypatch):
    alice, bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice, bob], detected_at=DETECTED_AT)

    admin_ctx = _capture_context(monkeypatch, routes.health_page, _User(True))
    assert [a["username"] for a in admin_ctx["quarantine"]["accounts"]] == [
        "alice",
        "bob",
    ]

    user_ctx = _capture_context(
        monkeypatch, routes.health_page, _User(False, user_id=9, username="dave")
    )
    assert user_ctx["quarantine"] is None


def test_the_health_handler_opens_no_directory(monkeypatch):
    """The page render must not touch a vault root — not to confirm the
    condition, not to resolve a name."""
    vault_overlap.publish_synthetic_snapshot(_overlap_pair(), detected_at=DETECTED_AT)
    _NoFilesystem(monkeypatch)

    ctx = _capture_context(monkeypatch, routes.health_page, _User(True))
    assert len(ctx["quarantine"]["accounts"]) == 2


def test_the_strip_carries_the_condition_onto_the_dashboard(monkeypatch):
    vault_overlap.publish_synthetic_snapshot(_overlap_pair(), detected_at=DETECTED_AT)

    async def _graph(*_a, **_k):
        return {}

    monkeypatch.setattr(routes, "_graph_stats", _graph)
    ctx = _capture_context(monkeypatch, routes.dashboard, _User(True))

    accounts = ctx["health"]["quarantine"]["accounts"]
    assert [a["username"] for a in accounts] == ["alice", "bob"]


def test_the_dashboard_strip_hands_a_non_admin_nothing(monkeypatch):
    vault_overlap.publish_synthetic_snapshot(_overlap_pair(), detected_at=DETECTED_AT)

    async def _graph(*_a, **_k):
        return {}

    monkeypatch.setattr(routes, "_graph_stats", _graph)
    ctx = _capture_context(
        monkeypatch, routes.dashboard, _User(False, user_id=9, username="dave")
    )
    assert ctx["health"]["quarantine"] is None


# --------------------------------------------------------------------------
# 5. The vault browser
# --------------------------------------------------------------------------


def _vault_context(monkeypatch, user, warmed="/tmp/test-vault"):
    from pathlib import Path

    async def _warm(_session, _uid):
        return None if warmed is None else Path(warmed)

    monkeypatch.setattr(routes, "warm_user_vault_cache", _warm)
    return _capture_context(monkeypatch, routes.vault_page, user)


def test_the_vault_browser_refuses_a_quarantined_user(monkeypatch):
    """It is the panel's third consuming surface. Listing a tree beneath a root
    that overlaps another tenant's shows this user another tenant's notes."""
    alice, _bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice], detected_at=DETECTED_AT)

    ctx = _vault_context(monkeypatch, _User(True, user_id=1, username="alice"))

    assert ctx["vault_error"], "the empty state, not a directory listing"
    assert ctx["notes"] == [] and ctx["folders"] == []
    # The wording is the gate's own, which names no other account and no other
    # path: this page is not admin-only.
    assert "bob" not in ctx["vault_error"]
    assert "/vaults/team/private" not in ctx["vault_error"]


def test_the_vault_browser_refuses_an_unexaminable_root(monkeypatch):
    vault_overlap.publish_synthetic_snapshot([_unexaminable()], detected_at=DETECTED_AT)

    ctx = _vault_context(monkeypatch, _User(False, user_id=3, username="carol"))

    assert "could not be examined" in ctx["vault_error"]


def test_the_vault_browser_refuses_before_the_first_snapshot(
    monkeypatch, unpublished_vault_root_snapshot
):
    ctx = _vault_context(monkeypatch, _User(True, user_id=1, username="alice"))
    assert "not available yet" in ctx["vault_error"]


def test_an_unassigned_account_is_still_told_it_has_no_vault(monkeypatch):
    """The unassigned wording wins over a stale quarantine: clearing the
    assignment is the operator's remedy, and telling them the account is
    quarantined for a root it no longer holds is the wrong sentence."""
    alice, _bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice], detected_at=DETECTED_AT)

    ctx = _vault_context(
        monkeypatch, _User(True, user_id=1, username="alice"), warmed=None
    )
    assert "is not assigned" in ctx["vault_error"]


def test_an_unaffected_account_still_browses(monkeypatch, tmp_path):
    alice, _bob = _overlap_pair()
    vault_overlap.publish_synthetic_snapshot([alice], detected_at=DETECTED_AT)
    (tmp_path / "Notes").mkdir()
    (tmp_path / "top.md").write_text("# top\n")

    ctx = _vault_context(
        monkeypatch,
        _User(False, user_id=42, username="dave"),
        warmed=str(tmp_path),
    )

    assert ctx.get("vault_error") is None
    assert [f["name"] for f in ctx["folders"]] == ["Notes"]
    assert [n["name"] for n in ctx["notes"]] == ["top"]


# --------------------------------------------------------------------------
# 6. E4 — the on-demand reindex
# --------------------------------------------------------------------------


class _RecordingLock:
    def __init__(self, events):
        self._events = events

    async def __aenter__(self):
        self._events.append("lock")
        return self

    async def __aexit__(self, *_exc):
        return False


def test_the_on_demand_reindex_publishes_before_it_takes_the_pass_lock(monkeypatch):
    """E4. Reindex Now, re-embed and reset embeddings all land in
    `_reindex_background`, which mirrors `run_indexer_loop` and shares only the
    pass lock with it — a detection installed in that loop is not installed
    here. And the call is *ahead* of the lock: the check that gates the pass
    must not queue behind the pass it gates.
    """
    from src.services import indexer

    events = []

    async def _detect(where):
        events.append(("detect", where))

    async def _users():
        return []

    async def _index(*_a, **_k):
        events.append("index")
        return {}

    async def _embed(*_a, **_k):
        events.append("embed")
        return 0

    class _Stats:
        def record_index(self, _v):
            pass

        def record_embedded(self, _v):
            pass

    @contextlib.asynccontextmanager
    async def _run(_trigger, _uid):
        yield _Stats()

    monkeypatch.setattr(indexer, "detect_root_overlaps", _detect)
    monkeypatch.setattr(indexer, "index_pass_lock", _RecordingLock(events))
    monkeypatch.setattr(indexer, "_active_user_ids", _users)
    monkeypatch.setattr(indexer, "index_vault", _index)
    monkeypatch.setattr(indexer, "embed_vault", _embed)
    monkeypatch.setattr(indexer, "record_indexer_run", _run)
    monkeypatch.setattr(routes.settings, "multi_user_mode", False)

    asyncio.run(routes._reindex_background())

    assert events[0] == ("detect", "panel on-demand"), events
    assert events[1] == "lock", events
    assert events[2:] == ["index", "embed"], events


def test_the_on_demand_reindex_routes_through_the_shared_detection_helper():
    """It calls the indexer's `detect_root_overlaps`, which calls the one
    `detect_and_publish` — rather than reimplementing the failure handling that
    keeps a detection error from aborting the pass."""
    import inspect

    source = inspect.getsource(routes._reindex_background)
    assert "detect_root_overlaps" in source
    assert source.index("detect_root_overlaps(") < source.index("index_pass_lock:")


@pytest.mark.parametrize(
    "handler_name",
    ["trigger_reindex", "trigger_reembed", "reset_embeddings"],
)
def test_all_three_danger_zone_controls_reach_the_guarded_entry_point(handler_name):
    """Three controls, one entry point. The guard is on `_reindex_background`
    because that is where all three land — Reindex Now directly, re-embed and
    reset embeddings at their tails."""
    import inspect

    source = inspect.getsource(getattr(routes, handler_name))
    assert "_reindex_background" in source
