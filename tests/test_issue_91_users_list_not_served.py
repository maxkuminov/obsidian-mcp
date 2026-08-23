"""Regression test (#91, display half): the users list must not print a note
count for an account whose tool calls are refused.

`_active_user_ids()` filters `vault_path IS NOT NULL`, so an unassigned user's
`notes_metadata` / `note_embeddings` / `note_links` rows are frozen rather than
removed. Since #66 that is deliberate: every tool for that account is refused by
the admission gate in `_tracked` before its body runs, so the rows are not a
leak, and keeping them is what lets a reassignment to the same directory resume
without re-embedding ~16.7k chunks.

The panel said none of that where the operator was looking. `users.html`
rendered `(unassigned)` in the Vault column and, three columns right on the same
row, a live-looking note count for the same account — while `user_edit.html`'s
own selector, one click away, already read "(unassigned — every MCP tool
refuses; index kept for reassignment)". Two pages described one account and only
one of them was true about what that account could serve.

That is the same over-reporting of liveness as #76's "API Keys: 4" for four
revoked keys. The cost is what the operator does next: a number that reads as
capacity invites re-running a search that was never going to answer, or reaching
for the Danger zone to "fix" an index that is not the problem — the same
misdiagnosis #78 found on the dashboard. The actual fix is to assign a vault,
and the page never said so.

**The fix is template-only, and that is the load-bearing part.** The aggregate
query is unchanged and no index row is deleted to make the display true.
Deleting them is the full re-embed #66 exists to avoid; the display was what was
wrong, not the data. The different-root half of #91 — where a reassignment to a
*different* root leaves the previous vault's index answering the metadata-only
tools — is deferred to the next migration-carrying wave and is not in scope
here.
"""
import asyncio
import os

import pydantic_settings

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from starlette.templating import Jinja2Templates

    from src.control_panel import routes
    from src.control_panel import users as users_mod
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(os.path.dirname(users_mod.__file__), "templates")

# A count nothing else on the page can produce, so "the number is absent" is a
# claim about the Notes cell and not a coincidence of small integers.
UNSERVED_COUNT = 16731
SERVED_COUNT = 24907


# --- Rendering ------------------------------------------------------------


def _render_users_page(users) -> str:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,  # users.html never touches `request` directly
        name="users.html",
        context={
            "active": "users",
            "is_admin": True,
            "multi_user_mode": True,
            "username": "max",
            "csrf_token": "csrf",
            "users": users,
            "flash": None,
            "flash_kind": "ok",
            "error": None,
        },
    )
    return response.body.decode()


def _row(**overrides) -> dict:
    row = {
        "id": 2,
        "username": "bob",
        "is_admin": False,
        "is_active": True,
        "vault_path": "/vaults/bob",
        "last_login_at": None,
        "created_at": "2026-08-20T00:00:00+00:00",
        "api_keys_active": 1,
        "api_keys_total": 3,
        "notes": SERVED_COUNT,
    }
    row.update(overrides)
    return row


def _flat(html: str) -> str:
    """Collapse whitespace — the cell's copy wraps across source lines."""
    return " ".join(html.split())


# --- The unassigned account ------------------------------------------------


def test_an_unassigned_account_gets_a_not_served_state_not_a_number():
    html = _render_users_page(
        [_row(vault_path=None, notes=UNSERVED_COUNT)]
    )
    assert str(UNSERVED_COUNT) not in html, (
        "the note count is still printed for an account every tool refuses"
    )
    assert "not served" in _flat(html).lower()


def test_the_not_served_state_names_both_reasons():
    """The state on its own is still a shrug. It has to say *why* there is no
    number — the tools are refused — and why the rows are nonetheless still
    there, or the operator's next move is the Danger zone."""
    flat = _flat(
        _render_users_page([_row(vault_path=None, notes=UNSERVED_COUNT)])
    ).lower()
    assert "every mcp tool refuses" in flat
    assert "kept for reassignment" in flat


