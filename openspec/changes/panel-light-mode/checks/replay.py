"""Zero-visual-diff replay (tasks 1.x gate, 4.1).

Reads `baseline-manifest.json` — recorded before any template was touched —
and, for every entry, finds the declaration that now carries that color and
resolves it through the tokens in force for the *dark* theme.  The resolved
value must equal the baseline literal exactly (whitespace- and
case-insensitively).  Semantic-keyword entries must still carry their keyword.

    python3 openspec/changes/panel-light-mode/checks/replay.py

Matching is mechanical, with three deterministic allowances, each of which is
a relocation the sweep genuinely performs:

  * a token definition recorded in `base.html`/`auth_base.html`'s own `:root`
    is now in the shared partial `_theme.html`;
  * a color recorded in an SVG `fill`/`stroke` presentation attribute is now
    in the same tag's `style=""` (SVG2 does not substitute `var()` in
    presentation attributes, so it cannot stay where it was);
  * a declaration recorded inside a `prefers-color-scheme` block whose whole
    rule became a token override resolves against the base rule with that
    media block's token values layered on top — which is exactly what the
    cascade does.

No hand-written per-entry mapping: every allowance is a rule, so a color that
quietly changed shade cannot hide in it.
"""

from pathlib import Path
import sys

import colorscan
from colorscan import PANEL_TEMPLATES, normalize, resolve, Unresolved

MANIFEST = Path(__file__).resolve().parent.parent / "baseline-manifest.json"

# A color may move between these kinds without changing what renders.
ATTR_KINDS = {"svg-attr", "style-attr"}


def split_context(entry) -> tuple:
    parts = [p.strip() for p in entry.context.split(" > ")]
    media = " > ".join(p for p in parts if p.startswith("@"))
    selector = parts[-1] if parts else ""
    return media, selector


def index(entries: list) -> dict:
    out: dict = {}
    for e in entries:
        media, selector = split_context(e)
        out.setdefault((e.template, e.kind, media, selector, e.prop), []).append(e)
    return out


def find(idx: dict, templates, kinds, media, selector, prop, ordinal):
    for tpl in templates:
        for kind in kinds:
            hits = idx.get((tpl, kind, media, selector, prop))
            if hits and ordinal < len(hits):
                return hits[ordinal]
    return None


def main() -> int:
    baseline = colorscan.load(MANIFEST)
    current = colorscan.scan_all(record_all=True)
    idx = index(current)
    base_idx = index(baseline)

    failures, checked, keyword_checked = [], 0, 0

    for e in baseline:
        media, selector = split_context(e)
        is_token_def = e.prop.startswith("--") and selector in (":root", "html")
        templates = [e.template]
        if is_token_def and e.template in PANEL_TEMPLATES:
            templates.append("_theme.html")
        kinds = ATTR_KINDS if e.kind in ATTR_KINDS else {e.kind}

        # Ambiguity guard: when the group still exists at the same media +
        # selector + property, it must hold exactly as many color-carrying
        # declarations as the baseline did, or the ordinal is not a reliable
        # identity and the match would be a guess.
        for tpl in templates:
            for kind in kinds:
                post = idx.get((tpl, kind, media, selector, e.prop))
                pre = base_idx.get((e.template, e.kind, media, selector, e.prop))
                if post is not None and pre is not None and len(post) != len(pre):
                    failures.append(
                        f"AMBIGUOUS {e.key}: {len(pre)} baseline vs {len(post)} "
                        f"current declarations at that selector/property"
                    )

        got = find(idx, templates, kinds, media, selector, e.prop, e.ordinal)
        if got is None and media:
            # The rule folded into a token override; the base rule now carries
            # it, resolved with this media block's token values on top.
            got = find(idx, templates, kinds, "", selector, e.prop, e.ordinal)
        if got is None:
            failures.append(f"MISSING  {e.key}  (baseline value {e.value!r})")
            continue

        env = colorscan.dark_env(current, e.template)
        if media:
            env.update(colorscan.media_env(current, e.template, media))

        try:
            resolved = resolve(got.value, env)
        except Unresolved as exc:
            failures.append(f"UNRESOLVED {e.key}: {exc}")
            continue

        if e.literals:
            checked += 1
            # The baseline value may itself hold a var() with a literal
            # fallback (settings.html shipped `var(--bg-2,#1c1c20)` against an
            # undefined token). Resolve it against the *pre-sweep* token set so
            # both sides are compared as rendered colors.
            try:
                expected = resolve(e.value, colorscan.dark_env(baseline, e.template))
            except Unresolved:
                expected = e.value
            if normalize(resolved) != normalize(expected):
                failures.append(
                    f"CHANGED  {e.key}\n"
                    f"           baseline: {e.value}  ->  {expected}\n"
                    f"           now:      {got.value}  ->  {resolved}"
                )
        else:
            keyword_checked += 1
            if sorted(k.lower() for k in got.keywords) != sorted(k.lower() for k in e.keywords):
                failures.append(
                    f"KEYWORD  {e.key}: {e.keywords} -> {got.keywords}"
                )

    print(f"baseline entries      : {len(baseline)}")
    print(f"literal entries replayed: {checked}")
    print(f"keyword entries checked : {keyword_checked}")
    print(f"failures              : {len(failures)}")
    for f in failures:
        print("  " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
