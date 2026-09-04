"""The indexer's link rebuild is bounded, off-loop, and declares its cap (#203).

Three properties, none of which the pre-#203 indexer had:

1. **Peak link-row memory is one note plus one insert batch**, not one pass.
   The old rebuild accumulated every changed note's rows into a single
   `new_rows` list and inserted the lot at the end, so the peak scaled with the
   number of changed notes — and a re-derive makes *every* note changed. The
   test instruments both ends of the buffer (rows produced by extraction, rows
   handed to an INSERT) and asserts the running difference never exceeds the
   bound. It is written so the old shape fails it: the assertion is strictly
   below the pass's total.

2. **A note over `MAX_LINKS_PER_NOTE` is a declared degradation, not a skip.**
   Exactly the cap is persisted, in document order across both link kinds; the
   note is marked `links_truncated`; one ERROR line names the path and the cap;
   and `skips` stays empty, which is precisely the condition
   `_index_vault_pinned` reads to decide whether a re-derive records its
   provenance (A.7a). A skip here would hold a tenant with one generated MOC in
   re-derive mode for ever, with no repair that could end it.

3. **Extraction runs off the event loop**, and the extraction-version bump that
   re-derives the vault under the new link grammar costs no re-embedding.

The link-rebuild cases drive `_update_links_for_changed` directly against a
fake async session — the same offline technique as
`tests/test_issue_13_reresolve_shared_stem.py` — because what is under test is
the shape of the write traffic, and a real database would hide it behind rows.
"""
import ast
import asyncio
import inspect
import logging
import textwrap

import pytest

import src.services.indexer as indexer
from src.services.embeddings import clean_at_version
from src.services.indexer import _update_links_for_changed

LOGGER_NAME = "src.services.indexer"


# ── The fake session ──────────────────────────────────────────────────────


class _Row:
    def __init__(self, file_path, id):
        self.file_path = file_path
        self.id = id


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows

    def fetchall(self):
        return self._rows


class _RecordingSession:
    """Records what the rebuild writes, in the order it writes it.

    `link_inserts` is one entry per INSERT INTO note_links, each the list of row
    dicts that statement carried — which is what makes the batch bound and the
    per-note flush observable. `flag_updates` is one entry per
    `notes_metadata.links_truncated` UPDATE as `(value, [ids])`.
    """

    def __init__(self, rows):
        self._rows = rows
        self._select_served = False
        self.link_inserts: list[list[dict]] = []
        self.flag_updates: list[tuple[bool, list[int]]] = []
        self.other: list[object] = []

    async def execute(self, statement, params=None):
        table = getattr(getattr(statement, "table", None), "name", None)
        multi = getattr(statement, "_multi_values", None)

        if table == "note_links" and multi:
            self.link_inserts.append(
                [{c.name: v for c, v in row.items()} for row in multi[0]]
            )
            return _FakeResult([])

        if table == "notes_metadata" and getattr(statement, "_values", None):
            compiled = statement.compile().params
            self.flag_updates.append(
                (compiled["links_truncated"], list(compiled.get("id_1", [])))
            )
            return _FakeResult([])

        sql = getattr(statement, "text", None)
        if isinstance(sql, str):
            self.other.append((sql, params or {}))
            return _FakeResult([])

        if not self._select_served:
            self._select_served = True
            return _FakeResult(self._rows)
        self.other.append(statement)
        return _FakeResult([])

    async def commit(self):
        pass


def _run_rebuild(session, tmp_path, changed, bodies, skips):
    return asyncio.run(
        _update_links_for_changed(
            session,
            tmp_path,
            changed,
            user_id=None,
            path_to_content=bodies,
            skips=skips,
        )
    )


# ── 1. Peak buffered link rows ────────────────────────────────────────────


