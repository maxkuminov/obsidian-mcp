"""Literal-sweep scan (task 3.2).

Proves the requirement "Single token source for panel colors": within the
scan scope, no color literal appears outside a custom-property definition.

    python3 openspec/changes/panel-light-mode/checks/literal_sweep.py

Scope (normative, `specs/panel-theming/spec.md`): CSS declarations in
`<style>` blocks and `style=""` attributes, SVG `fill`/`stroke` presentation
attributes, Chart.js color options in template JavaScript, and
`<meta name="theme-color">`.

Exceptions, and nothing else:
  * the semantic keywords `currentColor`, `transparent`, `inherit`;
  * colors inside data-URI images (they are theme-neutral, or supplied per
    theme through a token that holds the whole `url()` — `--select-arrow`);
  * vendored assets under `static/vendor/`, which are never scanned.

The transfer templates are in scope as their own surface: their literals must
live in their own local `--t-*` token block.
"""

import sys

import colorscan


def main() -> int:
    hits = []
    for e in colorscan.scan_all():
        if not e.literals:
            continue
        if e.prop.startswith("--"):
            continue          # a token definition is where literals belong
        hits.append(e)

    total = len(colorscan.scan_all())
    print(f"in-scope color declarations : {total}")
    print(f"literals outside tokens     : {len(hits)}")
    for e in hits:
        print(f"  {e.template}:{e.line}  [{e.kind}] {e.context}  "
              f"{e.prop}: {e.value}   -> {e.literals}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
