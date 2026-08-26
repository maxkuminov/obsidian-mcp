"""`set_frontmatter` refuses a malformed block instead of writing past it (#128).

The bug: `parse_frontmatter` answered `({}, raw)` for a broken block exactly as
it does for a note with no block at all, so `set_frontmatter` merged into an
empty dict and serialized — prepending a SECOND `---` block above the broken
one and reporting success. `remove=` on the same note found no keys in the
empty dict and silently did nothing, also reporting success. Both are the
product's top failure class: a write outcome the agent is told is something
else.

Three rules are pinned here (D6): the diagnosis runs before the no-op check, so
a broken note is reported broken even for a call that would have changed
nothing; only an *effective* mutation reaches the serializer, compared
type-sensitively; and removing the last key removes the block entirely.
"""

import asyncio
import os
import tempfile

os.environ.setdefault("SECRET_KEY", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("VAULT_PATH", "/tmp/test-vault")
os.chdir(tempfile.gettempdir())

import pytest  # noqa: E402

import src.mcp_server.tools as tools  # noqa: E402
from src.mcp_server.auth import current_permission  # noqa: E402
from src.services import vault as vault_service  # noqa: E402
from src.services import vault_fs  # noqa: E402


@pytest.fixture
def vault(monkeypatch, tmp_path):
    async def noop(*args, **kwargs):
        return None

    monkeypatch.setattr(vault_service.settings, "vault_path", str(tmp_path))
    monkeypatch.setattr(tools, "_log_usage", noop)
    vault_fs.reset_filesystem_probe_cache()
    token = current_permission.set("readwrite")
    yield tmp_path
    current_permission.reset(token)
    vault_fs.reset_filesystem_probe_cache()


def write(vault, name, text):
    (vault / name).write_text(text, encoding="utf-8", newline="")
    return vault / name


def read(vault, name):
    return (vault / name).read_bytes().decode("utf-8")


DEFECTS = [
    ("---\nunclosed\nbody\n", "never closed"),
    ("---\n", "never closed"),
    ("---", "never closed"),
    ("---\na: [\n---\nbody\n", "does not parse as YAML"),
    ("---\n- a\n- b\n---\nbody\n", "not a mapping"),
    ("---\njust a scalar\n---\nbody\n", "not a mapping"),
    ("---\nnull\n---\nbody\n", "not a mapping"),
    ("---\n~\n---\nbody\n", "not a mapping"),
    ("---\n# only a comment\n---\nbody\n", "not a mapping"),
]


# ── the three defect classes are refused, with nothing written ──────────────


@pytest.mark.parametrize("original,defect", DEFECTS)
def test_updates_on_a_malformed_block_are_refused(vault, original, defect):
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"status": "done"})
    )
    assert "malformed frontmatter block" in result
    assert defect in result
    assert "replace_frontmatter=True" in result
    assert read(vault, "n.md") == original
    # The specific damage: no second block above the broken one.
    assert not read(vault, "n.md").startswith("---\nstatus: done\n---\n")


@pytest.mark.parametrize("original,defect", DEFECTS)
def test_remove_on_a_malformed_block_refuses_rather_than_no_ops(
    vault, original, defect
):
    """A silent no-op told the agent the key was gone. It was not."""
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={}, remove=["x"])
    )
    assert "malformed frontmatter block" in result
    assert defect in result
    assert read(vault, "n.md") == original


@pytest.mark.parametrize("original,defect", DEFECTS)
def test_the_diagnosis_precedes_the_empty_call_no_op(vault, original, defect):
    """D6, and the spec says so explicitly: diagnosis precedes the no-op check.
    `updates={}, remove=[]` used to return a cheerful "no changes" about a note
    this tool cannot safely touch."""
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={}, remove=[])
    )
    assert "malformed frontmatter block" in result
    assert defect in result
    assert "No changes" not in result
    assert read(vault, "n.md") == original


def test_a_yaml_error_carries_the_parser_message(vault):
    write(vault, "n.md", "---\na: [\n---\nbody\n")
    result = asyncio.run(tools.set_frontmatter_impl("n.md", updates={"a": 1}))
    # PyYAML's own text, so the author can find the offending line.
    assert "does not parse as YAML" in result
    assert "line" in result


# ── the empty fenced block is valid input ───────────────────────────────────


