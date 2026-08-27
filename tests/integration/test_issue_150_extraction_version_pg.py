"""The extraction-version re-derivation pass, against a real PostgreSQL — #150.

A fence-grammar change moves no bytes, so `content_hash` cannot see it and the
`content_hash`-gated scan would skip every unchanged note forever. The marker
column is what makes the pass revisit them; the properties worth a database are
all about *which rows it then rewrites and which it leaves alone*:

* a stale marker refreshes links AND tags for every note, whatever its hash;
* embedding invalidation is scoped to notes whose recognised fence spans
  actually moved — a marker bump must not re-embed a 2,500-note vault;
* `content_hash` keeps the true hash throughout, so an external rename landing
  between the migration and the pass is still matched as a move rather than
  delete-and-reinserted (which cascade-deletes the note's vectors);
* the keyword vectors the pass rewrites are real tsvectors, not nulls.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` is set — see `_harness.py`.
"""
import hashlib

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import src.database
from src.config import settings
from src.models.db import NoteEmbedding, NoteLink, NoteMetadata
from src.services import embeddings as embeddings_service
from src.services import indexer
import _harness

pytestmark = [
    _harness.requires_pgvector,
    pytest.mark.asyncio(loop_scope="module"),
]

DIM = int(settings.embedding_dimensions)

# `Spans.md`'s fence spans differ between grammar 0 and grammar 1 (the closer
# is indented three spaces, which the pre-#150 regexes did not recognise), so
# its cleaned embedding text changes and its vectors must be rebuilt.
SPANS_CHANGE = "# S\nprose\n```\nsecret payload\n   ```\ntail\n"
# `Plain.md` has no fence at all, so both grammars recognise nothing: its
# cleaned text is identical and its certification must survive untouched.
PLAIN = "# P\njust prose, and a [[Spans]] link\n"
# `Links.md` carries a link and a tag inside a newly recognised fence: the
# re-derivation must drop both. Its spans differ too, so it is also invalidated.
BURIED = "# L\n   ```\n#buried\n[[Plain]]\n   ```\n#visible see [[Spans]]\n"


def content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@pytest.fixture(scope="module")
def migrated_url():
    yield from _harness.throwaway_database("extraction_version_150", DIM)


@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def sessionmaker(migrated_url):
    engine = create_async_engine(migrated_url, poolclass=None)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest_asyncio.fixture(loop_scope="module")
async def vault(sessionmaker, monkeypatch, tmp_path):
    root = tmp_path / "vault"
    root.mkdir(parents=True)

    monkeypatch.setattr(settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer.settings, "vault_path", str(root), raising=False)
    monkeypatch.setattr(indexer, "async_session", sessionmaker)
    monkeypatch.setattr(src.database, "async_session", sessionmaker)
    monkeypatch.setattr(embeddings_service, "async_session", sessionmaker, raising=False)
    monkeypatch.setattr(indexer, "_is_paused", lambda: False)

    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM note_links"))
        await session.execute(text("DELETE FROM note_embeddings"))
        await session.execute(text("DELETE FROM notes_metadata"))
        # Users too: two tests here create a named owner, and `users.username`
        # is unique, so a survivor from a previous test makes the next one
        # error rather than fail.
        await session.execute(text("DELETE FROM users"))
        await session.commit()

    yield root


async def seed_pre_150(sessionmaker, root, notes):
    """Rows as the pre-#150 indexer left them: marker 0, certified vectors.

    Tags and links are seeded to what the OLD grammar extracted — the whole
    point is that the pass has to notice they are wrong.
    """
    ids = {}
    async with sessionmaker() as session:
        for rel, body, tags in notes:
            (root / rel).write_text(body, encoding="utf-8")
            h = content_hash(body)
            note = NoteMetadata(
                user_id=None,
                file_path=rel,
                title=rel.removesuffix(".md"),
                tags=tags,
                frontmatter={},
                content_hash=h,
                embedded_content_hash=h,
                extraction_version=0,
            )
            session.add(note)
            await session.flush()
            ids[rel] = note.id
            session.add(
                NoteEmbedding(
                    note_id=note.id,
                    chunk_index=0,
                    chunk_text="stale chunk",
                    embedding=[1.0] + [0.0] * (DIM - 1),
                )
            )
        await session.commit()
    return ids


