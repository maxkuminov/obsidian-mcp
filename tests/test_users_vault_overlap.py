"""The assignment-time vault-root overlap refusal, and the users-list
quarantined state (#199, enforcement point 1).

`_check_vault_path_unique` compared two `vault_path` **strings** and rejected
only an exact duplicate. Three shapes walked past it:

- `/vaults/team` for A beside `/vaults/team/private` for B. Two different
  strings; B's root *is* beneath A's, every path lookup is
  `openat2(RESOLVE_BENEATH)` without `RESOLVE_NO_XDEV`, and the containment
  check agrees — so A's `edit_note`, `write_file` and `delete_note` reach B's
  files, and A's index pass files B's notes under A's `user_id`.
- A symlink, or a same-filesystem bind mount, making two different pathnames
  name one directory. The strings differ, so the check passed.
- The same pair reached through a symlinked *component*, which a raw string
  comparison cannot canonicalise.

So the panel's claim to check collisions was a false assurance, and the blast
radius is the two failures this server ranks highest: a cross-tenant
destructive write and a silently wrong search result, both delivered to an
agent that acts on them without a human seeing the query.

What replaces it is the *shared* predicate — `vault_overlap.relation_between`,
the same one the periodic detection runs — so there is one implementation of
"do these roots collide" rather than two that drift. Exact duplication is not
lost: it is the degenerate case, reported as `identical`, and it keeps the
wording operators already know.

The two properties that are easy to get wrong and are pinned here:

- **Component-wise, never a string prefix.** `/vaults/team-2` is not inside
  `/vaults/team`, and a `startswith` says it is — refusing an assignment that
  overlaps nothing, which is the false-positive direction this codebase treats
  as the expensive failure.
- **The check is gated on the edit's *resulting* state.** Deactivating or
  unassigning an overlapping account is the operator's remedy for the
  quarantine; a guard that refuses that edit because the account still overlaps
  is a guard with no exit.
"""
import asyncio
import os

import pydantic_settings
import pytest

_orig_init = pydantic_settings.BaseSettings.__init__


def _no_env_file_init(self, *args, **kwargs):
    kwargs.setdefault("_env_file", None)
    _orig_init(self, *args, **kwargs)


pydantic_settings.BaseSettings.__init__ = _no_env_file_init
try:
    from starlette.templating import Jinja2Templates

    from src.control_panel import routes
    from src.control_panel import users as users_mod
    from src.control_panel.flash import FLASH_SESSION_KEY
    from src.models.db import User
    from src.services import vault_overlap
finally:
    pydantic_settings.BaseSettings.__init__ = _orig_init


TEMPLATES_DIR = os.path.join(os.path.dirname(users_mod.__file__), "templates")


# --- Fakes for the edit handler -------------------------------------------


class _Result:
    """`rowcount` answers `revoke_user_sessions`, which `edit_user_submit`
    calls on a deactivating transition (#198). Zero: this fake models the
    vault-root peer query, not the session registry."""

    rowcount = 0

    def __init__(self, value=None, rows=None):
        self._value = value
        self._rows = rows or []

    def scalar_one_or_none(self):
        return self._value

    def one_or_none(self):
        return self._value

    def scalar(self):
        return self._value

    def all(self):
        return self._rows


class _ActorRow:
    def __init__(self, is_admin=True, is_active=True):
        self.is_admin = is_admin
        self.is_active = is_active


class _PeerRow:
    """What `select(User.id, User.username, User.vault_path)` yields."""

    def __init__(self, id, username, vault_path):
        self.id = id
        self.username = username
        self.vault_path = vault_path


