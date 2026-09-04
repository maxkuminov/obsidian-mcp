"""The frozen v0 cleaner is linear AND byte-identical — issue #180, ASVS.

`_v0_clean` reproduces `clean_for_embedding` as it stood before #150. Its
output is frozen forever: `extraction_version` comparisons decide whether a
note's vector is stale, and the documented rollback recipe compares a stamped
version's cleaner against the current one. So the implementation could not
simply be "improved" when it turned out to be quadratic in the number of
unclosed fence openers (`.*?` walks to end of input at every opener, and `^`
retries at every following line) — it had to be replaced by something proved
equal.

**This module holds the ORACLE.** The two regexes below are the original v0
implementation, moved here verbatim from `src/services/embeddings.py`. The
differential test drives both the oracle and the scanner over generated inputs
and every fence-shaped fixture already in the tree, and asserts byte equality.
`tests/test_clean_for_embedding.py` imports them from here too, so there is
exactly one copy of the retired grammar in the repository.
"""
import ast
import pathlib
import random
import re
import time

import pytest

from src.services.embeddings import _v0_clean, _v0_sub_fences

# ── The oracle: v0 exactly as it was ────────────────────────────────────────

V0_FENCE_BACKTICK_RE = re.compile(r"^```[^\n]*\n.*?\n```\s*$", re.MULTILINE | re.DOTALL)
V0_FENCE_TILDE_RE = re.compile(r"^~~~[^\n]*\n.*?\n~~~\s*$", re.MULTILINE | re.DOTALL)


def v0_clean_oracle(body: str) -> str:
    """The retired implementation. Sequential, backticks first — the order is
    part of the behaviour: the two patterns' `$`-anchored spans can overlap,
    so the first substitution changes the text the second matches against."""
    body = V0_FENCE_BACKTICK_RE.sub("", body)
    body = V0_FENCE_TILDE_RE.sub("", body)
    return body


def assert_identical(body: str) -> None:
    expected = v0_clean_oracle(body)
    actual = _v0_clean(body)
    assert actual == expected, (
        f"scanner and oracle disagree\n"
        f"  input:    {body!r}\n"
        f"  oracle:   {expected!r}\n"
        f"  scanner:  {actual!r}"
    )


# ── Generated inputs: the classes the empirical characterisation found ──────
#
# Every clause in the design's D2 is represented here, several of them as
# *surprises* relative to the v1 fence grammar: a closer's trailing run is
# Unicode `\s` (NBSP and `\x0b` DO close, where v1 admits only space and tab),
# lines are split on `\n` and nothing else (a lone `\r` is an ordinary
# character, and `str.splitlines()` would be wrong), an empty block is not
# removed, and a closer run longer than the opener does not close.

LINE_VOCABULARY = [
    "```",            # bare opener / closer
    "~~~",            # the other fence character
    "````",           # over-long run: opens, never closes
    "~~~~",
    "```py",          # info string
    "~~~py",
    "``` ",           # trailing space on a closer
    "```\t",          # trailing tab
    "```\xa0",        # NBSP — closes under v0's Unicode \s, unlike v1
    "```\x0b",        # vertical tab — likewise
    "```\x1c",        # file separator — `str.splitlines()` would break here
    "~~~\xa0",
    "```x",           # non-whitespace after the run: does not close
    " ```",           # indented: not an opener and not a closer for v0
    "   ~~~",
    "    ```",
    "",               # blank line
    "  ",             # all-whitespace line
    "\xa0",
    "text",
    "# Heading",
    "code(); // ```",
]

TERMINATORS = ["\n", "\n", "\n", "\n", "\r\n", "\r"]


def _generated_documents(count: int, seed: int) -> list[str]:
    rng = random.Random(seed)
    docs = []
    for _ in range(count):
        n_lines = rng.randint(1, 12)
        parts = []
        for _ in range(n_lines):
            parts.append(rng.choice(LINE_VOCABULARY))
            parts.append(rng.choice(TERMINATORS))
        if rng.random() < 0.4:
            parts.pop()  # no trailing terminator
        docs.append("".join(parts))
    return docs


def _exhaustive_documents() -> list[str]:
    """Every 4-line document over the seven most load-bearing line shapes,
    with and without a trailing newline. Short, but exhaustive over the
    interactions — nesting, adjacency, orphan closers, empty blocks."""
    alphabet = ["```", "~~~", "````", "```\xa0", "", "x", "  "]
    docs = []
    for a in alphabet:
        for b in alphabet:
            for c in alphabet:
                for d in alphabet:
                    body = "\n".join((a, b, c, d))
                    docs.append(body)
                    docs.append(body + "\n")
    return docs