@pytest.mark.parametrize(
    "original,expected",
    [
        ("---\n---\nbody\n", "---\na: 1\n---\nbody\n"),
        ("---\n---\n", "---\na: 1\n---\n"),
        ("---\n---", "---\na: 1\n---\n"),
        ("---\n\n \n---\nbody\n", "---\na: 1\n---\nbody\n"),
    ],
)
def test_an_empty_block_is_updated_in_place(vault, original, expected):
    write(vault, "n.md", original)
    result = asyncio.run(tools.set_frontmatter_impl("n.md", updates={"a": 1}))
    assert "Updated frontmatter" in result
    assert read(vault, "n.md") == expected


# ── only an effective mutation writes ───────────────────────────────────────


def test_setting_a_key_to_the_value_it_already_holds_writes_nothing(vault):
    original = "---\nstatus: draft\n# a comment worth keeping\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"status": "draft"})
    )
    assert "No changes" in result
    # Byte-identical: the YAML comment PyYAML would have destroyed is still
    # there, which is the visible proof that nothing was serialized.
    assert read(vault, "n.md") == original


def test_removing_an_absent_key_writes_nothing(vault):
    original = "---\nstatus: draft\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={}, remove=["nope"])
    )
    assert "No changes" in result
    assert read(vault, "n.md") == original


def test_a_remove_that_removed_nothing_does_not_drop_an_empty_block(vault):
    """The guard's whole reason for existing. `serialize_frontmatter({}, body)`
    emits no fences at all, so a remove-of-nothing on an empty-block note would
    delete the block — and on this note the body's own mapping-shaped fenced
    prefix would then BECOME the frontmatter."""
    original = "---\n---\n---\npromoted: yes\n---\nbody text\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={}, remove=["absent"])
    )
    assert "No changes" in result
    assert read(vault, "n.md") == original
    # And the body prefix was NOT promoted into active frontmatter.
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert fm == {}


@pytest.mark.parametrize(
    "stored,proposed",
    [
        ("true", 1),        # bool stored, int proposed
        ("1", True),        # int stored, bool proposed
        ("false", 0),
        ("0", False),
    ],
)
def test_bool_and_int_are_not_the_same_value(vault, stored, proposed):
    """`True == 1` in Python, so a plain `==` guard would report a real type
    change as a no-op and leave the note carrying the type the caller did not
    ask for — while telling them nothing happened."""
    write(vault, "n.md", f"---\nflag: {stored}\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"flag": proposed})
    )
    assert "Updated frontmatter" in result
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert fm["flag"] is proposed or fm["flag"] == proposed
    assert type(fm["flag"]) is type(proposed)


def test_a_nested_bool_does_not_satisfy_a_nested_int(vault):
    write(vault, "n.md", "---\nopts:\n  - 1\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"opts": [True]})
    )
    assert "Updated frontmatter" in result
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert fm["opts"] == [True]
    assert type(fm["opts"][0]) is bool


def test_a_genuine_change_still_writes(vault):
    write(vault, "n.md", "---\nstatus: draft\nkeep: me\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"status": "done"})
    )
    assert "set: status" in result
    fm, body = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert fm == {"status": "done", "keep": "me"}
    assert body == "body\n"


def test_a_mixed_call_writes_when_only_one_half_is_effective(vault):
    write(vault, "n.md", "---\nstatus: draft\nwip: yes\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl(
            "n.md", updates={"status": "draft"}, remove=["wip"]
        )
    )
    assert "removed: wip" in result
    assert "set: status" not in result
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert fm == {"status": "draft"}


# ── removing the last key removes the block ─────────────────────────────────


def test_removing_the_last_key_leaves_exactly_the_prior_body(vault):
    write(vault, "n.md", "---\nonly: key\n---\n# Body\n\ntext\n")
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={}, remove=["only"])
    )
    assert "Updated frontmatter" in result
    # No opening fence, no YAML region, no closing fence, no separator.
    assert read(vault, "n.md") == "# Body\n\ntext\n"


def test_removing_the_last_key_from_a_note_with_an_empty_body(vault):
    write(vault, "n.md", "---\nonly: key\n---\n")
    asyncio.run(tools.set_frontmatter_impl("n.md", updates={}, remove=["only"]))
    assert read(vault, "n.md") == ""