class _FakeSession:
    """Answers the handler's queries and records the order it asked them in.

    The order is the point of one of the tests: the peer enumeration must come
    *after* the advisory lock and *before* the commit, or the check is
    check-then-act and two concurrent admins each read the other's previous row.
    """

    def __init__(self, target: User, peers=(), remaining_admins: int = 3):
        self._target = target
        self._peers = list(peers)
        self._remaining_admins = remaining_admins
        self.committed = False
        self.rolled_back = False
        self.events: list[str] = []
        self.lock_keys: list[object] = []

    async def execute(self, stmt, *_a, **_k):
        sql = str(stmt)
        if "pg_advisory_xact_lock" in sql:
            params = _a[0] if _a else _k.get("params", {})
            self.lock_keys.append(params.get("key"))
            self.events.append("lock")
            return _Result()
        if sql.startswith("SELECT users.is_admin, users.is_active"):
            self.events.append("actor")
            return _Result(_ActorRow())
        if sql.startswith("SELECT users.id, users.username, users.vault_path"):
            self.events.append("peers")
            return _Result(rows=list(self._peers))
        if "count(" in sql.lower():
            self.events.append("count")
            return _Result(self._remaining_admins)
        self.events.append("target")
        return _Result(self._target)

    async def rollback(self):
        self.rolled_back = True

    async def commit(self):
        self.events.append("commit")
        self.committed = True


class _Req:
    def __init__(self):
        self.session: dict = {}
        self.query_params: dict = {}


def _user(**overrides) -> User:
    u = User(
        username="bob",
        password_hash="x",
        is_admin=False,
        is_active=True,
        vault_path=None,
    )
    u.id = 2
    for k, v in overrides.items():
        setattr(u, k, v)
    return u


def _actor() -> User:
    u = User(
        username="max",
        password_hash="x",
        is_admin=True,
        is_active=True,
        vault_path=None,
    )
    u.id = 1
    return u


def _flash_of(request: _Req) -> tuple[str | None, str]:
    entry = request.session.get(FLASH_SESSION_KEY)
    if not entry:
        return None, "ok"
    return entry["message"], entry["kind"]


@pytest.fixture(autouse=True)
def _accept_any_absolute_path(monkeypatch):
    """Let the tests use real `tmp_path` directories as roots.

    `validate_vault_root_path` admits only `settings.vault_path` or a subpath of
    `/vaults/`, neither of which a test can create. The overlap check runs on
    the *normalised* value that validator returns, and what is under test is
    what the check does with two real directories — so the validator is stubbed
    to normalise and admit, and its own rules are exercised by its own tests.

    The stub is a **coroutine function**, because the real validator is one: its
    existence check is a syscall against a bind mount and runs off the event
    loop under a deadline, inside a handler holding the account guard.
    """
    async def _accept(p):
        raw = (p or "").strip()
        if not raw:
            return None, None
        return os.path.normpath(raw), None

    monkeypatch.setattr(users_mod, "_validate_vault_path", _accept)


def _submit(target, peers, *, vault_path, is_active="on", actor=None):
    session = _FakeSession(target, peers=peers)
    request = _Req()
    response = asyncio.run(
        users_mod.edit_user_submit(
            user_id=target.id,
            request=request,
            vault_path=vault_path,
            vault_path_custom="",
            is_admin=None,
            is_active=is_active,
            session=session,
            user=actor or _actor(),
        )
    )
    response.flash_request = request
    return response, session


def _refusal(response) -> str | None:
    message, kind = _flash_of(response.flash_request)
    return message if kind == "err" else None


# --- The three shapes string equality could not see -------------------------


def test_a_descendant_of_another_users_root_is_refused(tmp_path):
    """`/vaults/team/private` under `/vaults/team`: two different strings, one
    root beneath the other, and every tool of the outer tenant reaching the
    inner tenant's files."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    target = _user()
    response, session = _submit(
        target,
        [_PeerRow(3, "carol", str(outer))],
        vault_path=str(inner),
    )
    message = _refusal(response)
    assert message is not None, "the nested assignment was accepted"
    assert "carol" in message, "the refusal must name the conflicting user"
    assert "is inside" in message
    assert session.committed is False
    assert target.vault_path is None, "vault_path was written despite the refusal"


def test_an_ancestor_of_another_users_root_is_refused(tmp_path):
    """The same condition tested in the other direction — and it is the one
    that matters most, because the *outer* tenant is the one whose write tools
    can clobber the inner tenant's notes."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    target = _user()
    response, _ = _submit(
        target,
        [_PeerRow(3, "carol", str(inner))],
        vault_path=str(outer),
    )
    message = _refusal(response)
    assert message is not None
    assert "carol" in message
    assert "contains" in message