async def rows(sessionmaker):
    async with sessionmaker() as session:
        result = await session.execute(
            select(
                NoteMetadata.id,
                NoteMetadata.file_path,
                NoteMetadata.content_hash,
                NoteMetadata.embedded_content_hash,
                NoteMetadata.extraction_version,
                NoteMetadata.tags,
            ).order_by(NoteMetadata.file_path)
        )
        return {r.file_path: r for r in result.all()}


async def link_targets(sessionmaker, note_id):
    async with sessionmaker() as session:
        result = await session.execute(
            select(NoteLink.target_path).where(NoteLink.source_note_id == note_id)
        )
        return sorted(r[0] for r in result.all())


CORPUS = [
    ("Spans.md", SPANS_CHANGE, ["stale-tag"]),
    ("Plain.md", PLAIN, ["P"]),
    ("Buried.md", BURIED, ["buried", "visible"]),
]


async def test_a_stale_marker_re_derives_links_and_tags_for_every_note(
    sessionmaker, vault
):
    ids = await seed_pre_150(sessionmaker, vault, CORPUS)

    await indexer.index_vault(user_id=None)

    after = await rows(sessionmaker)
    assert {p: r.extraction_version for p, r in after.items()} == {
        "Spans.md": indexer.CURRENT_EXTRACTION_VERSION,
        "Plain.md": indexer.CURRENT_EXTRACTION_VERSION,
        "Buried.md": indexer.CURRENT_EXTRACTION_VERSION,
    }
    # Row identity survived — no delete-and-reinsert.
    assert {p: r.id for p, r in after.items()} == ids

    # Tags were re-extracted under the new grammar: the one inside the
    # newly recognised fence is gone, the one outside it stayed.
    assert after["Buried.md"].tags == ["visible"]
    assert after["Spans.md"].tags == []

    # So were the links: `[[Plain]]` sat inside the fence and is gone;
    # `[[Spans]]` sat outside it and survived.
    assert await link_targets(sessionmaker, ids["Buried.md"]) == ["Spans"]
    assert await link_targets(sessionmaker, ids["Plain.md"]) == ["Spans"]


async def test_embedding_invalidation_is_scoped_to_span_diff_notes(
    sessionmaker, vault
):
    """The cost control, and the assertion the whole mechanism exists for: a
    marker bump must not re-embed a vault. Only the two notes whose recognised
    spans moved lose their certification."""
    await seed_pre_150(sessionmaker, vault, CORPUS)
    before = await rows(sessionmaker)

    await indexer.index_vault(user_id=None)

    after = await rows(sessionmaker)
    # `Plain.md` has no fence under either grammar: identical spans, so its
    # certification is untouched and no later pass selects it.
    assert after["Plain.md"].embedded_content_hash == (
        before["Plain.md"].embedded_content_hash
    )
    assert after["Plain.md"].embedded_content_hash == after["Plain.md"].content_hash
    # The two whose spans moved are cleared, so the embed backlog picks them up.
    for path in ("Spans.md", "Buried.md"):
        assert after[path].embedded_content_hash is None, path
        # And `content_hash` is untouched — never nulled, never sentinelled.
        assert after[path].content_hash == before[path].content_hash


async def test_the_embed_pass_rebuilds_exactly_the_invalidated_notes(
    sessionmaker, vault, monkeypatch
):
    """Counted at the provider, because "which notes were re-embedded" is the
    resource question the scoping answers."""
    await seed_pre_150(sessionmaker, vault, CORPUS)
    await indexer.index_vault(user_id=None)

    embedded: list[str] = []

    async def counting_batch(chunks):
        embedded.extend(chunks)
        return [[1.0] + [0.0] * (DIM - 1) for _ in chunks]

    monkeypatch.setattr(embeddings_service, "get_embeddings_batch", counting_batch)

    await indexer.embed_vault(user_id=None)

    joined = "\n".join(embedded)
    assert "tail" in joined  # Spans.md was re-embedded
    assert "visible" in joined  # Buried.md was re-embedded
    assert "just prose" not in joined  # Plain.md was not
    # And the code the new grammar recognises did not reach the provider.
    assert "secret payload" not in joined
    assert "buried" not in joined

    after = await rows(sessionmaker)
    for path in ("Spans.md", "Buried.md", "Plain.md"):
        assert after[path].embedded_content_hash == after[path].content_hash, path