# ── the absent-fence prepend is unchanged ───────────────────────────────────


@pytest.mark.parametrize(
    "original",
    [
        "plain body\n",
        "\n---\na: 1\n---\nnot on line 1\n",
        "text first\n---\na: 1\n---\n",
        "----\nfour dashes\n",
    ],
)
def test_a_note_with_no_line_1_fence_still_gets_a_block_prepended(vault, original):
    write(vault, "n.md", original)
    result = asyncio.run(tools.set_frontmatter_impl("n.md", updates={"tags": ["x"]}))
    assert "Updated frontmatter" in result
    after = read(vault, "n.md")
    assert after.startswith("---\n")
    fm, body = vault_service.parse_frontmatter(after)
    assert fm == {"tags": ["x"]}
    assert body == original


def test_the_body_is_byte_identical_across_an_ordinary_update(vault):
    body = "# Body\n\nSome text with --- a dash run.\n\n## Sub\n"
    write(vault, "n.md", "---\na: 1\n---\n" + body)
    asyncio.run(tools.set_frontmatter_impl("n.md", updates={"b": 2}))
    _, after_body = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert after_body == body


# ── a cancelling update+remove pair is a NET no-op ──────────────────────────


def test_an_update_cancelled_by_a_remove_writes_nothing(vault):
    """Per-key bookkeeping records the *steps*; the question is whether the
    destination differs. `updates={"temp": 1}, remove=["temp"]` takes two
    recorded steps back to where it started, so both lists are non-empty and
    the old guard waved it through — serializing an unchanged mapping, which
    on this note drops the valid empty block and promotes the body's own
    fenced prefix into active frontmatter."""
    original = "---\n---\n---\npromoted: yes\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"temp": 1}, remove=["temp"])
    )
    assert "No changes" in result
    assert read(vault, "n.md") == original
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert fm == {}


def test_a_cancelling_pair_on_an_ordinary_note_writes_nothing(vault):
    original = "---\nstatus: draft\n# comment kept\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"temp": 1}, remove=["temp"])
    )
    assert "No changes" in result
    assert read(vault, "n.md") == original


def test_removing_a_key_the_same_call_also_sets_still_removes_it(vault):
    """The documented precedence: updates first, then removals. A key named in
    both ends up removed — and that IS a net change."""
    write(vault, "n.md", "---\nstatus: draft\nkeep: me\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl(
            "n.md", updates={"status": "done"}, remove=["status"]
        )
    )
    assert "Updated frontmatter" in result
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert fm == {"keep": "me"}


def test_a_reordering_update_is_a_change(vault):
    """`safe_dump` runs with `sort_keys=False`, so key order is part of the
    note's bytes. A reorder that compared equal would report no change while
    the order the caller asked for was dropped."""
    write(vault, "n.md", "---\nmeta:\n  a: 1\n  b: 2\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"meta": {"b": 2, "a": 1}})
    )
    assert "Updated frontmatter" in result
    assert read(vault, "n.md").index("b: 2") < read(vault, "n.md").index("a: 1")


# ── signed zero ─────────────────────────────────────────────────────────────


def test_signed_zero_is_a_change(vault):
    """`-0.0 == 0.0` is True, while `safe_dump` writes them differently — so a
    caller correcting the sign was told nothing changed and the note kept the
    sign it had."""
    write(vault, "n.md", "---\nzero: -0.0\n---\nbody\n")
    result = asyncio.run(tools.set_frontmatter_impl("n.md", updates={"zero": 0.0}))
    assert "Updated frontmatter" in result
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    import math
    assert not math.copysign(1.0, fm["zero"]) < 0


def test_nested_signed_zero_is_a_change(vault):
    write(vault, "n.md", "---\nvals:\n  - -0.0\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"vals": [0.0]})
    )
    assert "Updated frontmatter" in result
    import math
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert not math.copysign(1.0, fm["vals"][0]) < 0


def test_an_identical_signed_zero_is_not_a_change(vault):
    original = "---\nzero: -0.0\n# comment kept\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(tools.set_frontmatter_impl("n.md", updates={"zero": -0.0}))
    assert "No changes" in result
    assert read(vault, "n.md") == original


def test_an_identical_float_is_not_a_change(vault):
    original = "---\nratio: 1.5\n# comment kept\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(tools.set_frontmatter_impl("n.md", updates={"ratio": 1.5}))
    assert "No changes" in result
    assert read(vault, "n.md") == original