def test_a_symlink_alias_of_another_users_root_is_refused(tmp_path):
    """Two pathnames, one directory. The strings differ, so string equality
    passed; an opened descriptor for each reports the same `(st_dev, st_ino)`,
    which is check 1 — and check 1 is precisely what string equality misses.

    The relation is `identical` (one directory object), so the refusal carries
    the exact-duplicate wording rather than a containment: the pair *is* a
    duplicate, reached under a second name."""
    real = tmp_path / "shared"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real)

    target = _user()
    response, session = _submit(
        target,
        [_PeerRow(3, "carol", str(real))],
        vault_path=str(alias),
    )
    message = _refusal(response)
    assert message is not None, "the aliased root was accepted"
    assert "carol" in message
    assert "is inside" not in message and "contains" not in message
    assert session.committed is False


def test_an_alias_reached_through_a_symlinked_component_is_refused(tmp_path):
    """Check 2 over `realpath`, not over the raw string. `link/private` and
    `team/private` are two different strings that canonicalise to one path, and
    the containment test sees the nesting only because it compares the resolved
    form."""
    team = tmp_path / "team"
    (team / "private").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(team)

    target = _user()
    response, _ = _submit(
        target,
        [_PeerRow(3, "carol", str(team))],
        vault_path=str(link / "private"),
    )
    message = _refusal(response)
    assert message is not None, "a symlinked component hid the nesting"
    assert "carol" in message
    assert "is inside" in message


# --- What must still be accepted -------------------------------------------


def test_two_sibling_directories_are_accepted(tmp_path):
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    alice.mkdir()
    bob.mkdir()

    target = _user()
    response, session = _submit(
        target,
        [_PeerRow(3, "alice", str(alice))],
        vault_path=str(bob),
    )
    assert _refusal(response) is None
    assert session.committed is True
    assert target.vault_path == str(bob)


def test_a_sibling_sharing_a_string_prefix_is_accepted(tmp_path):
    """`/vaults/team-2` is not inside `/vaults/team`. A raw `startswith` says
    it is, and would quarantine two healthy tenants — the false-positive
    direction, which is the expensive one here."""
    team = tmp_path / "team"
    team_2 = tmp_path / "team-2"
    team.mkdir()
    team_2.mkdir()

    target = _user()
    response, session = _submit(
        target,
        [_PeerRow(3, "carol", str(team))],
        vault_path=str(team_2),
    )
    assert _refusal(response) is None
    assert session.committed is True


