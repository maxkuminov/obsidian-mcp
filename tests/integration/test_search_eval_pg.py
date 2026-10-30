"""Real-Postgres gate for the search-eval harness (#356).

What only a real server can show:

* **The export entrypoint.** `make search-eval-export` itself runs, through
  real `psql` and the real recipe (the `-v` quoting, the redirect, the umask,
  the `.tmp` rename and the failure cleanup). Only the transport is swapped:
  `DB_EXEC` is `tests/integration/fixtures/local-db-exec.sh`, which runs psql
  locally against this throwaway server.
* **The usable filter** in Python agrees with `search_analytics.analysable_sql`.
* **The snapshot membership SQL** (D6) on real `content_tsvector` /
  `note_embeddings` rows, and the read-only replay session (25006 on a write).
* **The built-in arms write nothing**, read from `pg_stat_user_tables`.
* **Corpus loss end to end**, for a deleted row and for deleted embeddings.

Skipped unless `PGVECTOR_TEST_ADMIN_URL` names a throwaway Postgres server.
When it is set, `psql` must be on PATH: this module **fails**, never skips,
without it (CI installs `postgresql-client` if the runner lacks it).
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import textwrap
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

import _harness
from src.config import settings
from src.models.db import APIKey, NoteEmbedding, NoteMetadata, OAuthClient, OAuthToken, UsageLog, User
from src.services.search_analytics import analysable_sql
from src.services.usage_stats import PRE_BODY_REFUSAL_BINDS

from scripts.search_eval import EXIT_FAILURE, EXIT_INSUFFICIENT, EXIT_OK
from scripts.search_eval._io import canonical_json, pretty_json
from scripts.search_eval.classify import classify
from scripts.search_eval.commands import write_snapshot
from scripts.search_eval.db import open_readonly
from scripts.search_eval.mining import load_export, mine, passes_usable_filter, query_id
from scripts.search_eval.runner import run_generation
from scripts.search_eval.scoring import score

pytestmark = [_harness.requires_pgvector]

DIM = settings.embedding_dimensions
ROOT = _harness.ROOT
SHIM = "tests/integration/fixtures/local-db-exec.sh"
F0 = {"folder": None, "tags": None, "frontmatter": None}


@pytest.fixture(scope="module", autouse=True)
def _psql_present():
    if shutil.which("psql") is None:
        pytest.fail("PGVECTOR_TEST_ADMIN_URL is set but psql is not on PATH; the export "
                    "gate runs the real `make search-eval-export` (install postgresql-client)")


def arun(coro):
    return asyncio.run(coro)


def _engine(url):
    return create_async_engine(url, poolclass=NullPool)


def _dbname(url):
    return unquote(urlsplit(url).path).lstrip("/")


def _pg_env():
    parts = urlsplit(_harness.PGVECTOR_TEST_ADMIN_URL)
    env = {k: v for k, v in os.environ.items() if not k.startswith("PG")}
    env.update(PGHOST=parts.hostname or "127.0.0.1", PGPORT=str(parts.port or 5432),
               PGPASSWORD=unquote(parts.password or ""))
    return env


def make_export(out_dir: Path, dbname: str, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["make", "-s", "search-eval-export", f"DB_EXEC=bash {SHIM}",
         f"SEARCH_EVAL_DB_NAME={dbname}", f"SEARCH_EVAL_DIR={out_dir}", *extra],
        cwd=ROOT, env=_pg_env(), capture_output=True, text=True, timeout=120,
    )


# --------------------------------------------------------------------------
# Seeding.
# --------------------------------------------------------------------------

NOTES = [
    # (owner, path, title, body, keyword, embeddings, stale)
    ("u1", "Ops/Runbook.md", "Runbook", "deploy runbook steps", True, True, False),
    ("u1", "Misc/Runbook notes.md", "Runbook notes", "runbook drafts", True, True, True),
    ("u1", "Ops/NoVector.md", "No vector", "runbook without vectors", True, False, False),
    ("u1", "Ops/NoTsv.md", "No tsvector", "zebra", False, True, False),
    ("u2", "Ops/Other runbook.md", "Other runbook", "runbook for user two", True, True, False),
    (None, "Shared/Ownerless.md", "Ownerless", "runbook ownerless", True, False, False),
]


async def seed_notes(url) -> dict:
    engine = _engine(url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with maker() as s:
        users = {"u1": User(username="se-u1", password_hash="x"),
                 "u2": User(username="se-u2", password_hash="x")}
        s.add_all(users.values())
        await s.flush()
        for owner, path, title, body, kw, emb, stale in NOTES:
            uid = users[owner].id if owner else None
            note = NoteMetadata(user_id=uid, file_path=path, title=title, content_hash="h1",
                                embedded_content_hash="h0" if stale else "h1",
                                modified_at=datetime.now(timezone.utc))
            s.add(note)
            await s.flush()
            if kw:
                await s.execute(text(
                    "UPDATE notes_metadata SET content_tsvector = "
                    "to_tsvector('english', :t || ' ' || :b) WHERE id = :id"),
                    {"t": title, "b": body, "id": note.id})
            if emb:
                s.add(NoteEmbedding(note_id=note.id, chunk_index=0, chunk_text=body,
                                    embedding=[0.01] * DIM))
        await s.commit()
        ids = {k: u.id for k, u in users.items()}
    await engine.dispose()
    return ids


def _row(tool, ago_s, params, *, user_id, key_id=None, token_id=None, actor_ref=None):
    return UsageLog(tool=tool, params=params, user_id=user_id, key_id=key_id,
                    oauth_token_id=token_id, actor_ref=actor_ref, duration_ms=1,
                    response_size=1,
                    created_at=datetime.now(timezone.utc) - timedelta(seconds=ago_s))


async def seed_usage(url, users: dict) -> dict:
    engine = _engine(url)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    now = datetime.now(timezone.utc)
    async with maker() as s:
        key = APIKey(user_id=users["u1"], name="k1", key_hash="a" * 64, key_prefix="omcp_k1",
                     permission="read")
        key2 = APIKey(user_id=users["u2"], name="k2", key_hash="b" * 64, key_prefix="omcp_k2",
                      permission="read")
        client = OAuthClient(user_id=users["u1"], client_id="client-a", client_name="c",
                             token_endpoint_auth_method="none",
                             redirect_uris=["https://example.invalid/cb"])
        s.add_all([key, key2, client])
        await s.flush()
        g1 = OAuthToken(user_id=users["u1"], token_hash="c" * 64, token_type="access",
                        client_id="client-a", scope="read", grant_id="grant-g1",
                        grant_issued_at=now, expires_at=now + timedelta(hours=1))
        g2 = OAuthToken(user_id=users["u1"], token_hash="d" * 64, token_type="access",
                        client_id="client-a", scope="read", grant_id="grant-g2",
                        grant_issued_at=now, expires_at=now + timedelta(hours=1))
        s.add_all([g1, g2])
        await s.flush()
        u1, u2 = users["u1"], users["u2"]
        k = dict(user_id=u1, key_id=key.id, actor_ref="omcp_k1")
        a = dict(user_id=u1, token_id=g1.id, actor_ref="client-a")
        b = dict(user_id=u1, token_id=g2.id, actor_ref="client-a")
        un = dict(user_id=u1, actor_ref="client-a")
        k2 = dict(user_id=u2, key_id=key2.id, actor_ref="omcp_k2")
        q = lambda text_, paths, **x: {"query": text_, "folder": None, "tags": None,  # noqa: E731
                                        "frontmatter": None, "limit": 10, "result_paths": paths,
                                        "result_count": len(paths), **x}
        rows = [
            _row("keyword_search", 600, q("deploy runbook", ["Ops/Runbook.md"]), **k),
            _row("read_note", 560, {"path": "Ops/Runbook.md"}, **k),
            _row("keyword_search", 500, q("taxes", ["Notes/Taxes.md"]), **a),
            _row("keyword_search", 490, q("receipts", ["Notes/Taxes.md"]), **b),
            _row("read_note", 480, {"path": "Notes/Taxes.md"}, **a),
            _row("keyword_search", 400, q("issue tracker", ["Issues/1.md"]), **un),
            _row("read_note", 395, {"path": "Issues/1.md"}, **un),
            _row("keyword_search", 300, q("weekly review", ["Weekly.md"]), **k2),
            _row("read_note", 290, {"path": "Weekly.md"}, **k2),
            # The usable-filter cases.
            _row("keyword_search", 200, q("quota", ["A.md"], over_quota=True), **k),
            _row("keyword_search", 199, q("quota", ["A.md"], over_quota=False), **k),
            _row("keyword_search", 198, q("rl", ["A.md"], error="rate_limited"), **k),
            _row("keyword_search", 197, q("nv", ["A.md"], error="no_vault_assigned"), **k),
            _row("keyword_search", 196, q("nul", ["A.md"], error=None), **k),
            _row("read_note", 195, {"path": "A.md", "error": "not_found",
                                    "body_outcome": "refused"}, **k),
            _row("read_note", 194, {"path": "A.md", "error": "tool_exception"}, **k),
            _row("semantic_search", 193, None, **k),
            # Outside the export: another tool, and a row older than DAYS.
            _row("find_related", 190, {"path": "A.md", "result_paths": []}, **k),
            _row("keyword_search", 100 * 86400, q("ancient", ["A.md"]), **k),
        ]
        s.add_all(rows)
        await s.commit()
    await engine.dispose()
    return {"g1": "grant-g1", "g2": "grant-g2"}


async def usage_rows(url) -> list:
    engine = _engine(url)
    async with engine.connect() as c:
        rows = (await c.execute(text(
            "SELECT id, tool, params::text, created_at, key_id, oauth_token_id, user_id, "
            "actor_ref FROM usage_logs ORDER BY id"))).fetchall()
    await engine.dispose()
    return [tuple(r) for r in rows]


@pytest.fixture(scope="module")
def main_db():
    for url in _harness.throwaway_database("search_eval", DIM):
        users = arun(seed_notes(url))
        arun(seed_usage(url, users))
        yield url, users


@pytest.fixture
def loss_db():
    yield from _harness.throwaway_database("search_eval_loss", DIM)


# --------------------------------------------------------------------------
# The export gate.
# --------------------------------------------------------------------------


def test_make_search_eval_export_against_a_real_server(main_db, tmp_path):
    url, users = main_db
    before = arun(usage_rows(url))
    out = tmp_path / "se"
    res = make_export(out, _dbname(url))
    assert res.returncode == 0, res.stdout + res.stderr
    assert (out.stat().st_mode & 0o777) == 0o700
    f = out / "usage-export.jsonl"
    assert (f.stat().st_mode & 0o777) == 0o600
    assert not list(out.glob("*.tmp")), "no temp file left behind"
    lines = f.read_text().splitlines()
    rows = [json.loads(line) for line in lines]
    assert len(rows) == 17, "find_related and the 100-day-old row are outside the export"
    assert {r["tool"] for r in rows} == {"keyword_search", "semantic_search", "read_note"}
    assert all(set(r) == {"id", "created_at", "tool", "user_id", "actor_kind", "actor_ref",
                          "key_id", "oauth_token_id", "grant_id", "user_filter",
                          "deployment_users", "params"} for r in rows)
    assert {r["grant_id"] for r in rows if r["oauth_token_id"]} == {"grant-g1", "grant-g2"}
    assert all(r["user_filter"] is None and r["deployment_users"] == 2 for r in rows)
    assert all("body_outcome" not in (r["params"] or {}) for r in rows), "params subset only"
    loaded = load_export(f)
    assert loaded.malformed == 0
    res_mine = mine(loaded.rows)
    got = {(q["text"], q["user_id"]): sorted(res_mine.qrels[q["qid"]]) for q in res_mine.queries}
    assert got == {("deploy runbook", users["u1"]): ["Ops/Runbook.md"],
                   ("taxes", users["u1"]): ["Notes/Taxes.md"],
                   ("weekly review", users["u2"]): ["Weekly.md"]}
    assert res_mine.report["excluded"]["unresolved_principal"] == 2
    assert res_mine.report["provenance"]["users"] == "multi"
    assert arun(usage_rows(url)) == before, "the export wrote nothing"


def test_a_user_filtered_export_is_single_user(main_db, tmp_path):
    url, users = main_db
    res = make_export(tmp_path / "se", _dbname(url), f"USER_ID={users['u1']}", "DAYS=7")
    assert res.returncode == 0, res.stdout + res.stderr
    loaded = load_export(tmp_path / "se" / "usage-export.jsonl")
    assert {r["user_id"] for r in loaded.rows} == {users["u1"]}
    assert {r["user_filter"] for r in loaded.rows} == {str(users["u1"])}
    assert mine(loaded.rows).report["provenance"] == {"users": 1, "basis": "user_filter"}


def test_a_failed_export_leaves_nothing_behind(main_db, tmp_path):
    out = tmp_path / "se"
    out.mkdir()
    sentinel = out / "usage-export.jsonl"
    sentinel.write_bytes(b"sentinel\n")
    res = make_export(out, "does_not_exist")
    assert res.returncode != 0
    assert not list(out.glob("*.tmp")), "no temp file left behind"
    assert sentinel.read_bytes() == b"sentinel\n"


def test_the_python_usable_filter_agrees_with_analysable_sql(main_db):
    url, _ = main_db

    async def go():
        engine = _engine(url)
        async with engine.connect() as c:
            rows = (await c.execute(text(
                f"SELECT ul.id, ul.params, {analysable_sql()} AS ok FROM usage_logs ul"),
                PRE_BODY_REFUSAL_BINDS)).fetchall()
        await engine.dispose()
        return rows

    rows = arun(go())
    assert len(rows) >= 19
    assert {r.ok for r in rows} == {True, False}
    for r in rows:
        assert passes_usable_filter(r.params) is r.ok, (r.id, r.params)


# --------------------------------------------------------------------------
# Snapshot membership and the read-only session.
# --------------------------------------------------------------------------


def test_the_snapshot_flags_membership_exactly_as_d6_defines_it(main_db, tmp_path):
    url, users = main_db
    meta = arun(write_snapshot(data=tmp_path / "d", database_url=url))
    snap_file = tmp_path / "d" / "corpus-snapshot.json"
    snap = json.loads(snap_file.read_text())
    u1, u2 = str(users["u1"]), str(users["u2"])
    assert set(snap) == {u1, u2, "null"}
    flags = {(o, p): (e["keyword"], e["semantic"], e["stale_vector"])
             for o, paths in snap.items() for p, e in paths.items()}
    assert flags == {
        (u1, "Ops/Runbook.md"): (True, True, False),
        (u1, "Misc/Runbook notes.md"): (True, True, True),  # stale stays semantic
        (u1, "Ops/NoVector.md"): (True, False, False),
        (u1, "Ops/NoTsv.md"): (False, True, False),
        (u2, "Ops/Other runbook.md"): (True, True, False),
        ("null", "Shared/Ownerless.md"): (True, False, False),
    }
    assert all(e["metadata"] for paths in snap.values() for e in paths.values())
    assert meta["sha256"] == _harness_sha(snap_file)
    assert meta["database"] == _dbname(url) and meta["rows"] == 6
    assert "test" not in json.dumps(meta).replace(_dbname(url), ""), "no password"
    assert (snap_file.stat().st_mode & 0o777) == 0o600
    with pytest.raises(Exception, match="--force"):
        arun(write_snapshot(data=tmp_path / "d", database_url=url))


def _harness_sha(path):
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_the_replay_session_is_read_only(main_db):
    url, _ = main_db

    async def go():
        engine, maker = await open_readonly(url)
        try:
            async with maker() as s:
                assert (await s.execute(text("SHOW transaction_read_only"))).scalar() == "on"
            async with maker() as s:
                with pytest.raises(DBAPIError) as exc:
                    await s.execute(text(
                        "INSERT INTO users (username, password_hash) VALUES ('se-x', 'x')"))
                err = exc.value.orig
                state = getattr(err, "sqlstate", None) or getattr(err.__cause__, "sqlstate", None)
                assert state == "25006"
        finally:
            await engine.dispose()
        e2 = _engine(url)
        async with e2.connect() as c:
            n = (await c.execute(text("SELECT count(*) FROM users WHERE username = 'se-x'"))).scalar()
        await e2.dispose()
        return n

    assert arun(go()) == 0


# --------------------------------------------------------------------------
# Replay.
# --------------------------------------------------------------------------


def write_data(d: Path, queries: list[dict]) -> dict:
    d.mkdir(parents=True, exist_ok=True)
    rows, qrels = [], {}
    for q in queries:
        filters = {**F0, "folder": q.get("folder")}
        qid = query_id(q["user_id"], q["text"], filters)
        rows.append({"qid": qid, "user_id": q["user_id"], "text": q["text"], **filters,
                     "class": classify(q["text"]), "sources": ["mined"],
                     "source_tools": ["keyword_search"], "pool": sorted(q["relevant"]),
                     "n_searches": 1})
        qrels[qid] = {p: 1 for p in q["relevant"]}
    (d / "queries.jsonl").write_text("".join(canonical_json(r) + "\n" for r in rows))
    (d / "qrels.json").write_text(canonical_json(qrels))
    (d / "mine-report.json").write_text(pretty_json(
        {"window_seconds": 120, "provenance": {"users": 1, "basis": "user_filter"}}))
    return {(r["text"], r["folder"]): r["qid"] for r in rows}


async def table_writes(url) -> dict:
    engine = _engine(url)
    async with engine.connect() as c:
        await c.execute(text("SELECT pg_stat_clear_snapshot()"))
        rows = (await c.execute(text(
            "SELECT relname, n_tup_ins + n_tup_upd + n_tup_del AS w FROM pg_stat_user_tables"
        ))).fetchall()
    await engine.dispose()
    return {r.relname: r.w for r in rows}


def _settled_writes(url):
    # Other backends flush their counters when they exit; give them a moment.
    time.sleep(1.5)
    return arun(table_writes(url))


def test_the_keyword_arm_is_wired_and_writes_nothing(main_db, tmp_path, monkeypatch):
    url, users = main_db
    monkeypatch.delenv("DATABASE_URL", raising=False)
    d = tmp_path / "d"
    arun(write_snapshot(data=d, database_url=url))
    qids = write_data(d, [
        {"user_id": users["u1"], "text": "runbook", "relevant": ["Ops/Runbook.md"]},
        {"user_id": users["u1"], "text": "runbook", "folder": "Ops",
         "relevant": ["Ops/Runbook.md"]},
    ])

    # Sanity: a write from another backend does show up in the counters.
    async def touch():
        e = _engine(url)
        async with e.begin() as c:
            await c.execute(text("UPDATE notes_metadata SET title = title WHERE file_path = 'Ops/NoTsv.md'"))
        await e.dispose()

    first = _settled_writes(url)
    arun(touch())
    second = _settled_writes(url)
    assert second["notes_metadata"] > first["notes_metadata"], "the stats probe is not live"

    out = arun(run_generation(d, ["keyword"], generation="K1", database_url=url, depth=20))
    assert out.exit_code == EXIT_OK, out.manifest
    third = _settled_writes(url)
    assert third == second, "the keyword arm wrote something"

    staged = json.loads((d / "runs" / out.manifest["files"]["keyword"]["path"]).read_text())
    runs = staged["runs"]
    plain = runs[qids[("runbook", None)]]
    assert set(plain) == {"Ops/Runbook.md", "Misc/Runbook notes.md", "Ops/NoVector.md"}, \
        "owner-scoped: user 2's and the ownerless note are absent"
    assert set(runs[qids[("runbook", "Ops")]]) == {"Ops/Runbook.md", "Ops/NoVector.md"}, \
        "the folder filter is passed through"
    out2 = arun(run_generation(d, ["keyword"], generation="K2", database_url=url, depth=1))
    staged2 = json.loads((d / "runs" / out2.manifest["files"]["keyword"]["path"]).read_text())
    assert all(len(v) <= 1 for v in staged2["runs"].values())
    res = score(d, ["keyword"], min_queries=1)
    assert res.exit_code == EXIT_OK and res.report["arms"][0]["corpus_loss"] == 0


WRITING_ARM = textwrap.dedent('''
    from sqlalchemy import text

    def make(ctx):
        async def arm(q, depth):
            async with ctx.session_factory() as s:
                await s.execute(text(
                    "INSERT INTO users (username, password_hash) VALUES ('se-arm', 'x')"))
                await s.commit()
            return []
        return arm

    make.membership = "metadata"
''')


def test_an_accidental_write_through_the_supplied_session_fails_the_run(main_db, tmp_path,
                                                                        monkeypatch):
    url, users = main_db
    (tmp_path / "se_writing_arm.py").write_text(WRITING_ARM)
    monkeypatch.syspath_prepend(str(tmp_path))
    d = tmp_path / "d"
    arun(write_snapshot(data=d, database_url=url))
    write_data(d, [{"user_id": users["u1"], "text": "runbook", "relevant": ["Ops/Runbook.md"]}])
    out = arun(run_generation(d, ["se_writing_arm:make"], generation="W1", database_url=url))
    assert out.exit_code == EXIT_FAILURE
    assert out.manifest["status"] == "failed" and "files" not in out.manifest

    async def count():
        e = _engine(url)
        async with e.connect() as c:
            n = (await c.execute(text("SELECT count(*) FROM users WHERE username = 'se-arm'"))).scalar()
        await e.dispose()
        return n

    assert arun(count()) == 0


# --------------------------------------------------------------------------
# Corpus loss end to end.
# --------------------------------------------------------------------------


async def _exec(url, sql, **params):
    e = _engine(url)
    async with e.begin() as c:
        await c.execute(text(sql), params)
    await e.dispose()


def test_a_deleted_row_is_corpus_loss(loss_db, tmp_path):
    users = arun(seed_notes(loss_db))
    d = tmp_path / "d"
    arun(write_snapshot(data=d, database_url=loss_db))
    qids = write_data(d, [{"user_id": users["u1"], "text": "runbook",
                           "relevant": ["Ops/Runbook.md", "Misc/Runbook notes.md"]}])
    arun(_exec(loss_db, "DELETE FROM notes_metadata WHERE file_path = 'Misc/Runbook notes.md'"))
    out = arun(run_generation(d, ["keyword"], generation="L1", database_url=loss_db))
    assert out.exit_code == EXIT_OK, out.manifest
    staged = json.loads((d / "runs" / out.manifest["files"]["keyword"]["path"]).read_text())
    assert staged["meta"]["corpus_loss"] == {qids[("runbook", None)]: ["Misc/Runbook notes.md"]}
    res = score(d, ["keyword"], min_queries=1)
    assert res.exit_code == EXIT_INSUFFICIENT and res.report["verdict"] == "CORPUS_LOSS"
    assert res.report["arms"][0]["metrics"]["overall"]["recall@10"] == 0.5


def test_deleted_embeddings_are_loss_for_the_semantic_arm_only(loss_db, tmp_path, monkeypatch):
    users = arun(seed_notes(loss_db))

    async def fake_get_embedding(text_: str):
        return [0.01] * DIM

    monkeypatch.setattr("src.services.embeddings.get_embedding", fake_get_embedding)
    d = tmp_path / "d"
    arun(write_snapshot(data=d, database_url=loss_db))
    write_data(d, [{"user_id": users["u1"], "text": "runbook",
                    "relevant": ["Ops/Runbook.md", "Misc/Runbook notes.md"]}])
    arun(_exec(loss_db, "DELETE FROM note_embeddings WHERE note_id = (SELECT id FROM notes_metadata "
                        "WHERE file_path = 'Misc/Runbook notes.md')"))
    out = arun(run_generation(d, ["keyword", "semantic"], generation="L2", database_url=loss_db))
    assert out.exit_code == EXIT_OK, out.manifest
    res = score(d, ["keyword", "semantic"], baseline="keyword", min_queries=1)
    by = {a["arm"]: a for a in res.report["arms"]}
    assert by["keyword"]["corpus_loss"] == 0
    assert by["semantic"]["corpus_loss"] == 1
    assert res.exit_code == EXIT_INSUFFICIENT and res.report["verdict"] == "CORPUS_LOSS"


def test_the_hybrid_arm_replays_the_service_and_writes_nothing(main_db, tmp_path, monkeypatch):
    """#354: `scripts.search_eval_arms.hybrid:make_arm` answers with exactly the
    fused paths `hybrid_search` returns for the same query, owner, filters and
    k, and moves no write counter."""
    from src.services import hybrid

    url, users = main_db
    monkeypatch.delenv("DATABASE_URL", raising=False)

    async def fake_get_embedding(text_: str):
        return [0.01] * DIM

    monkeypatch.setattr("src.services.embeddings.get_embedding", fake_get_embedding)
    d = tmp_path / "d"
    arun(write_snapshot(data=d, database_url=url))
    qids = write_data(d, [
        {"user_id": users["u1"], "text": "runbook", "relevant": ["Ops/Runbook.md"]},
        {"user_id": users["u1"], "text": "runbook", "folder": "Ops",
         "relevant": ["Ops/Runbook.md"]},
    ])
    before = _settled_writes(url)
    spec = "scripts.search_eval_arms.hybrid:make_arm"
    out = arun(run_generation(d, [spec], generation="H1", database_url=url, depth=20,
                              arm_options={"hybrid_k": "20"}))
    assert out.exit_code == EXIT_OK, out.manifest
    assert _settled_writes(url) == before, "the hybrid arm wrote something"
    staged = json.loads((d / "runs" / out.manifest["files"][spec]["path"]).read_text())
    assert staged["meta"]["membership"] == "keyword_or_semantic"
    assert staged["meta"]["options"] == {"hybrid_k": "20"}

    async def service(folder):
        engine = _engine(url)
        maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with maker() as s:
            res = await hybrid.hybrid_search(s, "runbook", limit=20, folder=folder,
                                             user_id=users["u1"], rrf_k=20)
        await engine.dispose()
        return [r.path for r in res.rows]

    assert staged["runs"][qids[("runbook", None)]] == arun(service(None))
    assert staged["runs"][qids[("runbook", "Ops")]] == arun(service("Ops"))
    assert "Ops/Other runbook.md" not in staged["runs"][qids[("runbook", None)]]
