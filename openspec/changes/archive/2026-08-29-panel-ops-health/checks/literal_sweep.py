"""Literal-sweep scan for the templates this change touches (task 3.x).

`health.html` is new and `base.html` and `dashboard.html` both gained markup
here, so the panel-theming requirement they already satisfy — "no color literal
outside a custom-property definition" — has to be re-proved rather than assumed.

    python3 openspec/changes/archive/2026-08-29-panel-ops-health/checks/literal_sweep.py

The shared archived scanner resolves its own repository root (#170). Locate
its module by walking ancestors instead of assuming an archive depth (#220),
then use its root helper. The nonzero declaration check prevents an empty
scan from passing.
"""
import sys
from pathlib import Path

SCANNER_REL = Path("2026-08-29-panel-light-mode/checks/colorscan.py")
ARCHIVED = next(
    parent / SCANNER_REL.parent
    for parent in Path(__file__).resolve().parents
    if (parent / SCANNER_REL).is_file()
)
sys.path.insert(0, str(ARCHIVED))

import colorscan  # noqa: E402

REPO = colorscan.repo_root()

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