def test_an_inactive_or_unassigned_peer_is_not_a_conflict(tmp_path):
    """The peer query filters `is_active AND vault_path IS NOT NULL`, so a
    deactivated or unassigned account never reaches the pairwise check even
    when its old path nests. Nothing serves or indexes it, so it can create no
    overlap — and the fake session returns the *filtered* set, which is what
    the assertion on the emitted SQL below pins."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    target = _user()
    response, session = _submit(target, [], vault_path=str(inner))
    assert _refusal(response) is None
    assert session.committed is True


def test_the_peer_query_filters_to_active_assigned_others():
    """The filter itself, so the test above is a statement about the query and
    not only about what the fake was handed."""
    captured = {}

    class _CapturingSession(_FakeSession):
        async def execute(self, stmt, *a, **k):
            sql = str(stmt)
            if sql.startswith("SELECT users.id, users.username, users.vault_path"):
                captured["sql"] = sql
            return await super().execute(stmt, *a, **k)

    target = _user()
    session = _CapturingSession(target, peers=[])
    request = _Req()
    asyncio.run(
        users_mod.edit_user_submit(
            user_id=target.id,
            request=request,
            vault_path="/tmp",
            vault_path_custom="",
            is_admin=None,
            is_active="on",
            session=session,
            user=_actor(),
        )
    )
    sql = captured["sql"].lower()
    assert "is_active" in sql
    assert "vault_path is not null" in sql
    assert "users.id !=" in sql, "the target's own row must be excluded"


def test_resaving_the_targets_own_unchanged_path_proceeds(tmp_path):
    """The target's row is excluded from the peer set, so a no-op save is not a
    conflict with itself."""
    root = tmp_path / "bob"
    root.mkdir()

    target = _user(vault_path=str(root))
    response, session = _submit(target, [], vault_path=str(root))
    assert _refusal(response) is None
    assert session.committed is True
    assert target.vault_path == str(root)


# --- The exact-duplicate wording is preserved ------------------------------


def test_an_identical_path_keeps_the_existing_wording(tmp_path):
    """The message operators already know. "contains" or "is inside" would be
    false for an equal pair and would send them looking for a nesting that does
    not exist."""
    root = tmp_path / "shared"
    root.mkdir()

    target = _user()
    response, _ = _submit(
        target,
        [_PeerRow(3, "carol", str(root))],
        vault_path=str(root),
    )
    message = _refusal(response)
    assert message == (
        f"Vault path '{root}' is already assigned to user 'carol'."
    )


def test_an_identical_path_is_still_refused_when_neither_root_can_be_opened(
    tmp_path,
):
    """Equality of the two normalised strings is the degenerate case of both
    checks and does not depend on a descriptor. A vanished mount must not turn
    a duplicate assignment into an accepted one."""
    gone = tmp_path / "gone"

    target = _user()
    response, _ = _submit(
        target,
        [_PeerRow(3, "carol", str(gone))],
        vault_path=str(gone),
    )
    message = _refusal(response)
    assert message is not None
    # The peer is unexaminable, so the refusal may be either wording — what is
    # forbidden is acceptance.
    assert "carol" in message or "could not be examined" in message


# --- "We could not look" is not evidence of safety --------------------------


def test_an_unopenable_peer_root_refuses_the_assignment(tmp_path):
    """Identity is precisely the check that catches what string equality
    misses, so admitting on a root nobody could open would reopen the hole.
    The refusal names *that root* and says the overlap could not be ruled out —
    it does not report an overlap that was never observed, which would send the
    operator looking for a nesting that does not exist."""
    mine = tmp_path / "bob"
    mine.mkdir()
    gone = tmp_path / "carols-mount-that-is-not-there"

    target = _user()
    response, session = _submit(
        target,
        [_PeerRow(3, "carol", str(gone))],
        vault_path=str(mine),
    )
    message = _refusal(response)
    assert message is not None, "an unexaminable peer was admitted"
    assert str(gone) in message, "the refusal must name the root it could not open"
    assert "could not be ruled out" in message
    assert "is inside" not in message and "contains" not in message
    assert session.committed is False


# --- Ordering: after the lock, before the commit ---------------------------


def test_the_check_runs_after_the_lock_and_before_the_commit(tmp_path):
    """Outside the lock this is check-then-act: two admins assigning
    `/vaults/team` and `/vaults/team/private` at the same moment each read the
    other's *previous* row, each see no conflict, and both writes land."""
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    alice.mkdir()
    bob.mkdir()

    target = _user()
    _, session = _submit(
        target,
        [_PeerRow(3, "alice", str(alice))],
        vault_path=str(bob),
    )
    assert session.events.index("lock") < session.events.index("peers")
    assert session.events.index("peers") < session.events.index("commit")


def test_the_check_introduces_no_second_advisory_lock_key(tmp_path):
    """`panel-user-administration` already pins one key for both handlers; a
    second would serialize nothing against the first."""
    alice = tmp_path / "alice"
    bob = tmp_path / "bob"
    alice.mkdir()
    bob.mkdir()

    target = _user()
    _, session = _submit(
        target,
        [_PeerRow(3, "alice", str(alice))],
        vault_path=str(bob),
    )
    assert session.lock_keys == [users_mod._ADMIN_GUARD_LOCK_KEY]


# --- The guard has an exit ---------------------------------------------------


