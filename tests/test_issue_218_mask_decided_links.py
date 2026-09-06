"""Code masking may hide links, but must never manufacture their targets."""

import pytest

from src.services import indexer
from src.services.embeddings import clean_at_version
from src.services.links import extract_links, extract_links_bounded


@pytest.mark.parametrize("candidate", [
    "[[`x`Old]]", "![[`x`Old]]", "[[Old`x`]]", "[[Ol`x`d]]",
    "[t](`x`Old.md)", "[t](Ol`x`d.md)", "[t](<`x`Old.md>)",
    "[t](<Ol`x`d.md#section>)", "[[` `Old]]",
])
def test_masked_deciding_span_is_rejected(candidate):
    assert extract_links(candidate) == []
    assert extract_links_bounded(candidate, max_links=0) == ([], False)


@pytest.mark.parametrize("candidate,target,kind", [
    ("[[Old|`alias`]]", "Old", "link"),
    ("![[Old#`anchor`|`alias`]]", "Old", "embed"),
    ("[`label`](Old.md#`anchor`)", "Old", "markdown"),
    ("[`label`](<Old.md#`anchor`>)", "Old", "markdown"),
    ("[[  Café 🐈  ]]", "Café 🐈", "link"),
    ("[t](  Café%20🐈.md)", "Café 🐈", "markdown"),
    ("[t](<Note (draft).md>)", "Note (draft)", "markdown"),
])
def test_unchanged_deciding_spans_keep_target_and_position(candidate, target, kind):
    prefix = "🌍 café\n"
    links = extract_links(prefix + candidate)
    assert [(link.target, link.kind, link.position) for link in links] == [
        (target, kind, len(prefix))
    ]


@pytest.mark.parametrize("rejected", ["[[`x`Old]]", "![[Old`x`]]", "[t](<`x`Old.md>)"])
@pytest.mark.parametrize("placement", ["before", "after", "both"])
@pytest.mark.parametrize("extra", [False, True])
def test_rejected_candidates_do_not_spend_cap_or_set_truncation(rejected, placement, extra):
    valid = "[first](One.md) [[Two]]"
    body = (rejected if placement in {"before", "both"} else "") + valid
    body += rejected if placement in {"after", "both"} else ""
    if extra:
        body += " ![[Three]]"
    links, truncated = extract_links_bounded(body, max_links=2)
    assert [link.target for link in links] == ["One", "Two"]
    assert truncated is extra


def test_zero_cap_reports_only_valid_overflow():
    assert extract_links_bounded("[[`x`Old]] [t](Old.md)", max_links=0) == ([], True)


def test_unbounded_historical_kind_order_is_preserved():
    links = extract_links("[t](One.md) [[`x`Bad]] [[Two]] [t](`x`Bad.md) ![[Three]]")
    assert [link.target for link in links] == ["Two", "Three", "One"]


@pytest.mark.parametrize("body", [
    "[[`x`Old]] [t](<`x`Old.md>) #tag",
    "```\nfenced code\n```\n[[Old|`alias`]]",
    "~~~\nfenced code\n~~~\n[t](Old.md#`anchor`)",
    "🌍 prose", "",
])
def test_version_three_preserves_version_two_embedding_input(body):
    assert indexer.CURRENT_EXTRACTION_VERSION == 3
    assert clean_at_version(3, body) == clean_at_version(2, body)
    assert indexer._grammar_changed_the_embedding_text(2, body) is False