async def test_a_second_pass_is_a_no_op(sessionmaker, vault, monkeypatch):
    """The marker is stamped, so nothing is re-derived and nothing is
    re-invalidated — otherwise every tick would re-embed the vault."""
    await seed_pre_150(sessionmaker, vault, CORPUS)
    await indexer.index_vault(user_id=None)
    async with sessionmaker() as session:
        await session.execute(
            text("UPDATE notes_metadata SET embedded_content_hash = content_hash")
        )
        await session.commit()
    before = await rows(sessionmaker)

    await indexer.index_vault(user_id=None)

    after = await rows(sessionmaker)
    for path in before:
        assert after[path].embedded_content_hash == (
            before[path].embedded_content_hash
        ), path
        assert after[path].extraction_version == before[path].extraction_version


async def test_an_external_rename_in_the_window_keeps_note_identity(
    sessionmaker, vault
):
    """`content_hash` holds the true hash throughout the remediation window, so
    the move detector still pairs the vanished path with the new one and
    updates the row in place. A sentinel there would have made this a
    delete-and-insert, cascade-deleting the note's vectors."""
    ids = await seed_pre_150(sessionmaker, vault, CORPUS)
    buried_id = ids["Buried.md"]

    (vault / "Buried.md").rename(vault / "Renamed.md")
    await indexer.index_vault(user_id=None)

    after = await rows(sessionmaker)
    assert "Buried.md" not in after
    assert after["Renamed.md"].id == buried_id  # same row, id preserved
    assert after["Renamed.md"].content_hash == content_hash(BURIED)
    # The vectors went with the row rather than being cascade-deleted.
    async with sessionmaker() as session:
        vectors = (
            await session.execute(
                select(NoteEmbedding.id).where(NoteEmbedding.note_id == buried_id)
            )
        ).all()
    assert len(vectors) == 1


async def test_a_rename_in_the_window_still_re_derives_in_one_pass(
    sessionmaker, vault
):
    """The id-preserving move branch is the only writer that touches a note
    during the remediation window, so deferring its re-derivation to "the next
    pass" broke the next-pass refresh promise for exactly the notes a rename
    touched. It re-derives and stamps in its own transaction instead."""
    ids = await seed_pre_150(sessionmaker, vault, CORPUS)
    buried_id = ids["Buried.md"]

    (vault / "Buried.md").rename(vault / "Renamed.md")
    await indexer.index_vault(user_id=None)

    after = await rows(sessionmaker)
    row = after["Renamed.md"]
    # ONE pass: the marker is stamped and the tags came out of the current
    # grammar — `#buried` sits inside a newly recognised fence and is gone,
    # `#visible` sits outside it and stayed. The seeded row carried both.
    assert row.extraction_version == indexer.CURRENT_EXTRACTION_VERSION
    assert row.tags == ["visible"]
    # Links likewise, through `moved_new_paths`: `[[Plain]]` was inside the
    # fence, `[[Spans]]` outside it.
    assert await link_targets(sessionmaker, buried_id) == ["Spans"]
    # The path change still invalidates the certification (#127) — unchanged
    # by the marker, and not the grammar rule.
    assert row.embedded_content_hash is None

    # And a second pass changes nothing, so the stamp really did settle it.
    before = after
    await indexer.index_vault(user_id=None)
    again = await rows(sessionmaker)
    assert again["Renamed.md"].tags == before["Renamed.md"].tags
    assert again["Renamed.md"].extraction_version == (
        indexer.CURRENT_EXTRACTION_VERSION
    )


