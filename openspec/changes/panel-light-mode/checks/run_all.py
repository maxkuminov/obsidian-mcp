"""Run every gate of change `panel-light-mode`.

    python3 openspec/changes/panel-light-mode/checks/run_all.py

  replay          zero-visual-diff: every baseline manifest entry still
                  resolves, under the dark theme, to its exact literal
  literal_sweep   no color literal outside a token definition
  token_coverage  full light coverage, no palette drift, color-scheme synced,
                  one physical palette
  contrast        the light palette's full contrast matrix
  render          all templates compile and render; the transfer surface stays
                  separate

Exits non-zero if any gate fails.
"""

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
GATES = ["replay", "literal_sweep", "token_coverage", "contrast", "render"]


def main() -> int:
    failed = []
    for name in GATES:
        print(f"\n=== {name} " + "=" * (60 - len(name)))
        rc = subprocess.run([sys.executable, str(HERE / f"{name}.py")], cwd=HERE).returncode
        if rc:
            failed.append(name)
    print("\n" + "=" * 66)
    print("FAILED: " + ", ".join(failed) if failed else "all gates green")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