def test_deactivating_an_overlapping_account_succeeds(tmp_path):
    """The operator's remedy for a quarantine. An inactive account is outside
    the set the check compares against and can create no overlap, so refusing
    this edit protects nothing and removes the only way out of the condition
    through the interface that reports it."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    target = _user(vault_path=str(inner))
    response, session = _submit(
        target,
        [_PeerRow(3, "carol", str(outer))],
        vault_path=str(inner),
        is_active=None,  # the Active box unchecked
    )
    assert _refusal(response) is None, "deactivation was refused by the guard"
    assert session.committed is True
    assert target.is_active is False
    assert "peers" not in session.events, "the check ran for an inactive result"


def test_clearing_the_assignment_of_an_overlapping_account_succeeds(tmp_path):
    """The other remedy. An empty `vault_path` normalises to None, so the
    resulting state holds no assignment and there is nothing to compare."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    target = _user(vault_path=str(inner))
    response, session = _submit(
        target,
        [_PeerRow(3, "carol", str(outer))],
        vault_path="",
    )
    assert _refusal(response) is None
    assert session.committed is True
    assert target.vault_path is None
    assert "peers" not in session.events


def test_reactivating_an_overlapping_account_is_refused(tmp_path):
    """The asymmetry is the whole point: leaving the state is free, re-entering
    it runs the full check. An edit whose result is active *and* assigned is an
    assignment, whatever it was before."""
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    target = _user(vault_path=str(inner), is_active=False)
    response, session = _submit(
        target,
        [_PeerRow(3, "carol", str(outer))],
        vault_path=str(inner),
        is_active="on",
    )
    message = _refusal(response)
    assert message is not None, "reactivation walked back into the overlap"
    assert "carol" in message
    assert session.committed is False
    assert target.is_active is False


# --- The users list ---------------------------------------------------------


SERVED_COUNT = 24907
QUARANTINED_COUNT = 16731


def _render_users_page(users) -> str:
    templates = Jinja2Templates(directory=TEMPLATES_DIR)
    response = templates.TemplateResponse(
        request=None,
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
        "created_at": "2026-09-05T00:00:00+00:00",
        "api_keys_active": 1,
        "api_keys_total": 3,
        "notes": SERVED_COUNT,
        "quarantine": None,
    }
    row.update(overrides)
    return row


def _flat(html: str) -> str:
    return " ".join(html.split())


def test_the_list_shows_the_quarantined_state_for_an_overlap():
    html = _render_users_page([
        _row(
            notes=QUARANTINED_COUNT,
            quarantine={
                "detail": "vault root is inside the vault root of user 'carol'",
                "detected_at": "2026-09-05T09:00:00+00:00",
            },
        )
    ])
    flat = _flat(html).lower()
    assert str(QUARANTINED_COUNT) not in html, (
        "a note count is still printed for an account every tool refuses"
    )
    assert "quarantined — not served" in flat
    assert "carol" in flat, "the operator must be able to see which account"
    assert "index retained" in flat


def test_the_list_shows_the_quarantined_state_for_an_unexaminable_root():
    """Worded apart from the overlap, because an operator investigating a
    misconfiguration and an operator investigating a missing mount do different
    things — and naming a peer here would send them looking for a second
    account that does not exist."""
    html = _render_users_page([
        _row(
            notes=QUARANTINED_COUNT,
            quarantine={
                "detail": "vault root could not be examined — ENOENT (No such file or directory)",
                "detected_at": "2026-09-05T09:00:00+00:00",
            },
        )
    ])
    flat = _flat(html).lower()
    assert str(QUARANTINED_COUNT) not in html
    assert "quarantined — not served" in flat
    assert "could not be examined" in flat


def test_an_unaffected_assigned_account_still_shows_its_count():
    html = _render_users_page([_row(notes=SERVED_COUNT)])
    assert str(SERVED_COUNT) in html
    assert "quarantined" not in _flat(html).lower()


def test_an_unassigned_account_keeps_its_own_wording():
    """The three states stay distinct: unassigned is not quarantined, and its
    remedy (assign a vault) is not the quarantine's (correct the overlap)."""
    html = _render_users_page([_row(vault_path=None, notes=QUARANTINED_COUNT)])
    flat = _flat(html).lower()
    assert "quarantined" not in flat
    assert "not served" in flat
    assert "kept for reassignment" in flat