async def test_the_keyword_vectors_are_rewritten_not_nulled(sessionmaker, vault):
    await seed_pre_150(sessionmaker, vault, CORPUS)
    await indexer.index_vault(user_id=None)

    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT file_path, content_tsvector IS NOT NULL AS present, "
                "       length(content_tsvector::text) AS size "
                "FROM notes_metadata ORDER BY file_path"
            )
        )
        for row in result.all():
            assert row.present, f"{row.file_path} has a null keyword vector"
            assert row.size > 0


async def test_a_failed_pass_leaves_no_stamped_marker(sessionmaker, vault, monkeypatch):
    """Retry safety: the marker is written in the same transaction as the state
    it certifies, so a pass that dies part way through commits neither."""
    await seed_pre_150(sessionmaker, vault, CORPUS)

    real_update = indexer._update_links_for_changed

    async def explode(*args, **kwargs):
        await real_update(*args, **kwargs)
        raise RuntimeError("pass died after the link rebuild")

    monkeypatch.setattr(indexer, "_update_links_for_changed", explode)

    with pytest.raises(RuntimeError):
        await indexer.index_vault(user_id=None)

    after = await rows(sessionmaker)
    for path, row in after.items():
        assert row.extraction_version == 0, f"{path} was stamped by a failed pass"
        # And the invalidation did not commit either, so the next pass will
        # make both decisions again from the same inputs.
        assert row.embedded_content_hash == row.content_hash, path

    # The retry completes normally.
    monkeypatch.setattr(indexer, "_update_links_for_changed", real_update)
    await indexer.index_vault(user_id=None)
    after = await rows(sessionmaker)
    assert all(
        r.extraction_version == indexer.CURRENT_EXTRACTION_VERSION
        for r in after.values()
    )


async def test_the_re_derivation_is_owner_scoped(sessionmaker, vault):
    """One user's grammar sweep must never clear another's certification. The
    other owner's rows are not even discoverable from this pass's root."""
    from src.models.db import User

    await seed_pre_150(sessionmaker, vault, CORPUS)
    async with sessionmaker() as session:
        other = User(username="mallory", password_hash="x", vault_path="/v/m")
        session.add(other)
        await session.flush()
        h = content_hash(SPANS_CHANGE)
        session.add(
            NoteMetadata(
                user_id=other.id,
                file_path="Spans.md",  # same relative path, different owner
                title="Spans",
                tags=["theirs"],
                content_hash=h,
                embedded_content_hash=h,
                extraction_version=0,
            )
        )
        await session.commit()

    await indexer.index_vault(user_id=None)

    async with sessionmaker() as session:
        theirs = (
            await session.execute(
                select(
                    NoteMetadata.extraction_version,
                    NoteMetadata.embedded_content_hash,
                    NoteMetadata.tags,
                ).where(NoteMetadata.user_id.is_not(None))
            )
        ).first()
    assert theirs.extraction_version == 0
    assert theirs.embedded_content_hash == content_hash(SPANS_CHANGE)
    assert theirs.tags == ["theirs"]


# ══════════════════════════════════════════════════════════════════════════
# The two production callsites of `extract_tags` hand it the BODY
# ══════════════════════════════════════════════════════════════════════════

# A valid frontmatter block whose YAML scalar is fence-shaped, and a body line
# that would CLOSE it. The discriminating shape: scanned as raw text the two
# openers pair up and `#real` is masked; scanned as the body (which is what
# `extract_tags` takes, because every caller has already partitioned) the
# opener is gone with the block, the body's own `   ``` ` is an unmatched
# indented opener rather than a fence, and `#real` survives.
#
# The unmatched variant cannot stand in for this: there, raw scanning masks
# nothing either, so a callsite that regressed to raw text would go unnoticed.
FM_SCALAR_MATCHED_BY_BODY = "---\nliteral: |\n   ```\n---\n#real\n   ```\ntail\n"


