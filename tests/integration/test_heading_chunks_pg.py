"""Heading-aware chunks on a real PostgreSQL (#355, tasks 3.5a, 7.1–7.4).

* the transition: pre-034 rows keep serving (fresh, previewed, no locator)
  while the per-note chunking-version backlog re-embeds them, and converges;
* membership at the cutoff across budgeted passes, the `index_transition:`
  line and `/health`'s non-volumetric `indexer.rechunk`;
* a rollback to a lower `CHUNKING_VERSION` is still a transition;
* the marker is owner-scoped;
* real edits go first and the version-only segment is budgeted in single-user
  mode;
* reuse keyed on `(embed_prefix, chunk_text)` under the chunking version;
* the title/tags invalidation in the upsert and the full-hash digest backstop;
* end-to-end ordinal parity through `read_note_impl(section="#N")`.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import asyncio
import hashlib
import json
import time

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
import src.mcp_server.tools as tools
from src.config import settings
from src.models.db import NoteEmbedding, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import index_state, indexer, indexer_health
from src.services import vault as vault_service
from src.services.index_state import KEY_EMBEDDING_FINGERPRINT
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)


def sha(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def vector_of(seed: str) -> list[float]:
    out: list[float] = []
    block = seed.encode("utf-8")
    while len(out) < DIM:
        block = hashlib.sha256(block).digest()
        out.extend(b / 255.0 - 0.5 for b in block)
    return out[:DIM]


class Provider:
    """Deterministic 'model'; records every document input and query."""

    def __init__(self):
        self.documents: list[str] = []
        self.queries: list[str] = []
        self.overrides: dict[str, list[float]] = {}
        self.delay = 0.0

    def vector(self, text_in: str) -> list[float]:
        for needle, vec in self.overrides.items():
            if needle in text_in:
                return list(vec)
        return vector_of(text_in)

    async def batch(self, texts, *, kind):
        assert kind == "document"
        if self.delay:
            await asyncio.sleep(self.delay)
        self.documents.extend(texts)
        return [self.vector(t) for t in texts]

    async def one(self, text_in, *, kind):
        assert kind == "query"
        self.queries.append(text_in)
        return self.vector(text_in)


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("heading_chunks_355", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def world(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir()
    for target in (settings, indexer.settings, tools.settings):
        monkeypatch.setattr(target, "vault_path", str(root), raising=False)
    monkeypatch.setattr(settings, "multi_user_mode", False, raising=False)
    monkeypatch.setattr(settings, "embedding_exclude_patterns", [], raising=False)
    monkeypatch.setattr(settings, "embed_chunk_budget_per_user", 0, raising=False)
    monkeypatch.setattr(settings, "embed_time_budget_seconds_per_user", 0, raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(tools, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async def noop(*_a, **_k):
        return None

    monkeypatch.setattr(tools, "_log_usage", noop)
    provider = Provider()
    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", provider.batch)
    monkeypatch.setattr(embeddings_service, "get_embedding", provider.one)
    indexer._last_full_hash.clear()
    indexer_health.reset()
    vault_service.clear_user_vault_cache()

    async with sessionmaker() as session:
        for table in ("note_links", "note_embeddings", "notes_metadata",
                      "indexer_runs", "indexer_state", "users"):
            await session.execute(text(f"DELETE FROM {table}"))
        await index_state.set_state(
            session, KEY_EMBEDDING_FINGERPRINT, index_state.embedding_fingerprint()
        )
        await session.commit()
    yield {"root": root, "provider": provider}
    indexer._last_full_hash.clear()
    indexer_health.reset()


def write(root, rel, body):
    (root / rel).write_text(body, encoding="utf-8")


async def seed_legacy(sessionmaker, root, notes: dict[str, str], *, ages=None):
    """Index `notes`, then give each the pre-034 shape: one legacy chunk row
    (NULL locator, NULL prefix), certified for its hash, NULL version."""
    for rel, body in notes.items():
        write(root, rel, body)
    await indexer.index_vault(user_id=None)
    async with sessionmaker() as session:
        for i, rel in enumerate(notes):
            note = (await session.execute(
                select(NoteMetadata).where(NoteMetadata.file_path == rel)
            )).scalar_one()
            session.add(NoteEmbedding(
                note_id=note.id, chunk_index=0,
                chunk_text=f"legacy text of {rel}",
                embedding=vector_of(f"legacy {rel}"),
            ))
            note.embedded_content_hash = note.content_hash
            note.embedded_chunking_version = None
            note.embedded_input_digest = None
            if ages is not None:
                await session.execute(
                    text("UPDATE notes_metadata SET modified_at = now() - "
                         "make_interval(secs => :s) WHERE id = :i"),
                    {"s": ages[rel], "i": note.id},
                )
        await session.commit()


async def note_rows(sessionmaker):
    async with sessionmaker() as session:
        return {
            r.file_path: r
            for r in (await session.execute(select(
                NoteMetadata.id, NoteMetadata.file_path, NoteMetadata.content_hash,
                NoteMetadata.embedded_content_hash,
                NoteMetadata.embedded_chunking_version,
                NoteMetadata.embedded_input_digest,
            ))).all()
        }


async def embedding_rows(sessionmaker):
    async with sessionmaker() as session:
        return (await session.execute(text(
            "SELECT nm.file_path, ne.chunk_index, ne.chunk_text, ne.heading_path, "
            "ne.section_ordinal, ne.embed_prefix FROM note_embeddings ne "
            "JOIN notes_metadata nm ON nm.id = ne.note_id "
            "ORDER BY nm.file_path, ne.chunk_index"
        ))).all()


async def pending(sessionmaker, user_id=None):
    async with sessionmaker() as session:
        return await embeddings_service.rechunk_pending_count(session, user_id)


async def not_current(sessionmaker):
    async with sessionmaker() as session:
        return (await session.execute(
            select(func.count(NoteMetadata.id)).where(
                embeddings_service.vectors_not_current_expr()
            )
        )).scalar_one()


# ══════════════════════════════════════════════════════════════════════════
# 7.1 — the transition, end to end
# ══════════════════════════════════════════════════════════════════════════


async def test_legacy_rows_serve_unstale_then_converge(sessionmaker, world):
    root = world["root"]
    await seed_legacy(sessionmaker, root, {
        "A.md": "intro\n# Alpha\nalpha body\n",
        "B.md": "no headings at all, just prose\n",
        "C.md": "# Gamma\n## Delta\ndelta body\n",
    })
    async with sessionmaker() as session:
        stored = await index_state.get_state(session, KEY_EMBEDDING_FINGERPRINT)
    assert index_state.compare_fingerprint(
        stored, index_state.embedding_fingerprint()
    ).status is index_state.FingerprintStatus.MATCH
    assert await pending(sessionmaker) == 3
    assert await not_current(sessionmaker) == 3

    async with sessionmaker() as session:
        results = await embeddings_service.semantic_search(session, "alpha", limit=10)
    assert {r["path"] for r in results} == {"A.md", "B.md", "C.md"}
    for r in results:
        assert r["stale"] is False
        assert r["chunk"].startswith("legacy text of")
        assert r["section"] is None

    out = await tools.semantic_search_impl("alpha", limit=10)
    assert "index_transition: 3 notes not yet re-chunked" in out.splitlines()[1]
    assert "§" not in out

    await indexer.embed_vault(user_id=None)
    rows = await embedding_rows(sessionmaker)
    assert rows and all(r.embed_prefix is not None for r in rows)
    by_path = {}
    for r in rows:
        by_path.setdefault(r.file_path, []).append(r)
    assert [(r.heading_path, r.section_ordinal) for r in by_path["A.md"]] == [
        ([], 0), (["Alpha"], 1)
    ]
    assert by_path["A.md"][1].embed_prefix == "A > Alpha"
    assert by_path["C.md"][0].heading_path == ["Gamma", "Delta"]
    assert by_path["C.md"][0].section_ordinal == 2
    notes = await note_rows(sessionmaker)
    assert {n.embedded_chunking_version for n in notes.values()} == {1}
    assert all(n.embedded_input_digest for n in notes.values())
    assert await pending(sessionmaker) == 0
    assert await not_current(sessionmaker) == 0

    out = await tools.semantic_search_impl("alpha", limit=10)
    assert "index_transition" not in out
    assert '§ "Alpha" (#1)' in out or "§ (top of note)" in out
    assert indexer_health.snapshot()["rechunk"] == "complete"


# ══════════════════════════════════════════════════════════════════════════
# 7.1a — membership at the cutoff, /health, rollback, owner scoping
# ══════════════════════════════════════════════════════════════════════════


async def test_membership_across_budgeted_passes_and_health(
    sessionmaker, world, monkeypatch
):
    root, provider = world["root"], world["provider"]
    query = vector_of("the query")
    # Legacy A sits on the query; legacy B far from it.
    await seed_legacy(
        sessionmaker, root,
        {"A.md": "# A\nalpha\n", "B.md": "# B\nbravo\n"},
        ages={"A.md": 100, "B.md": 10},
    )
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE note_embeddings SET embedding = CAST(:v AS vector) "
            "WHERE chunk_text = 'legacy text of A.md'"
        ), {"v": str(query)})
        await session.commit()
    provider.overrides["the query"] = query
    # B's re-embedded vector lands on the query too.
    provider.overrides["bravo"] = query
    monkeypatch.setattr(settings, "embed_chunk_budget_per_user", 1, raising=False)

    await indexer.embed_vault(user_id=None)  # pass 1: B (newest) only
    assert await pending(sessionmaker) == 1
    out = await tools.semantic_search_impl("the query", limit=1)
    assert out.splitlines()[1].startswith("index_transition: 1 notes not yet re-chunked")
    health = indexer_health.snapshot()
    assert health["rechunk"] == "in_progress"
    assert health["status"] == "ok"
    assert "rechunk_pending" not in health
    assert all(not isinstance(v, int) or k != "rechunk" for k, v in health.items())

    await indexer.embed_vault(user_id=None)  # pass 2: A
    assert await pending(sessionmaker) == 0
    out = await tools.semantic_search_impl("the query", limit=1)
    assert "index_transition" not in out
    assert indexer_health.snapshot()["rechunk"] == "complete"


async def test_a_rollback_to_a_lower_version_is_still_a_transition(
    sessionmaker, world, monkeypatch
):
    root, provider = world["root"], world["provider"]
    await seed_legacy(
        sessionmaker, root,
        {"A.md": "# A\nalpha\n", "B.md": "# B\nbravo\n", "C.md": "# C\ncharlie\n"},
        ages={"A.md": 300, "B.md": 20, "C.md": 10},
    )
    await indexer.embed_vault(user_id=None)  # everything at version 1
    assert await pending(sessionmaker) == 0

    # A version-2 build re-embeds B and C (the two newest), then stops.
    monkeypatch.setattr(embeddings_service, "CHUNKING_VERSION", 2)
    monkeypatch.setattr(settings, "embed_chunk_budget_per_user", 2, raising=False)
    await indexer.embed_vault(user_id=None)
    stamps = {p: n.embedded_chunking_version for p, n in (await note_rows(sessionmaker)).items()}
    assert stamps == {"A.md": 1, "B.md": 2, "C.md": 2}

    # Recreate rollback to version 1, no schema downgrade.
    monkeypatch.setattr(embeddings_service, "CHUNKING_VERSION", 1)
    monkeypatch.setattr(settings, "embed_chunk_budget_per_user", 0, raising=False)
    assert await pending(sessionmaker) == 2
    out = await tools.semantic_search_impl("bravo", limit=5)
    assert out.splitlines()[1].startswith("index_transition: 2 notes not yet re-chunked")
    provider.documents.clear()
    await indexer.embed_vault(user_id=None)
    # Re-selected and re-embedded under version 1 — no reuse of version-2 rows.
    assert sorted(d.split("\n")[0] for d in provider.documents) == ["B > B", "C > C"]
    assert await pending(sessionmaker) == 0
    assert "index_transition" not in await tools.semantic_search_impl("bravo", limit=5)


async def test_the_marker_is_owner_scoped(sessionmaker, world):
    async with sessionmaker() as session:
        uid = (await session.execute(text(
            "INSERT INTO users (username, password_hash) VALUES ('other', 'x') "
            "RETURNING id"
        ))).scalar()
        for i in range(3):
            # Legacy vectors (the marker counts notes that HAVE vectors of
            # another generation), owned by the other tenant.
            nid = (await session.execute(text(
                "INSERT INTO notes_metadata (user_id, file_path, title, content_hash) "
                "VALUES (:u, :p, 't', 'h') RETURNING id"
            ), {"u": uid, "p": f"o{i}.md"})).scalar()
            session.add(NoteEmbedding(note_id=nid, chunk_index=0, chunk_text="x",
                                      embedding=vector_of(f"o{i}")))
        await session.commit()
    assert await pending(sessionmaker, None) == 0
    assert await pending(sessionmaker, uid) == 3
    out = await tools.semantic_search_impl("anything", limit=5)
    assert "index_transition" not in out


# ══════════════════════════════════════════════════════════════════════════
# 7.2 — ordering and the single-user budget
# ══════════════════════════════════════════════════════════════════════════


async def test_a_real_edit_goes_first_and_the_segment_stops_at_the_budget(
    sessionmaker, world, monkeypatch
):
    root, provider = world["root"], world["provider"]
    notes = {f"V{i}.md": f"# V{i}\nversion only {i}\n" for i in range(5)}
    notes["Edited.md"] = "# E\nold\n"
    await seed_legacy(
        sessionmaker, root, notes,
        ages={**{f"V{i}.md": i + 1 for i in range(5)}, "Edited.md": 1000},
    )
    async with sessionmaker() as session:
        # Edited.md is version-current but content-stale.
        await session.execute(text(
            "UPDATE notes_metadata SET embedded_chunking_version = 1 "
            "WHERE file_path = 'Edited.md'"
        ))
        await session.commit()
    write(root, "Edited.md", "# E\nnew text\n")
    await indexer.index_vault(user_id=None)
    provider.documents.clear()
    provider.delay = 0.6
    monkeypatch.setattr(settings, "embed_time_budget_seconds_per_user", 1, raising=False)
    result = await indexer.embed_vault(user_id=None)
    provider.delay = 0.0
    assert provider.documents[0].startswith("Edited > E\n")
    version_only = [d for d in provider.documents if d.startswith("V")]
    assert 1 <= len(version_only) < 5
    assert result.failures == 0
    assert await pending(sessionmaker) == 5 - len(version_only)


# ══════════════════════════════════════════════════════════════════════════
# 7.3 — reuse on PostgreSQL
# ══════════════════════════════════════════════════════════════════════════


async def _settle(sessionmaker, root, rel, body, provider):
    write(root, rel, body)
    await indexer.index_vault(user_id=None)
    provider.documents.clear()
    await indexer.embed_vault(user_id=None)
    return list(provider.documents)


async def test_reuse_append_inserted_heading_and_tag_change(sessionmaker, world):
    root, provider = world["root"], world["provider"]
    sent = await _settle(sessionmaker, root, "R.md", "# One\nfirst\n# Two\nsecond\n", provider)
    assert len(sent) == 2

    sent = await _settle(
        sessionmaker, root, "R.md", "# One\nfirst\n# Two\nsecond\n# Three\nthird\n", provider
    )
    assert sent == ["R > Three\n\nthird\n"], "an append sends only the new tail"

    sent = await _settle(
        sessionmaker, root, "R.md",
        "# Zero\nzeroth\n# One\nfirst\n# Two\nsecond\n# Three\nthird\n", provider,
    )
    assert sent == ["R > Zero\n\nzeroth\n"], "an inserted heading reuses the rest"
    rows = [r for r in await embedding_rows(sessionmaker) if r.file_path == "R.md"]
    assert [(r.heading_path, r.section_ordinal) for r in rows] == [
        (["Zero"], 1), (["One"], 2), (["Two"], 3), (["Three"], 4)
    ]

    sent = await _settle(
        sessionmaker, root, "R.md",
        "---\ntags: [fresh]\n---\n# Zero\nzeroth\n# One\nfirst\n# Two\nsecond\n# Three\nthird\n",
        provider,
    )
    assert len(sent) == 4 and all("tags: fresh" in s for s in sent), (
        "a tag change re-embeds every chunk"
    )


# ══════════════════════════════════════════════════════════════════════════
# 3.5a — title/tags invalidation and the full-hash backstop
# ══════════════════════════════════════════════════════════════════════════


async def test_changed_tags_under_a_rederive_invalidate_unchanged_do_not(
    sessionmaker, world
):
    root, provider = world["root"], world["provider"]
    write(root, "T.md", "# H\nbody #real\n")
    write(root, "U.md", "# H\nplain body\n")
    await indexer.index_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    async with sessionmaker() as session:
        # A stale marker; T's stored tags differ from what extraction yields.
        await session.execute(text(
            "UPDATE notes_metadata SET extraction_version = 3, "
            "tags = CASE WHEN file_path = 'T.md' THEN ARRAY['old']::varchar[] ELSE tags END"
        ))
        # As if T had been embedded under the old grammar's tags.
        await session.execute(text(
            "UPDATE note_embeddings SET embed_prefix = 'T > H' || chr(10) || 'tags: old' "
            "WHERE note_id = (SELECT id FROM notes_metadata WHERE file_path = 'T.md')"
        ))
        await session.commit()
    await indexer.index_vault(user_id=None)
    rows = await note_rows(sessionmaker)
    assert rows["T.md"].embedded_content_hash is None
    assert rows["U.md"].embedded_content_hash == rows["U.md"].content_hash
    provider.documents.clear()
    await indexer.embed_vault(user_id=None)
    assert provider.documents == ["T > H\ntags: real\n\nbody #real\n"]


async def test_the_full_hash_backstop_catches_a_corrupted_digest(sessionmaker, world):
    root, provider = world["root"], world["provider"]
    write(root, "D.md", "# H\ndigest body\n")
    write(root, "Ok.md", "# H\nfine body\n")
    await indexer.index_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE notes_metadata SET embedded_input_digest = repeat('0', 64) "
            "WHERE file_path = 'D.md'"
        ))
        await session.commit()
    provider.documents.clear()
    await indexer.index_vault(user_id=None, full_hash=True)
    assert provider.documents == [], "the backstop makes no provider call"
    rows = await note_rows(sessionmaker)
    assert rows["D.md"].embedded_content_hash is None
    assert rows["Ok.md"].embedded_content_hash == rows["Ok.md"].content_hash
    await indexer.embed_vault(user_id=None)
    rows = await note_rows(sessionmaker)
    assert rows["D.md"].embedded_input_digest != "0" * 64


# ══════════════════════════════════════════════════════════════════════════
# 7.4 — end-to-end parity through read_note
# ══════════════════════════════════════════════════════════════════════════

PARITY_NOTES = {
    "p1.md": "---\ntitle: P\n---\nintro\n# A\na text\n## B\nb text\n```\n# fenced\n```\nafter\n## B\nsecond b\n",
    "p2.md": "pre\r\n# X\r\nx body\r\n### Y\r\ny body\r\n",
    "p3.md": "lone\r# R\rr body\r## S\rs body\r",
    "p4.md": "# Only\n" + ("long line of words " * 400) + "\n# Next\nnext\n",
}


async def test_every_stored_ordinal_resolves_through_read_note(sessionmaker, world):
    root = world["root"]
    for rel, body in PARITY_NOTES.items():
        write(root, rel, body)
    await indexer.index_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    rows = [r for r in await embedding_rows(sessionmaker) if r.file_path in PARITY_NOTES]
    assert rows
    checked = 0
    for r in rows:
        if r.section_ordinal == 0:
            continue
        result = await tools.read_note_impl(r.file_path, section=f"#{r.section_ordinal}")
        assert result.heading is not None, (r.file_path, r.section_ordinal)
        assert result.heading.lstrip("#").strip() == r.heading_path[-1]
        raw = (root / r.file_path).read_text(encoding="utf-8")
        _fm, body = vault_service.parse_frontmatter(raw)
        cleaned = embeddings_service.clean_for_embedding(result.content)
        assert r.chunk_text in cleaned or r.chunk_text in embeddings_service._remove_spans(
            result.content,
            embeddings_service.scan_fences(result.content, context=embeddings_service.BODY).spans,
        ), (r.file_path, r.section_ordinal)
        checked += 1
    assert checked >= 6


async def test_health_stays_ok_through_a_long_healthy_transition(sessionmaker, world, monkeypatch):
    root = world["root"]
    await seed_legacy(sessionmaker, root, {f"N{i}.md": f"# N{i}\nbody {i}\n" for i in range(10)})
    monkeypatch.setattr(settings, "embed_chunk_budget_per_user", 1, raising=False)
    for _ in range(10):
        await indexer.embed_vault(user_id=None)
        assert indexer_health.snapshot()["status"] == "ok"
    assert await pending(sessionmaker) == 0
    assert indexer_health.snapshot()["rechunk"] == "complete"



# ══════════════════════════════════════════════════════════════════════════
# The marker's predicate (amended): vectors of another generation only
# ══════════════════════════════════════════════════════════════════════════


async def test_a_new_note_without_vectors_raises_no_marker(sessionmaker, world):
    """Created after deploy, not yet embedded: in the backlog (and pending on
    the dashboard), but not a generation mix, so no `index_transition:`."""
    root = world["root"]
    write(root, "New.md", "# N\nfresh\n")
    await indexer.index_vault(user_id=None)
    assert await not_current(sessionmaker) == 1
    assert await pending(sessionmaker) == 0
    assert "index_transition" not in await tools.semantic_search_impl("fresh", limit=5)
    await indexer.embed_vault(user_id=None)
    assert indexer_health.snapshot()["rechunk"] == "complete"


async def test_a_legacy_note_with_cleared_certification_still_counts(sessionmaker, world):
    """A tag edit clears a legacy note's certification; its old vectors are
    still served until it is re-embedded, so the marker still counts it."""
    root = world["root"]
    await seed_legacy(sessionmaker, root, {"L.md": "# L\nlegacy body\n"})
    write(root, "L.md", "---\ntags: [new]\n---\n# L\nlegacy body\n")
    await indexer.index_vault(user_id=None)
    rows = await note_rows(sessionmaker)
    assert rows["L.md"].embedded_content_hash is None
    assert await pending(sessionmaker) == 1
    out = await tools.semantic_search_impl("legacy", limit=5)
    assert out.splitlines()[1].startswith("index_transition: 1 notes not yet re-chunked")


# ══════════════════════════════════════════════════════════════════════════
# V3 — NULL and {} tags are one prefix; V4 — empty/excluded converge free
# ══════════════════════════════════════════════════════════════════════════


async def test_null_and_empty_tags_compare_equal_in_the_upsert(sessionmaker, world):
    root = world["root"]
    write(root, "N.md", "# H\nno tags here\n")
    write(root, "A.md", "# H\nno tags either\n")
    await indexer.index_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    async with sessionmaker() as session:
        await session.execute(text(
            "UPDATE notes_metadata SET extraction_version = 3, tags = CASE "
            "WHEN file_path = 'N.md' THEN NULL ELSE ARRAY['a']::varchar[] END"
        ))
        await session.commit()
    await indexer.index_vault(user_id=None)
    rows = await note_rows(sessionmaker)
    assert rows["N.md"].embedded_content_hash == rows["N.md"].content_hash, "NULL -> [] kept"
    assert rows["A.md"].embedded_content_hash is None, "['a'] -> [] cleared"


async def test_excluded_and_empty_legacy_notes_converge_without_a_provider_call(
    sessionmaker, world, monkeypatch
):
    root, provider = world["root"], world["provider"]
    (root / "Private").mkdir()
    monkeypatch.setattr(settings, "embedding_exclude_patterns", ["Private/*"], raising=False)
    write(root, "Private/X.md", "# X\nsecret\n")
    write(root, "Blank.md", "   \n\t\n")
    await indexer.index_vault(user_id=None)
    async with sessionmaker() as session:
        # Pre-034 certified state: no chunking version, no digest.
        await session.execute(text(
            "UPDATE notes_metadata SET embedded_content_hash = content_hash, "
            "embedded_chunking_version = NULL, embedded_input_digest = NULL"
        ))
        await session.commit()
    assert await not_current(sessionmaker) == 2
    provider.documents.clear()
    await indexer.embed_vault(user_id=None)
    assert provider.documents == []
    rows = await note_rows(sessionmaker)
    for p in ("Private/X.md", "Blank.md"):
        assert rows[p].embedded_chunking_version == embeddings_service.CHUNKING_VERSION
        assert rows[p].embedded_input_digest == embeddings_service.input_digest([])
    assert await not_current(sessionmaker) == 0
    await indexer.embed_vault(user_id=None)
    assert provider.documents == []


# ══════════════════════════════════════════════════════════════════════════
# V5 — the vector query really runs in REPEATABLE READ, READ ONLY
# ══════════════════════════════════════════════════════════════════════════


async def test_semantic_search_runs_in_its_snapshot_and_refuses_a_weaker_one(
    sessionmaker, world, monkeypatch
):
    root = world["root"]
    write(root, "S.md", "# S\nsnapshot body\n")
    await indexer.index_vault(user_id=None)
    await indexer.embed_vault(user_id=None)
    seen = {}
    real = embeddings_service.rechunk_pending_count

    async def spy(session, uid):
        seen["iso"] = (await session.execute(text(
            "SELECT current_setting('transaction_isolation'), "
            "current_setting('transaction_read_only')"
        ))).one()
        return await real(session, uid)

    monkeypatch.setattr(tools, "rechunk_pending_count", spy)
    await tools.semantic_search_impl("snapshot", limit=5)
    assert tuple(seen["iso"]) == ("repeatable read", "on")

    async with sessionmaker() as session:
        await session.execute(text("SELECT 1"))  # a READ COMMITTED transaction
        with pytest.raises(RuntimeError, match="REPEATABLE READ, READ ONLY"):
            await embeddings_service.semantic_search(session, "snapshot", limit=5)