def test_an_empty_string_vault_path_is_unassigned_too():
    """`user_edit.html` normalises a missing assignment to `""`; the list gets
    the raw column, which is `None`. Both are the same state and neither may
    print a count."""
    html = _render_users_page([_row(vault_path="", notes=UNSERVED_COUNT)])
    assert str(UNSERVED_COUNT) not in html
    assert "not served" in _flat(html).lower()


def test_the_row_still_says_unassigned_in_the_vault_column():
    """The two cells must agree. The Notes cell is the second half of a fact
    the Vault cell already states, not a replacement for it."""
    flat = _flat(_render_users_page([_row(vault_path=None)])).lower()
    assert "(unassigned)" in flat
    assert "not served" in flat


# --- The assigned account is untouched -------------------------------------


def test_an_assigned_account_still_shows_its_count():
    html = _render_users_page([_row(vault_path="/vaults/bob", notes=SERVED_COUNT)])
    assert str(SERVED_COUNT) in html
    assert "not served" not in _flat(html).lower()


def test_an_assigned_account_with_zero_notes_shows_the_zero():
    """A freshly assigned vault the indexer has not reached yet genuinely has
    no notes. That is a real count of zero, not a not-served state — the
    tools work, they just return nothing yet."""
    html = _render_users_page([_row(vault_path="/vaults/bob", notes=0)])
    assert "not served" not in _flat(html).lower()


def test_both_kinds_of_row_render_correctly_side_by_side():
    """The realistic page: one assigned account and one not. The states must
    not bleed into each other."""
    html = _render_users_page([
        _row(id=2, username="bob", vault_path="/vaults/bob", notes=SERVED_COUNT),
        _row(id=3, username="carol", vault_path=None, notes=UNSERVED_COUNT),
    ])
    assert str(SERVED_COUNT) in html
    assert str(UNSERVED_COUNT) not in html
    assert _flat(html).lower().count("not served") == 1


# --- The display change did not become a data change -----------------------


class _CountRow:
    def __init__(self, user_id, total, active):
        self.user_id = user_id
        self.total = total
        self.active = active


class _NoteCountRow:
    def __init__(self, user_id, cnt):
        self.user_id = user_id
        self.cnt = cnt


class _FixedTime:
    def isoformat(self):
        return "2026-08-20T00:00:00+00:00"


class _UserRow:
    def __init__(self, id, username, vault_path):
        self.id = id
        self.username = username
        self.is_admin = False
        self.is_active = True
        self.vault_path = vault_path
        self.last_login_at = None
        self.created_at = _FixedTime()


class _ListResult:
    def __init__(self, rows=None, scalars=None):
        self._rows = rows or []
        self._scalars = scalars or []

    def all(self):
        return self._rows

    def scalars(self):
        outer = self

        class _S:
            def all(self_inner):
                return outer._scalars

        return _S()


class _ListSession:
    """Answers `list_users`' three queries and records every statement, so the
    test can assert what the page did *not* do to the database."""

    def __init__(self, key_rows, note_rows, users):
        self._key_rows = key_rows
        self._note_rows = note_rows
        self._users = users
        self.statements = []
        self.deleted = []
        self.committed = False

    async def execute(self, stmt):
        self.statements.append(stmt)
        n = len(self.statements)
        if n == 1:
            return _ListResult(rows=self._key_rows)
        if n == 2:
            return _ListResult(rows=self._note_rows)
        return _ListResult(scalars=self._users)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def commit(self):
        self.committed = True


class _AdminUser:
    id = 1
    username = "max"
    is_admin = True
    is_active = True