async def test_the_indexer_extracts_a_body_tag_a_frontmatter_scalar_would_have_masked(
    sessionmaker, vault
):
    """`notes_metadata.tags` is what every tag-filtered search reads, and the
    indexer is what writes it. This drives the real pass against a real
    database rather than re-calling the helper: what is pinned is the wiring —
    that the scan loop hands `extract_tags` the body it just parsed."""
    from src.services.vault import extract_tags, parse_frontmatter

    # The premise, checked here too so this test cannot quietly become a
    # tautology if the grammar changes underneath it.
    fm, body = parse_frontmatter(FM_SCALAR_MATCHED_BY_BODY)
    assert "real" in extract_tags(body, fm)
    assert "real" not in extract_tags(FM_SCALAR_MATCHED_BY_BODY, fm)

    (vault / "Scalar.md").write_text(FM_SCALAR_MATCHED_BY_BODY, encoding="utf-8")

    await indexer.index_vault(user_id=None)

    row = (await rows(sessionmaker))["Scalar.md"]
    assert row.tags == ["real"]
    assert row.extraction_version == indexer.CURRENT_EXTRACTION_VERSION


# ══════════════════════════════════════════════════════════════════════════
# The transition-window probe is owner-scoped, against a real database
# ══════════════════════════════════════════════════════════════════════════
#
# The unit tests assert the probe's SQL carries the caller's ownership
# predicate. This asserts the *semantics* that predicate is there for, on real
# rows: one owner's unfinished re-derivation must not refuse another owner's
# rewrite-enabled move. Without it, a single idle account would wedge
# `move_note(rewrite_links=True)` for the whole server until someone noticed.
#
# `_stale_extraction_error` is called directly rather than through
# `move_note_impl`: the property under test is the query's scope, and driving
# the whole tool under a named user would drag in vault-root resolution and
# publication confirmation without testing anything more.


async def _stale_probe(sessionmaker, uid):
    import src.mcp_server.tools as tools

    async with sessionmaker() as session:
        return await tools._stale_extraction_error(session, uid)


@pytest_asyncio.fixture(loop_scope="module")
async def two_owners(sessionmaker, vault):
    """A NULL-owned note and a `mallory`-owned one, both stamped current."""
    from src.models.db import User

    async with sessionmaker() as session:
        mallory = User(username="mallory", password_hash="x", vault_path="/v/m")
        session.add(mallory)
        await session.flush()
        for owner in (None, mallory.id):
            session.add(
                NoteMetadata(
                    user_id=owner,
                    file_path="Shared.md",  # same relative path, different owner
                    title="Shared",
                    content_hash=content_hash(PLAIN),
                    extraction_version=indexer.CURRENT_EXTRACTION_VERSION,
                )
            )
        await session.commit()
        return mallory.id


async def _set_marker(sessionmaker, uid, version):
    owner = "user_id IS NULL" if uid is None else f"user_id = {uid}"
    async with sessionmaker() as session:
        await session.execute(
            text(f"UPDATE notes_metadata SET extraction_version = {version} "
                 f"WHERE {owner}")
        )
        await session.commit()


async def test_neither_owner_is_refused_when_every_row_is_stamped(
    sessionmaker, two_owners
):
    assert await _stale_probe(sessionmaker, None) is None
    assert await _stale_probe(sessionmaker, two_owners) is None


async def test_a_named_users_stale_row_does_not_refuse_the_null_owner(
    sessionmaker, two_owners
):
    """The isolation direction that matters most here: single-user mode
    (`user_id IS NULL`) is the production shape, and a leftover named account
    mid-re-derivation must not stop it moving notes."""
    await _set_marker(sessionmaker, two_owners, 0)

    assert await _stale_probe(sessionmaker, None) is None
    # And the account that IS mid-pass is refused, so the probe is not simply
    # blind.
    err = await _stale_probe(sessionmaker, two_owners)
    assert err is not None and "Shared.md" in err


async def test_the_null_owners_stale_row_does_not_refuse_a_named_user(
    sessionmaker, two_owners
):
    """The mirror direction, so the predicate cannot be a one-sided accident
    (e.g. a bare `IS NULL` that happens to satisfy the case above)."""
    await _set_marker(sessionmaker, None, 0)

    assert await _stale_probe(sessionmaker, two_owners) is None
    err = await _stale_probe(sessionmaker, None)
    assert err is not None and "Shared.md" in err
