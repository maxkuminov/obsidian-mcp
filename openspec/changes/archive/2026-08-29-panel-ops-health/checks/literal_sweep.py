"""Literal-sweep scan for the templates this change touches (task 3.x).

`health.html` is new and `base.html` and `dashboard.html` both gained markup
here, so the panel-theming requirement they already satisfy — "no color literal
outside a custom-property definition" — has to be re-proved rather than assumed.

    python3 openspec/changes/panel-ops-health/checks/literal_sweep.py

The scanner itself is `panel-light-mode`'s, reused rather than reimplemented: a
second copy of the parser is a second set of blind spots. It is imported from
the archive with **one** correction, which is why this wrapper exists at all —
`colorscan.TEMPLATE_DIR` is derived as `parents[4]` of its own path, and
archiving the change moved it one directory deeper, so it now resolves to
`openspec/src/control_panel/templates`, which does not exist. A scan of a
directory that does not exist reports zero declarations and exits 0, which is a
gate that passes by finding nothing (issue #170). So the directory is repointed
here and the declaration count is asserted non-zero: a sweep that scanned
nothing must fail, not pass.

`health.html` is not in `colorscan.PANEL_TEMPLATES` either — that list is the
set of templates that existed when the sweep was written — so it is named
explicitly below, exactly as #161's and #162's wrappers name theirs.
"""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
ARCHIVED = REPO / "openspec/changes/archive/2026-08-29-panel-light-mode/checks"
sys.path.insert(0, str(ARCHIVED))

import colorscan  # noqa: E402

# The correction. See the module docstring — and issue #170, which tracks
# fixing it at the source.
colorscan.TEMPLATE_DIR = REPO / "src" / "control_panel" / "templates"

#: The templates this change adds or edits, scanned alongside the shared token
#: partial, which is where their tokens are defined.
TOUCHED = ["_theme.html", "base.html", "dashboard.html", "health.html"]


def main() -> int:
    missing = [t for t in TOUCHED if not (colorscan.TEMPLATE_DIR / t).exists()]
    entries = colorscan.scan_all(templates=TOUCHED)
    hits = [e for e in entries if e.literals and not e.prop.startswith("--")]

    print(f"template dir                : {colorscan.TEMPLATE_DIR}")
    print(f"templates scanned           : {', '.join(TOUCHED)}")
    print(f"in-scope color declarations : {len(entries)}")
    print(f"literals outside tokens     : {len(hits)}")
    for e in hits:
        print(f"  {e.template}:{e.line}  [{e.kind}] {e.context}  "
              f"{e.prop}: {e.value}   -> {e.literals}")

    if missing:
        # `scan_all` skips a template that does not exist, so a renamed or
        # mistyped page would otherwise be reported as clean.
        print(f"FAIL: these templates were not found and so were never scanned: {missing}")
        return 1
    if not entries:
        print(
            "FAIL: the sweep found no color declarations at all, which means it "
            "scanned the wrong directory rather than that the templates are "
            "clean."
        )
        return 1
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