def _list_users_context(users, note_rows, monkeypatch):
    captured = {}

    def _fake_response(request, name, context):
        captured["context"] = context
        return None

    monkeypatch.setattr(users_mod.templates, "TemplateResponse", _fake_response)
    monkeypatch.setattr(routes, "generate_csrf_token", lambda _r: "csrf")
    session = _ListSession([], note_rows, users)

    class _Req:
        query_params: dict = {}
        scope: dict = {}

    asyncio.run(
        users_mod.list_users(request=_Req(), session=session, user=_AdminUser())
    )
    return captured["context"], session


def _sql(session) -> str:
    return " ".join(str(s) for s in session.statements).lower()


def test_rendering_the_page_deletes_no_index_row(monkeypatch):
    """The one thing this change must not do. Preserving the rows is what
    lets a reassignment to the same directory resume without re-embedding the
    vault (#66), and `mcp-request-routing` pins that they are still present.
    Making the *display* true by deleting the *data* would be the full
    re-embed #66 exists to avoid."""
    _, session = _list_users_context(
        [_UserRow(3, "carol", None)],
        [_NoteCountRow(user_id=3, cnt=UNSERVED_COUNT)],
        monkeypatch,
    )
    sql = _sql(session)
    for table in ("notes_metadata", "note_embeddings", "note_links"):
        assert f"delete from {table}" not in sql, f"the page deleted {table} rows"
    assert "delete from" not in sql, "the users list issued a DELETE"
    assert session.deleted == [], "the users list called session.delete()"
    assert session.committed is False, "a read-only page committed a transaction"


def test_the_page_issues_no_write_of_any_kind(monkeypatch):
    """Wider than the DELETE check: no UPDATE either. Nulling `vault_path`'s
    index by any other write is the same data change wearing a different
    statement."""
    _, session = _list_users_context(
        [_UserRow(3, "carol", None)],
        [_NoteCountRow(user_id=3, cnt=UNSERVED_COUNT)],
        monkeypatch,
    )
    sql = _sql(session)
    for verb in ("delete from", "update ", "insert into", "truncate"):
        assert verb not in sql, f"the users list issued a {verb.strip()!r}"


def test_the_aggregate_query_is_unchanged_and_still_counts_every_row(monkeypatch):
    """Template-only means the count still reaches the context for an
    unassigned account — the template is what declines to print it. Moving the
    filter into the query would be a different change, and would make the
    number unavailable to any surface that later wants to state it."""
    ctx, session = _list_users_context(
        [_UserRow(3, "carol", None)],
        [_NoteCountRow(user_id=3, cnt=UNSERVED_COUNT)],
        monkeypatch,
    )
    row = ctx["users"][0]
    assert row["vault_path"] is None
    assert row["notes"] == UNSERVED_COUNT, (
        "the aggregate stopped counting an unassigned account's rows — this "
        "slice is template-only"
    )
    note_sql = str(session.statements[1]).lower()
    assert "vault_path" not in note_sql, (
        "the note-count query grew a vault_path predicate; the display is "
        "what changed, not the query"
    )


def test_an_assigned_account_reaches_the_context_with_its_count(monkeypatch):
    ctx, _ = _list_users_context(
        [_UserRow(2, "bob", "/vaults/bob")],
        [_NoteCountRow(user_id=2, cnt=SERVED_COUNT)],
        monkeypatch,
    )
    row = ctx["users"][0]
    assert row["vault_path"] == "/vaults/bob"
    assert row["notes"] == SERVED_COUNT


def test_end_to_end_from_the_handler_through_the_template(monkeypatch):
    """The two halves joined: the handler's real context rendered by the real
    template, so a mismatch between the key the query writes and the key the
    template reads cannot pass."""
    ctx, _ = _list_users_context(
        [_UserRow(3, "carol", None)],
        [_NoteCountRow(user_id=3, cnt=UNSERVED_COUNT)],
        monkeypatch,
    )
    html = _render_users_page(ctx["users"])
    assert str(UNSERVED_COUNT) not in html
    flat = _flat(html).lower()
    assert "not served" in flat
    assert "every mcp tool refuses" in flat
    assert "kept for reassignment" in flat