HAND_WRITTEN = [
    "",
    "\n",
    "```",
    "```\n",
    "```\n```",                       # empty block: NOT removed
    "```\n```\n",
    "```\n\n```",                     # one blank line of content: removed
    "```\ncode\n```",
    "```\ncode\n```\n",
    "```\ncode\n```\n\n\nafter",      # blank-line run after the closer
    "```\ncode\n```   \n\nafter",
    "```\ncode\n```\xa0\nafter",
    "```\ncode\n````\nafter",         # over-long closer does not close
    "````\ncode\n```\nafter",         # opener is 4 backticks; 3 still closes
    "```\ncode",                      # unclosed
    "```\n```\n```\nafter",
    "text\n```\nafter",
    "```\r\ncode\r\n```\r\n",         # CRLF throughout
    "```\rcode\r```",                 # lone CR: one line, no fence at all
    "~~~\ncode\n~~~\n```\n# H\ncode\n```\n[[X]]\n",   # pinned in #150's tests
    "```\n~~~\ncode\n~~~\n```",                       # ditto
    "```\n~~~\ncode\n```\n~~~\n",     # interleaved, order-sensitive
    "~~~\n```\ncode\n~~~\n```\n",
    " ```\ncode\n ```\n",             # indented: v0 sees no fence
    "```\ncode\n```" + "\n" * 20,
    "a\n" * 5 + "```\n" * 5,
    "```x\ny\n```\n```x\ny\n```\n",
]


@pytest.mark.parametrize("body", HAND_WRITTEN)
def test_hand_written_cases_are_byte_identical(body):
    assert_identical(body)


def test_exhaustive_short_documents_are_byte_identical():
    docs = _exhaustive_documents()
    assert len(docs) > 4000
    for body in docs:
        assert_identical(body)


def test_generated_documents_are_byte_identical():
    for body in _generated_documents(4000, seed=180):
        assert_identical(body)


# ── Every fence-shaped fixture already in the tree ──────────────────────────


def _fence_literals_from(module_name: str) -> list[str]:
    """Every string literal containing a fence run in an existing test module.

    Harvested with `ast` rather than copied, so a fixture added to those
    modules later is covered here automatically."""
    path = pathlib.Path(__file__).parent / module_name
    tree = ast.parse(path.read_text(), filename=str(path))
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "```" in node.value or "~~~" in node.value:
                out.append(node.value)
    return out


FIXTURE_MODULES = [
    "test_clean_for_embedding.py",
    "test_issue_150_fence_grammar.py",
    "test_issue_150_write_refusals.py",
    "test_issue_128_section_mode_frontmatter.py",
    "test_issue_140_section_round_trip.py",
    "test_issue_14_extract_tags_code_blocks.py",
]


@pytest.mark.parametrize("module_name", FIXTURE_MODULES)
def test_existing_fixtures_are_byte_identical(module_name):
    literals = _fence_literals_from(module_name)
    assert literals, f"no fence-shaped literal found in {module_name}"
    for body in literals:
        assert_identical(body)
        # …and under the CRLF / lone-CR variants of the same fixture, which is
        # where a `splitlines()`-based scanner would diverge silently.
        assert_identical(body.replace("\n", "\r\n"))
        assert_identical(body.replace("\n", "\r"))


# ── Linearity ───────────────────────────────────────────────────────────────


def _time(fn, *args):
    start = time.perf_counter()
    fn(*args)
    return time.perf_counter() - start


def test_many_unclosed_openers_is_linear():
    """160 KB vs 320 KB of `` ```x\\n ``. The oracle is quadratic here (this is
    the shape that made a note of unclosed fences a cross-tenant stall); the
    scanner must be linear, and must still agree with the oracle."""
    unit = "```x\n"
    n = 160 * 1024
    small = unit * (n // len(unit))
    large = unit * (2 * n // len(unit))

    # Correctness first — a fast wrong answer is worse than a slow right one.
    # Against a 16 KB slice, not the 160 KB one: the oracle is the quadratic
    # implementation, and running it at 160 KB costs ~50 s all by itself,
    # which is the finding rather than a test budget. Byte equality at scale
    # is established by the exhaustive and generated corpora above.
    oracle_sample = unit * (16 * 1024 // len(unit))
    assert _v0_clean(oracle_sample) == v0_clean_oracle(oracle_sample)

    # Warm the interpreter, then take the best of three so a scheduler hiccup
    # on a shared runner cannot fabricate a ratio.
    _v0_clean(small)
    t_small = min(_time(_v0_clean, small) for _ in range(3))
    t_large = min(_time(_v0_clean, large) for _ in range(3))

    assert t_large < 4.0, f"{t_large:.3f}s for 320 KB of unclosed openers"
    ratio = t_large / max(t_small, 1e-6)
    assert ratio < 4, f"ratio {ratio:.2f} (n={t_small:.4f}s, 2n={t_large:.4f}s)"


def test_one_pass_alone_is_linear_for_both_fence_characters():
    for fence in ("```", "~~~"):
        unit = f"{fence}x\n"
        n = 160 * 1024
        small = unit * (n // len(unit))
        large = unit * (2 * n // len(unit))
        _v0_sub_fences(small, fence)
        t_small = min(_time(_v0_sub_fences, small, fence) for _ in range(3))
        t_large = min(_time(_v0_sub_fences, large, fence) for _ in range(3))
        assert t_large < 4.0, f"{fence}: {t_large:.3f}s"
        ratio = t_large / max(t_small, 1e-6)
        assert ratio < 4, f"{fence}: ratio {ratio:.2f}"