# --- The list reads the snapshot and nothing else ---------------------------


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
        return "2026-09-05T00:00:00+00:00"


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
    def __init__(self, key_rows, note_rows, users):
        self._key_rows = key_rows
        self._note_rows = note_rows
        self._users = users
        self.statements = []

    async def execute(self, stmt):
        self.statements.append(stmt)
        n = len(self.statements)
        if n == 1:
            return _ListResult(rows=self._key_rows)
        if n == 2:
            return _ListResult(rows=self._note_rows)
        return _ListResult(scalars=self._users)


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

    class _ListReq:
        query_params: dict = {}
        scope: dict = {}

    asyncio.run(
        users_mod.list_users(request=_ListReq(), session=session, user=_AdminUser())
    )
    return captured["context"], session


def _entry(user_id, username, assignment, reason):
    import datetime

    return vault_overlap.QuarantineEntry(
        user_id=user_id,
        username=username,
        assignment=assignment,
        reason=reason,
        detected_at=datetime.datetime(2026, 9, 5, 9, 0, tzinfo=datetime.timezone.utc),
    )


def test_the_handler_reads_the_snapshot_and_re_reads_no_users_row(monkeypatch):
    """The peer's name comes from the snapshot's **recorded** facts. The
    operator's first move on reading "overlaps carol" is to edit or delete
    carol; a render-time resolution would blank the display exactly then, while
    the condition is still in force."""
    vault_overlap.publish_synthetic_snapshot([
        _entry(
            2,
            "bob",
            "/vaults/team/private",
            vault_overlap.Overlap(3, "carol", "/vaults/team", vault_overlap.RELATION_CONTAINED_BY),
        )
    ])
    context, session = _list_users_context(
        [_UserRow(2, "bob", "/vaults/team/private")],
        [_NoteCountRow(user_id=2, cnt=QUARANTINED_COUNT)],
        monkeypatch,
    )
    row = context["users"][0]
    assert row["quarantine"] is not None
    assert "carol" in row["quarantine"]["detail"]
    assert "is inside" in row["quarantine"]["detail"]
    # Three statements exactly: the two aggregates and the user listing. No
    # fourth query resolves the peer.
    assert len(session.statements) == 3


def test_an_unexaminable_reason_names_no_peer(monkeypatch):
    import errno as errno_module

    vault_overlap.publish_synthetic_snapshot([
        _entry(
            2,
            "bob",
            "/vaults/bob",
            vault_overlap.RootUnexaminable(errno_module.ENOENT),
        )
    ])
    context, _ = _list_users_context(
        [_UserRow(2, "bob", "/vaults/bob")],
        [_NoteCountRow(user_id=2, cnt=QUARANTINED_COUNT)],
        monkeypatch,
    )
    detail = context["users"][0]["quarantine"]["detail"]
    assert "could not be examined" in detail
    assert "ENOENT" in detail
    assert "user '" not in detail, "an unopenable root is not an overlap"


def test_an_account_the_snapshot_does_not_name_gets_no_quarantine(monkeypatch):
    vault_overlap.publish_synthetic_snapshot()
    context, _ = _list_users_context(
        [_UserRow(2, "bob", "/vaults/bob")],
        [_NoteCountRow(user_id=2, cnt=SERVED_COUNT)],
        monkeypatch,
    )
    row = context["users"][0]
    assert row["quarantine"] is None
    assert row["notes"] == SERVED_COUNT


def test_rendering_the_page_deletes_no_index_row(monkeypatch):
    """The display changed; the data must not. Preserving the rows is what lets
    a corrected assignment resume without re-embedding the vault."""
    vault_overlap.publish_synthetic_snapshot([
        _entry(
            2,
            "bob",
            "/vaults/team/private",
            vault_overlap.Overlap(3, "carol", "/vaults/team", vault_overlap.RELATION_CONTAINED_BY),
        )
    ])
    _, session = _list_users_context(
        [_UserRow(2, "bob", "/vaults/team/private")],
        [_NoteCountRow(user_id=2, cnt=QUARANTINED_COUNT)],
        monkeypatch,
    )
    sql = " ".join(str(s) for s in session.statements).lower()
    assert "delete" not in sql
    assert "update" not in sql


