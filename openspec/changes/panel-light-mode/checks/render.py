"""Template render sanity check.

The app needs Postgres to run, so this is the cheap standing-in gate: load
every panel template through Jinja with a representative fake context and
prove it still compiles and renders — the `{% include "_theme.html" %}` wiring
resolves, no tag was left unbalanced by the sweep, and the theme partial and
bootstrap script land in the `<head>` of all three bases.

    python3 openspec/changes/panel-light-mode/checks/render.py
"""

import sys
from pathlib import Path

from jinja2 import Environment, FileSystemLoader

import colorscan

CONTEXT = {
    "request": None,
    "active": "dashboard",
    "username": "demo",
    "is_admin": True,
    "multi_user_mode": True,
    "csrf_token": "t0ken",
    "client_name": "Demo Client",
    "client_id": "demo-client",
    "redirect_uri": "https://example.invalid/cb",
    "code_challenge": "abc",
    "code_challenge_method": "S256",
    "state": "s",
    "client_state": "cs",
    "read_scope": "vault:read",
    "readwrite_scope": "vault:readwrite",
    "client_can_write": True,
    "requested_write": False,
    "write_unavailable": False,
    "chart_data": {"labels": ["a", "b"], "values": [1, 2]},
    "logs": [],
    "notes": [],
    "tags": [],
    "keys": [],
    "users": [],
    "grants": [],
    "nonce": "NONCE",
    "provider_ok": True,
    "last_run_ok": True,
    "note_content": "",
    "selected_note": None,
    "current_folder": "",
    "stats": {"active_keys": 3, "embedding_pct": 91, "embeddings": 12000,
              "notes_indexed": 2577, "notes_with_embeddings": 2340,
              "requests_today": 42},
    "target": {"id": 2, "is_active": True, "is_admin": False,
               "username": "demo", "vault_path": "/obsidian/demo"},
    "index_interval": 300,
    "provider": "ollama",
    "model": "bge-m3",
    "dim": 1024,
    "vault_path": "/obsidian",
    "folders": [],
    "recent": [],
    "graph": {"dangling_links": 4, "orphan_count": 2, "top_hubs": [],
              "total_links": 900},
}

# Rendered directly rather than through a base.
ROOTS = ["base.html", "auth_base.html", "authorize.html", "dashboard.html",
         "keys.html", "login.html", "oauth.html", "reembed_confirm.html",
         "register.html", "settings.html", "usage.html", "user_edit.html",
         "users.html", "vault.html", "transfer_upload.html",
         "transfer_download.html"]


def main() -> int:
    env = Environment(loader=FileSystemLoader(str(colorscan.TEMPLATE_DIR)))
    failures = []
    rendered = {}
    for name in ROOTS:
        try:
            rendered[name] = env.get_template(name).render(**CONTEXT)
        except Exception as exc:                      # noqa: BLE001
            failures.append(f"{name}: {type(exc).__name__}: {exc}")

    # Every base must actually carry the token layer and the pre-paint script.
    for name in ("base.html", "auth_base.html", "authorize.html"):
        html = rendered.get(name, "")
        head = html[:html.find("</head>")] if "</head>" in html else ""
        if "--primary-glow" not in head:
            failures.append(f"{name}: token partial missing from <head>")
        if "data-theme" not in head:
            failures.append(f"{name}: pre-paint theme bootstrap missing from <head>")
        if "__themeToggle" not in html:
            failures.append(f"{name}: theme toggle missing")

    # The transfer surface must stay separate.
    for name in ("transfer_upload.html", "transfer_download.html"):
        html = rendered.get(name, "")
        for forbidden in ("localStorage", "data-theme", "--primary-glow", "__themeToggle"):
            if forbidden in html:
                failures.append(f"{name}: must not contain {forbidden!r}")
        if 'nonce="NONCE"' not in html:
            failures.append(f"{name}: nonce not rendered into inline style/script")

    print(f"templates rendered : {len(rendered)}/{len(ROOTS)}")
    print(f"failures           : {len(failures)}")
    for f in failures:
        print("  " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
