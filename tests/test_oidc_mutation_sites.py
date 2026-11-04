"""The D15 mutation-site guard (#416 slice C, task 3.12).

With `LOCAL_LOGIN_ENABLED=false`, every panel mutation that can reduce the set
of reachable administrators (active, admin, bound under the configured
issuer) must refuse a result with zero of them. Design D15 names the sites —
the OIDC unlink, the user edit that clears `is_admin` / `is_active`, the soft
delete and the permanent delete — and says nothing else in `src/` writes
those columns or deletes a `users` row, apart from the first-login bind, the
bootstrap and the admin create, which only add to the set.

This test makes that claim checkable. It parses every module under `src/`
and finds, per enclosing function:

* an attribute assignment to `.is_admin`, `.is_active` or `.oidc_subject`;
* a keyword argument or a dict-literal key of one of those names (an
  `update(User).values(...)`, a `User(...)` constructor, a values dict);
* a `session.delete(...)`, a `delete(User)` or a literal `DELETE FROM users`.

Each site must be in `ALLOWED` (a users-row writer D15 accounts for) or in
`NOT_USERS` (the same spelling on another model, or a read-only keyword). A
new site fails until someone decides which list it belongs on — and, if it is
a users writer that can reduce the reachable set, adds the D15 check to it.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

FLAGGED = {"is_admin", "is_active", "oidc_subject"}

#: (module, enclosing function) → the kinds of write it may perform on `users`.
ALLOWED: dict[tuple[str, str], set[str]] = {
    # D15 site 2: clears is_admin / is_active, refused at zero reachable.
    ("src/control_panel/users.py", "edit_user_submit"): {"is_admin", "is_active"},
    # D15 sites 3 and 4: soft delete (is_active) and permanent delete.
    ("src/control_panel/users.py", "delete_user"): {"is_active", "delete"},
    # Admin create: a new non-admin, active row. Only adds.
    ("src/control_panel/users.py", "create_user"): {"is_admin", "is_active"},
    # Bootstrap: the first admin on an empty table (closed while OIDC is on).
    ("src/auth/routes.py", "register_submit"): {"is_admin", "is_active"},
    # First-login bind: writes the subject on a pending row. Only adds.
    ("src/services/oidc_links.py", "prepare_login"): {"oidc_subject"},
    # The admin bind: only on an unbound row. Only adds.
    ("src/services/oidc_links.py", "bind_subject"): {"oidc_subject"},
    # D15 site 1: the admin unlink, called only by `_oidc_link_action`, which
    # runs the D15 count before committing.
    ("src/services/oidc_links.py", "unlink"): {"oidc_subject"},
}

#: The same spellings on something that is not a `users` row.
NOT_USERS: dict[tuple[str, str], set[str]] = {
    # `APIKey.is_active` (revocation) and the key list's response field.
    ("src/api/routes.py", "revoke_key"): {"is_active"},
    ("src/api/routes.py", "list_keys"): {"is_active"},
    ("src/control_panel/routes.py", "revoke_key_form"): {"is_active"},
    ("src/control_panel/routes.py", "keys_page"): {"is_active"},
    # `session.delete(api_key)` / `session.delete(client)`.
    ("src/control_panel/routes.py", "delete_key_form"): {"delete"},
    ("src/control_panel/routes.py", "delete_oauth_client"): {"delete"},
    # Read-only: template-context dict keys, the actor snapshot, a query
    # argument.
    ("src/control_panel/routes.py", "_panel_context"): {"is_admin"},
    ("src/control_panel/routes.py", "actor_snapshot"): {"is_admin"},
    ("src/control_panel/routes.py", "performance_page"): {"is_admin"},
    ("src/control_panel/users.py", "list_users"): {"is_admin", "is_active"},
    ("src/control_panel/users.py", "edit_user_form"): {"is_admin", "is_active"},
}


def _enclosing_functions(tree: ast.AST) -> dict[ast.AST, str]:
    owner: dict[ast.AST, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = current
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                name = child.name
            owner[child] = name
            visit(child, name)

    visit(tree, "<module>")
    return owner


def _is_users_delete(node: ast.Call) -> bool:
    func = node.func
    if isinstance(func, ast.Attribute) and func.attr == "delete":
        # `session.delete(obj)`: any ORM delete is reported; the lists decide.
        return isinstance(func.value, ast.Name) and func.value.id in ("session", "db", "s")
    if isinstance(func, ast.Name) and func.id in ("delete", "sa_delete"):
        return bool(node.args) and isinstance(node.args[0], ast.Name) and node.args[0].id == "User"
    return False


def sites() -> dict[tuple[str, str], set[str]]:
    found: dict[tuple[str, str], set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(ROOT))
        tree = ast.parse(path.read_text(), filename=rel)
        owner = _enclosing_functions(tree)

        def add(node, kind):
            found.setdefault((rel, owner.get(node, "<module>")), set()).add(kind)

        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr in FLAGGED:
                        add(node, target.attr)
            elif isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg in FLAGGED:
                        add(node, keyword.arg)
                if _is_users_delete(node):
                    add(node, "delete")
            elif isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value in FLAGGED:
                        add(node, key.value)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "delete from users" in " ".join(node.value.lower().split()):
                    add(node, "delete")
    return found


def test_every_users_flag_writer_and_deleter_is_accounted_for():
    found = sites()
    unexpected = {
        site: kinds - ALLOWED.get(site, set()) - NOT_USERS.get(site, set())
        for site, kinds in found.items()
    }
    unexpected = {site: kinds for site, kinds in unexpected.items() if kinds}
    assert not unexpected, (
        "new writes of users.is_admin / is_active / oidc_subject or users deletes "
        f"outside the D15 allow-list: {unexpected}. If the site can reduce the set "
        "of reachable admins, add the D15 check (src/control_panel/users.py, "
        "`_leaves_no_reachable_admin`) and list it in ALLOWED; otherwise list it "
        "in NOT_USERS with a reason."
    )


def test_the_allow_list_names_sites_that_still_exist():
    found = sites()
    stale = {
        site: kinds - found.get(site, set())
        for site, kinds in {**NOT_USERS, **ALLOWED}.items()
    }
    stale = {site: kinds for site, kinds in stale.items() if kinds}
    assert not stale, f"allow-list entries with no matching site: {stale}"


def test_the_reducing_sites_run_the_d15_check():
    """The panel's reducing sites call the D15 helper, and the unlink's only
    caller is the guarded link action."""
    users_src = (SRC / "control_panel" / "users.py").read_text()
    tree = ast.parse(users_src)
    calls: dict[str, set[str]] = {}
    owner = _enclosing_functions(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", None)
            if name:
                calls.setdefault(owner.get(node, "<module>"), set()).add(name)
    for function in ("edit_user_submit", "delete_user", "_oidc_link_action"):
        assert "_leaves_no_reachable_admin" in calls.get(function, set()), function
        assert "_actor_still_privileged" in calls.get(function, set()), function
    callers = {fn for fn, names in calls.items() if "unlink" in names}
    assert callers == {"_oidc_link_action"}, callers
    for path in SRC.rglob("*.py"):
        if path.name in ("oidc_links.py", "users.py"):
            continue
        assert "oidc_links.unlink" not in path.read_text(), path