# --- Normalisation: one directory, several spellings ------------------------


@pytest.mark.parametrize("form", ["trailing-slash", "double-separator", "dot"])
@pytest.mark.parametrize("direction", ["candidate-outer", "candidate-inner"])
def test_the_assignment_check_normalises_every_spelling(form, direction, tmp_path):
    """`/vaults/team/`, `/vaults//team` and `/vaults/team/.` are one root.

    The assignment refusal has to see through all three, in **both ancestor
    directions**: an administrator types the path by hand, and a check that
    compared the raw strings would admit `/vaults/team/` beside the peer's
    `/vaults/team` — the exact-duplicate case, which is the one the panel has
    claimed to catch since before this change existed.
    """
    outer = tmp_path / "team"
    inner = outer / "private"
    inner.mkdir(parents=True)

    def _spell(path):
        if form == "trailing-slash":
            return f"{path}/"
        if form == "double-separator":
            return f"{path.parent}//{path.name}"
        return f"{path}/."

    if direction == "candidate-outer":
        candidate, peer, expected = _spell(outer), str(inner), "contains"
    else:
        candidate, peer, expected = _spell(inner), str(outer), "is inside"

    target = _user()
    response, session = _submit(
        target, [_PeerRow(3, "carol", peer)], vault_path=candidate
    )
    message = _refusal(response)
    assert message is not None, f"{candidate} beside {peer} was accepted"
    assert "carol" in message
    assert expected in message
    assert session.committed is False
    assert target.vault_path is None


@pytest.mark.parametrize("form", ["trailing-slash", "double-separator", "dot"])
def test_a_respelled_duplicate_keeps_the_exact_duplicate_wording(form, tmp_path):
    """One directory under two spellings is `identical`, not a containment —
    so it gets the message operators already know rather than one that would
    send them looking for a nesting that is not there."""
    shared = tmp_path / "team"
    shared.mkdir()

    if form == "trailing-slash":
        candidate = f"{shared}/"
    elif form == "double-separator":
        candidate = f"{shared.parent}//{shared.name}"
    else:
        candidate = f"{shared}/."

    target = _user()
    response, _ = _submit(
        target, [_PeerRow(3, "carol", str(shared))], vault_path=candidate
    )
    message = _refusal(response)
    assert message == (
        f"Vault path '{shared}' is already assigned to user 'carol'."
    ), message


# --- The `/vaults/*` dropdown does not block the loop ------------------------


def test_the_vaults_dropdown_is_probed_off_the_loop_and_bounded(monkeypatch):
    """`_list_available_vaults` is a directory listing plus one `is_dir` per
    entry, and every entry is a bind mount (#199, adversarial round 1).

    Run inline it was an unbounded blocking scan on the event loop, on a page
    render — so one hung mount took the whole panel down at exactly the moment
    an operator opened it to reassign a vault *away* from that mount. Expiry
    degrades the dropdown, never the page: an empty list still renders the
    form, and the custom-path field beside it is the way through.
    """
    import threading

    release = threading.Event()

    def _blocking_scan():
        release.wait(30)
        raise AssertionError("the deadline did not abandon the wait")

    monkeypatch.setattr(users_mod, "_list_available_vaults_blocking", _blocking_scan)
    monkeypatch.setattr(
        users_mod.settings, "vault_root_observe_timeout_seconds", 0.05
    )

    async def _drive():
        ticks = 0

        async def _tick():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.005)
                ticks += 1

        ticker = asyncio.create_task(_tick())
        try:
            listed = await asyncio.wait_for(users_mod._list_available_vaults(), 5)
        finally:
            ticker.cancel()
            release.set()
        return listed, ticks

    listed, ticks = asyncio.run(_drive())
    assert listed == [], "a hung scan offers no candidates rather than hanging"
    assert ticks > 0, "the event loop was blocked for the whole scan"