def test_peak_buffered_link_rows_is_one_note_plus_one_batch(tmp_path, monkeypatch):
    """The invariant the per-note flush exists to establish.

    Four link-heavy changed notes in one pass. `live` tracks rows that have been
    derived but not yet handed to an INSERT; its high-water mark is the thing
    the old `new_rows` accumulation made proportional to the pass.
    """
    per_note = 2500
    notes = 4
    body = " ".join(f"[[Target{i}]]" for i in range(per_note))
    paths = [f"n{i}.md" for i in range(notes)]
    bodies = {p: body for p in paths}
    rows = [_Row(p, i + 1) for i, p in enumerate(paths)]

    real = indexer.extract_links_bounded
    live = 0
    peak = 0

    def counting_extract(*args, **kwargs):
        nonlocal live, peak
        links, truncated = real(*args, **kwargs)
        live += len(links)
        peak = max(peak, live)
        return links, truncated

    monkeypatch.setattr(indexer, "extract_links_bounded", counting_extract)

    session = _RecordingSession(rows)
    real_execute = session.execute

    async def counting_execute(statement, params=None):
        nonlocal live
        before = len(session.link_inserts)
        result = await real_execute(statement, params)
        if len(session.link_inserts) > before:
            live -= len(session.link_inserts[-1])
        return result

    session.execute = counting_execute

    _run_rebuild(session, tmp_path, paths, bodies, [])

    inserted = sum(len(batch) for batch in session.link_inserts)
    assert inserted == per_note * notes, "every note's rows must still be written"

    # No statement carries more than one batch.
    assert max(len(b) for b in session.link_inserts) <= 1000

    # The bound the requirement states.
    assert peak <= per_note + 1000, (
        f"peak buffered link rows was {peak}, above one note ({per_note}) plus "
        "one insert batch (1000) — the rebuild is accumulating across notes"
    )
    # And it is a real measurement, not a vacuous one: a whole note *was*
    # buffered at some point, and the peak is strictly below what the
    # pass-wide accumulation would have held.
    assert peak >= per_note
    assert peak < inserted


# ── 2. The cap is a declared degradation, not a skip ──────────────────────


def _mixed_body(pairs: int) -> str:
    """`[[W0]] [t](m0.md) [[W1]] [t](m1.md) …` — both link kinds interleaved,
    so "the first N in document order" is distinguishable from "the first N
    wikilinks, then markdown links", which is what extraction's two sequential
    loops would otherwise produce."""
    out = []
    for i in range(pairs):
        out.append(f"[[W{i}]]")
        out.append(f"[t](m{i}.md)")
    return " ".join(out)