def test_two_nans_are_the_same_value(vault):
    """Documented non-finite policy: `.hex()` renders every NaN as `'nan'`, so
    two of them compare equal here. YAML round-trips both to `.nan`, so
    "changing" one to the other would rewrite the note to bytes it already
    holds."""
    original = "---\nn: .nan\n# comment kept\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"n": float("nan")})
    )
    assert "No changes" in result
    assert read(vault, "n.md") == original


def test_opposite_infinities_are_a_change(vault):
    write(vault, "n.md", "---\nbound: .inf\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"bound": float("-inf")})
    )
    assert "Updated frontmatter" in result
    fm, _ = vault_service.parse_frontmatter(read(vault, "n.md"))
    assert fm["bound"] == float("-inf")


# ── lone-CR notes ───────────────────────────────────────────────────────────


def test_a_lone_cr_block_is_updated_rather_than_prepended(vault):
    """The read path recognises this note's block through universal-newline
    translation. While the write path did not, `set_frontmatter` merged into an
    empty mapping and prepended a SECOND block above the real one."""
    write(vault, "n.md", "---\rtitle: Keep\r---\rbody\r")
    result = asyncio.run(tools.set_frontmatter_impl("n.md", updates={"status": "done"}))
    assert "Updated frontmatter" in result
    after = read(vault, "n.md")
    fm, body = vault_service.parse_frontmatter(after)
    assert fm == {"title": "Keep", "status": "done"}
    assert body == "body\r"
    # Exactly one block: no second one prepended above the first.
    assert after.count("---") == 2


@pytest.mark.parametrize(
    "original,defect",
    [
        ("---\ra: [\r---\rbody\r", "does not parse as YAML"),
        ("---\r- a\r---\rbody\r", "not a mapping"),
        ("---\rnope\r", "never closed"),
    ],
)
def test_a_defective_lone_cr_block_is_refused(vault, original, defect):
    write(vault, "n.md", original)
    result = asyncio.run(tools.set_frontmatter_impl("n.md", updates={"a": 1}))
    assert "malformed frontmatter block" in result
    assert defect in result
    assert read(vault, "n.md") == original


# ── recursive YAML aliases ──────────────────────────────────────────────────


def test_a_recursive_alias_no_op_does_not_raise(vault):
    """`a: &A [*A]` is valid YAML, and `safe_load` returns a genuinely
    self-referencing list. The net-change comparison walks the mapping, so
    without cycle detection even a remove-of-nothing died with a
    RecursionError — failing closed, but regressing input the parser accepts."""
    original = "---\na: &A [*A]\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={}, remove=["absent"])
    )
    assert "No changes" in result
    assert read(vault, "n.md") == original


def test_a_recursive_alias_update_to_the_same_value_is_a_no_op(vault):
    original = "---\na: &A [*A]\nstatus: draft\n---\nbody\n"
    write(vault, "n.md", original)
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"status": "draft"})
    )
    assert "No changes" in result
    assert read(vault, "n.md") == original


def test_a_recursive_alias_note_still_accepts_a_real_change(vault):
    """Cycle detection must answer the comparison, not short-circuit the
    tool: a genuine change on such a note still writes."""
    write(vault, "n.md", "---\na: &A [*A]\nstatus: draft\n---\nbody\n")
    result = asyncio.run(
        tools.set_frontmatter_impl("n.md", updates={"status": "done"})
    )
    assert "set: status" in result
    assert "status: done" in read(vault, "n.md")


def test_the_comparison_itself_terminates_on_self_referencing_input():
    import copy
    import yaml

    fm = yaml.safe_load("a: &A [*A]\n")
    assert fm["a"][0] is fm["a"]          # genuinely cyclic
    assert tools._same_frontmatter_value(copy.deepcopy(fm), fm) is True
    assert tools._same_frontmatter_value(fm, fm) is True
    # A revisited pair is treated as equal, which is sound because `all()`
    # short-circuits — an unequal pair propagates out before anything can
    # revisit it. So a real difference beside a cycle is still detected.
    other = yaml.safe_load("a: &A [*A]\n")
    other["b"] = 1
    assert tools._same_frontmatter_value(fm, other) is False
