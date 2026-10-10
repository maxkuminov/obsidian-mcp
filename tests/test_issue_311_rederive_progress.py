"""#311: re-derive progress — the per-row `derived_under` marker, offline.

The digest's definition, the scan's per-row eligibility under a re-derive,
and the writer rule ("only the re-derive tail writes a non-NULL marker"). The
pass's behaviour against real rows — carried-forward rows not rewritten, the
narrowed withholding, the invariant re-read, completion re-resolution, the
writers that clear — is `tests/integration/test_issue_311_rederive_progress_pg.py`.
"""

from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path

import pytest

from src.services import indexer

ROOT = Path(__file__).resolve().parents[1]


def h(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def facts(handle="1:aa", assignment="/vaults/a", realpath="/vaults/a"):
    return indexer.RootFacts(
        assignment=assignment,
        realpath=realpath,
        realpath_hex=indexer.encode_realpath(realpath),
        handle=handle,
    )


V = indexer.CURRENT_EXTRACTION_VERSION


# ── the digest (design D2) ────────────────────────────────────────────────


def test_the_digest_is_sha256_hex_and_deterministic():
    d = indexer.derived_under_digest(facts(), "a.md", h("a"), V)
    assert re.fullmatch(r"[0-9a-f]{64}", d)
    assert d == indexer.derived_under_digest(facts(), "a.md", h("a"), V)


@pytest.mark.parametrize(
    "other",
    [
        lambda: indexer.derived_under_digest(facts(assignment="/vaults/b"), "a.md", h("a"), V),
        lambda: indexer.derived_under_digest(facts(realpath="/vaults/b"), "a.md", h("a"), V),
        lambda: indexer.derived_under_digest(facts(handle="1:bb"), "a.md", h("a"), V),
        lambda: indexer.derived_under_digest(facts(handle=None), "a.md", h("a"), V),
        lambda: indexer.derived_under_digest(facts(), "b.md", h("a"), V),
        lambda: indexer.derived_under_digest(facts(), "a.md", h("b"), V),
        lambda: indexer.derived_under_digest(facts(), "a.md", h("a"), V + 1),
    ],
    ids=["assignment", "realpath", "handle", "handle-null", "path", "hash", "version"],
)
def test_every_bound_input_changes_the_digest(other):
    """The facts are compared exactly — an absent handle is a value, unlike
    `classify_provenance`'s tolerance — and the row's path, hash and
    extraction version are bound in (D3)."""
    assert other() != indexer.derived_under_digest(facts(), "a.md", h("a"), V)


def test_the_digest_is_total_over_surrogate_escaped_strings():
    weird = "caf\udcff.md"
    d = indexer.derived_under_digest(
        facts(assignment="/v/\udcfe", realpath="/v/\udcfe"), weird, h("x"), V
    )
    assert len(d) == 64


def test_row_is_current_requires_facts_a_row_and_a_matching_marker():
    f = facts()
    good = indexer.derived_under_digest(f, "a.md", h("a"), V)
    row = indexer.SnapshotRow(h("a"), V, None, good)
    assert indexer._row_is_current(f, "a.md", row) is True
    assert indexer._row_is_current(None, "a.md", row) is False
    assert indexer._row_is_current(f, "a.md", None) is False
    assert indexer._row_is_current(f, "b.md", row) is False, "bound to the path"
    assert indexer._row_is_current(
        f, "a.md", indexer.SnapshotRow(h("a"), V, None, None)
    ) is False
    assert indexer._row_is_current(
        facts(handle="1:bb"), "a.md", row
    ) is False, "a handle-mismatch re-derive does not trust the old directory"


def test_the_marker_is_part_of_the_c4_comparison():
    a = indexer.SnapshotRow(h("a"), V, None, None)
    b = indexer.SnapshotRow(h("a"), V, None, "x" * 64)
    assert a != b


# ── per-row eligibility in the scan (design D4) ───────────────────────────


@pytest.fixture
def vault(tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    return root


def _settled(path: Path) -> tuple:
    """Age the file past the racy window and return its recorded stat."""
    old = time.time() - 60
    os.utime(path, (old, old))
    st = os.stat(path)
    return indexer._stat_tuple(st)


def test_a_re_derive_uses_the_shortcut_only_for_a_current_row(vault):
    f = facts()
    for name in ("Current.md", "Foreign.md", "Null.md"):
        (vault / name).write_text(f"{name}\n", encoding="utf-8")
    stats = {n: _settled(vault / n) for n in ("Current.md", "Foreign.md", "Null.md")}
    snapshot = {
        "Current.md": indexer.SnapshotRow(
            h("Current.md\n"), V, stats["Current.md"],
            indexer.derived_under_digest(f, "Current.md", h("Current.md\n"), V),
        ),
        "Foreign.md": indexer.SnapshotRow(
            h("Foreign.md\n"), V, stats["Foreign.md"],
            indexer.derived_under_digest(
                facts(handle="9:99"), "Foreign.md", h("Foreign.md\n"), V
            ),
        ),
        "Null.md": indexer.SnapshotRow(h("Null.md\n"), V, stats["Null.md"], None),
    }
    with indexer.pinned_root(vault) as fd:
        result = indexer._scan_vault(
            fd, snapshot, force_read=False, re_derive=True,
            stop=threading.Event(), facts=f,
        )
    assert result.files["Current.md"].read is False
    assert result.files["Current.md"].raw is None
    for name in ("Foreign.md", "Null.md"):
        assert result.files[name].read is True
        assert result.files[name].raw == f"{name}\n", "a not-current row needs its body"


def test_a_re_derive_without_facts_trusts_no_row(vault):
    f = facts()
    (vault / "a.md").write_text("a\n", encoding="utf-8")
    st = _settled(vault / "a.md")
    snapshot = {
        "a.md": indexer.SnapshotRow(
            h("a\n"), V, st, indexer.derived_under_digest(f, "a.md", h("a\n"), V)
        )
    }
    with indexer.pinned_root(vault) as fd:
        result = indexer._scan_vault(
            fd, snapshot, force_read=False, re_derive=True, stop=threading.Event()
        )
    assert result.files["a.md"].read is True
    assert result.files["a.md"].raw == "a\n"


def test_a_full_hash_pass_still_reads_a_current_row(vault):
    f = facts()
    (vault / "a.md").write_text("a\n", encoding="utf-8")
    st = _settled(vault / "a.md")
    snapshot = {
        "a.md": indexer.SnapshotRow(
            h("a\n"), V, st, indexer.derived_under_digest(f, "a.md", h("a\n"), V)
        )
    }
    with indexer.pinned_root(vault) as fd:
        result = indexer._scan_vault(
            fd, snapshot, force_read=True, re_derive=True,
            stop=threading.Event(), facts=f,
        )
    assert result.files["a.md"].read is True
    # Hash equal and the row current: no body needed, ordinary detection.
    assert result.files["a.md"].raw is None


def test_needs_body_under_re_derive_is_per_row():
    f = facts()
    cur = indexer.SnapshotRow(
        h("a"), V, None, indexer.derived_under_digest(f, "a.md", h("a"), V)
    )
    snap = {"a.md": cur}
    assert indexer._needs_body("a.md", h("a"), snap, True, f) is False
    assert indexer._needs_body("a.md", h("b"), snap, True, f) is True
    assert indexer._needs_body("a.md", h("a"), snap, True, None) is True
    assert indexer._needs_body("a.md", h("a"), snap, False, None) is False


# ── the writer rule (design D3) ───────────────────────────────────────────


def test_only_the_re_derive_tail_writes_a_non_null_marker():
    """Every `derived_under` write in `src/` is a NULL, except exactly one:
    the tail's `SET derived_under = :d`."""
    non_null: list[str] = []
    writes = re.compile(
        r"SET derived_under\s*=\s*(\S+)"            # SQL text
        r"|values\(derived_under\s*=\s*([^,)\s]+)"  # ORM update().values
        r"|\"derived_under\":\s*([^,}\s]+)"          # upsert set_ mapping
    )
    for path in sorted((ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for m in writes.finditer(text):
            value = next(g for g in m.groups() if g is not None)
            if value.strip("\"'") in ("NULL", "None", "null()"):
                continue
            non_null.append(f"{path.relative_to(ROOT)}: {m.group(0)}")
    assert non_null == [
        "src/services/indexer.py: SET derived_under = :d"
    ], non_null


def test_the_writers_that_mutate_link_state_clear_the_marker():
    tools = (ROOT / "src" / "mcp_server" / "tools.py").read_text(encoding="utf-8")
    idx = (ROOT / "src" / "services" / "indexer.py").read_text(encoding="utf-8")
    # move_note: the moved row and the sources whose links it rewrites.
    assert "derived_under=None" in tools
    assert "NoteLink.target_path == from_rel" in tools
    # The pass's move branch and the link backfill.
    assert "move_clear_sql" in idx
    assert idx.count(".values(derived_under=None)") >= 1