def test_an_over_cap_note_persists_exactly_the_cap_in_document_order(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(indexer, "MAX_LINKS_PER_NOTE", 5)
    body = _mixed_body(20)
    session = _RecordingSession([_Row("moc.md", 1)])
    skips: list[str] = []

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        _run_rebuild(session, tmp_path, ["moc.md"], {"moc.md": body}, skips)

    persisted = [row for batch in session.link_inserts for row in batch]
    assert len(persisted) == 5, "exactly the cap, no more and no fewer"

    # Document order across both kinds: W0, m0, W1, m1, W2.
    assert [r["kind"] for r in persisted] == [
        "link",
        "markdown",
        "link",
        "markdown",
        "link",
    ]
    assert [r["target_path"] for r in persisted] == ["W0", "m0", "W1", "m1", "W2"]
    positions = [r["position"] for r in persisted]
    assert positions == sorted(positions)


def test_an_over_cap_note_is_marked_logged_and_is_not_a_skip(
    tmp_path, monkeypatch, caplog
):
    monkeypatch.setattr(indexer, "MAX_LINKS_PER_NOTE", 5)
    session = _RecordingSession([_Row("moc.md", 1)])
    skips: list[str] = []

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        _run_rebuild(session, tmp_path, ["moc.md"], {"moc.md": _mixed_body(20)}, skips)

    # The durable marker.
    assert (True, [1]) in session.flag_updates, session.flag_updates

    # Exactly one ERROR line, naming the path and the cap.
    errors = [
        r for r in caplog.records
        if r.name == LOGGER_NAME and r.levelno == logging.ERROR
    ]
    assert len(errors) == 1, [r.getMessage() for r in errors]
    message = errors[0].getMessage()
    assert "moc.md" in message
    assert "MAX_LINKS_PER_NOTE=5" in message

    # **Not a skip.** An empty `skips` is exactly the condition
    # `_index_vault_pinned` reads to decide that a re-deriving pass may record
    # the provenance of the directory it scanned (A.7a). A capped note must not
    # withhold that record: the truncation is deterministic and the rows written
    # are exactly the rows derived, so the pass's structural claim still holds,
    # and treating it as a skip would leave a tenant with one generated MOC in
    # re-derive mode with no repair that could ever end it.
    assert skips == [], skips


def test_the_marker_is_cleared_when_the_note_falls_back_under_the_cap(
    tmp_path, monkeypatch
):
    """Set, then cleared. The marker is derived state and has to track the note,
    or a note edited down to three links would answer `truncated: true` for
    ever."""
    monkeypatch.setattr(indexer, "MAX_LINKS_PER_NOTE", 5)
    rows = [_Row("moc.md", 1)]

    over = _RecordingSession(rows)
    _run_rebuild(over, tmp_path, ["moc.md"], {"moc.md": _mixed_body(20)}, [])
    assert (True, [1]) in over.flag_updates

    under = _RecordingSession(rows)
    _run_rebuild(under, tmp_path, ["moc.md"], {"moc.md": "[[Only]] [[Two]]"}, [])
    assert (False, [1]) in under.flag_updates, under.flag_updates
    assert not any(value for value, _ in under.flag_updates)


def test_a_note_under_the_cap_is_never_marked(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(indexer, "MAX_LINKS_PER_NOTE", 5)
    session = _RecordingSession([_Row("small.md", 1)])
    skips: list[str] = []

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        _run_rebuild(
            session, tmp_path, ["small.md"], {"small.md": "[[A]] [[B]]"}, skips
        )

    assert session.flag_updates == [(False, [1])]
    assert [r for r in caplog.records if r.name == LOGGER_NAME] == []
    assert skips == []


def test_a_note_exactly_at_the_cap_is_not_truncated(tmp_path, monkeypatch):
    """The boundary. `truncated` must mean "the note holds more links than the
    cap", not "the note holds at least the cap" — otherwise every note of
    exactly 10,000 links would claim an incompleteness that did not happen."""
    monkeypatch.setattr(indexer, "MAX_LINKS_PER_NOTE", 4)
    body = " ".join(f"[[W{i}]]" for i in range(4))
    session = _RecordingSession([_Row("edge.md", 1)])

    _run_rebuild(session, tmp_path, ["edge.md"], {"edge.md": body}, [])

    persisted = [row for batch in session.link_inserts for row in batch]
    assert len(persisted) == 4
    assert session.flag_updates == [(False, [1])]


# ── 3. Off the event loop ─────────────────────────────────────────────────


def test_the_changed_path_rebuild_extracts_through_to_thread(tmp_path, monkeypatch):
    """The observable the spec names is the dispatch itself, not a race against
    a concurrent request: a timing assertion on a shared CI runner is a flake
    generator, and a thread only yields between `re` calls anyway."""
    dispatched: list[str] = []
    real_to_thread = asyncio.to_thread

    async def spy(func, /, *args, **kwargs):
        dispatched.append(getattr(func, "__name__", repr(func)))
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", spy)

    session = _RecordingSession([_Row("a.md", 1)])
    _run_rebuild(session, tmp_path, ["a.md"], {"a.md": "[[B]]"}, [])

    assert "extract_links_bounded" in dispatched, dispatched


def _to_thread_targets(func) -> set[str]:
    """Names passed as the first argument of an `asyncio.to_thread(...)` call
    anywhere in `func`'s source."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    targets: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        callee = node.func
        if (
            isinstance(callee, ast.Attribute)
            and callee.attr == "to_thread"
            and isinstance(callee.value, ast.Name)
            and callee.value.id == "asyncio"
        ):
            first = node.args[0]
            if isinstance(first, ast.Name):
                targets.add(first.id)
            elif isinstance(first, ast.Attribute):
                targets.add(first.attr)
    return targets


def test_the_scan_dispatches_tag_extraction_off_the_loop():
    """`extract_tags` runs once per *changed* note — every note of every user on
    the pass that follows an extraction bump — and it regexes the whole body.
    Driving `_index_vault_pinned` needs a database, so this reads the call site:
    an assertion that fails the moment the dispatch is unwrapped."""
    assert "extract_tags" in _to_thread_targets(indexer._index_vault_pinned)


def test_the_backfill_dispatches_bounded_extraction_off_the_loop():
    """The one-shot backfill walks the whole vault in a single pass, so it is
    the longest-running holder of the loop in the process."""
    targets = _to_thread_targets(indexer._link_backfill_pinned)
    assert "extract_links_bounded" in targets, targets


def test_the_backfill_uses_the_bounded_extractor_with_the_cap():
    """Belt and braces on the same call site: the bound is only a bound if the
    cap is actually passed."""
    source = inspect.getsource(indexer._link_backfill_pinned)
    assert "max_links=MAX_LINKS_PER_NOTE" in source


# ── 4. The version-2 bump re-derives links without re-embedding ───────────

# Bodies that exercise the fence grammar, since that is what the cleaner sees.
_CORPUS = [
    "plain text with [[a link]] and [t](x.md)",
    "before\n```python\ncode [[not a link]]\n```\nafter",
    "~~~\ntilde fenced\n~~~\ntail",
    "unclosed ```opener\nand the rest of the note",
    "inline `code [[here]]` and prose",
    "",
    "# Heading\n\n- [[one]]\n- [[two]]\n",
]


def test_the_extraction_version_is_two():
    assert indexer.CURRENT_EXTRACTION_VERSION == 2


def test_version_two_cleans_identically_to_version_one():
    """Version 2 is version 1's cleaning function under a new key. That is the
    whole design: the *link* grammar changed, the *fence* grammar did not, so
    the marker has to move (nothing else can see a link-grammar change) while
    the embedded text must not."""
    for body in _CORPUS:
        assert clean_at_version(2, body) == clean_at_version(1, body), body


def test_the_bump_re_derives_links_but_re_embeds_nothing():
    """The scoped-invalidation predicate answers False for every v1-stamped row,
    so the pass that re-extracts every note's links makes no embedding call on
    account of the bump — the cost is one link-and-tag pass, not a re-embed of
    the vault."""
    for body in _CORPUS:
        assert indexer._grammar_changed_the_embedding_text(1, body) is False, body


def test_the_predicate_still_detects_a_real_grammar_difference():
    """The companion assertion, without which the test above is satisfied by a
    predicate that has stopped working. v0's cleaner genuinely differs from
    v1's on an unclosed opener, and that difference must still be seen."""
    body = "text\n```unclosed\nmore text"
    assert clean_at_version(0, body) != clean_at_version(1, body)
    assert indexer._grammar_changed_the_embedding_text(0, body) is True


def test_an_unknown_stamped_version_still_forces_a_re_embed():
    """A build downgraded past a bump must re-embed rather than certify against
    a comparison it could not make."""
    assert indexer._grammar_changed_the_embedding_text(99, "anything") is True


# ── 5. The A.7a carve-out is recorded where the rule is enforced ──────────


@pytest.mark.parametrize(
    "func", [indexer._index_vault_pinned, indexer._format_skips]
)
def test_the_capped_note_carve_out_is_documented_at_the_skip_machinery(func):
    """The rule "a capped note is not a skip" is only safe if the next reader of
    the skip machinery finds out about it there. Both the declaration that
    withholds the stamp and the reporter that names offenders say so."""
    source = inspect.getsource(func)
    assert "links_truncated" in source or "truncat" in source.lower(), source
