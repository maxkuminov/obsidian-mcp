"""Record the baseline manifest (task 1.0).

The baseline is the *pre-sweep* state of the templates.  It was recorded
before any template mutation and committed on its own; pass `--from-git REV`
to re-derive it from a commit rather than the working tree, so the numbers in
this change can be reproduced after the sweep has landed:

    python3 openspec/changes/panel-light-mode/checks/manifest.py
    python3 openspec/changes/panel-light-mode/checks/manifest.py --from-git <pre-sweep-rev>

`replay.py` reads the result and proves each entry still resolves to its
literal under the dark theme.
"""

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

import colorscan

OUT = Path(__file__).resolve().parent.parent / "baseline-manifest.json"
REPO = Path(__file__).resolve().parents[4]
REL = "src/control_panel/templates"


def checkout(rev: str, dest: Path) -> None:
    listing = subprocess.run(
        ["git", "-C", str(REPO), "ls-tree", "--name-only", f"{rev}:{REL}"],
        check=True, capture_output=True, text=True).stdout.split()
    for name in listing:
        blob = subprocess.run(
            ["git", "-C", str(REPO), "show", f"{rev}:{REL}/{name}"],
            check=True, capture_output=True, text=True).stdout
        (dest / name).write_text(blob)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-git", metavar="REV",
                    help="scan the templates as of this commit instead of the working tree")
    ap.add_argument("-o", "--out", default=str(OUT))
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        if args.from_git:
            checkout(args.from_git, Path(tmp))
            colorscan.TEMPLATE_DIR = Path(tmp)
        entries = colorscan.scan_all()

    colorscan.dump(
        entries, Path(args.out),
        "Pre-sweep baseline of every literal-bearing color declaration in the "
        "scan scope defined by specs/panel-theming/spec.md. Recorded before any "
        "template mutation; replay.py proves each entry still resolves to its "
        "literal under the dark theme.",
    )
    by_template: dict = {}
    for e in entries:
        by_template[e.template] = by_template.get(e.template, 0) + 1
    print(f"{len(entries)} baseline entries -> {args.out}")
    for name, n in sorted(by_template.items()):
        print(f"  {n:4d}  {name}")
    lits = sum(len(e.literals) for e in entries)
    kws = sum(1 for e in entries if not e.literals)
    print(f"  literals: {lits}   keyword-only entries: {kws}")


if __name__ == "__main__":
    main()
