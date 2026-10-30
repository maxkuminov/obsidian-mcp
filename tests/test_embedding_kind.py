"""Provider `kind`, `EMBEDDING_QUERY_INSTRUCTION` and the retrieval fingerprint
(#367 provider half; heading-chunks design D7).

- Every call to an embedding entry point under `src/` and `scripts/` passes
  `kind=` (an AST sweep), and the four call sites pass the right one.
- The instruction is applied in one place, to queries only, verbatim.
- It is outside the stored fingerprint and inside the non-stored retrieval
  fingerprint, which is logged once at startup with the digest only.
"""
import ast
import json
import logging
import pathlib
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from src.config import Settings, settings
from src.services import embeddings, index_state
from src.services.index_state import (
    FingerprintStatus,
    compare_fingerprint,
    embedding_fingerprint,
    retrieval_fingerprint,
)

ROOT = pathlib.Path(__file__).resolve().parent.parent
ENTRY_POINTS = {"get_embedding", "get_embeddings_batch", "embed_one", "embed_batch"}


def _calls_without_kind():
    missing = []
    for base in ("src", "scripts"):
        for path in sorted((ROOT / base).rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = (
                    func.attr if isinstance(func, ast.Attribute)
                    else func.id if isinstance(func, ast.Name) else None
                )
                if name not in ENTRY_POINTS:
                    continue
                if not any(k.arg == "kind" for k in node.keywords):
                    missing.append(f"{path.relative_to(ROOT)}:{node.lineno} {name}")
    return missing


def test_every_embedding_call_under_src_and_scripts_passes_kind():
    assert _calls_without_kind() == []


def test_the_sweep_catches_a_call_without_kind(tmp_path, monkeypatch):
    tree = ast.parse("async def f():\n    await get_embedding('q')\n")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    assert not any(k.arg == "kind" for k in call.keywords)


def _recording_provider(monkeypatch):
    inputs: list = []

    class _P:
        async def embed_one(self, text, *, kind):
            inputs.append((kind, text))
            return [0.0]

        async def embed_batch(self, texts, *, kind):
            inputs.extend((kind, t) for t in texts)
            return [[0.0] for _ in texts]

    monkeypatch.setattr(embeddings, "get_provider", lambda: _P())
    return inputs


async def test_the_default_query_input_is_the_query(monkeypatch):
    monkeypatch.setattr(settings, "embedding_query_instruction", "")
    inputs = _recording_provider(monkeypatch)
    await embeddings.get_embedding("find this", kind="query")
    assert inputs == [("query", "find this")]


async def test_an_instructed_query_is_instruction_plus_query(monkeypatch):
    monkeypatch.setattr(
        settings, "embedding_query_instruction", "Instruct: find notes\nQuery: "
    )
    inputs = _recording_provider(monkeypatch)
    await embeddings.get_embedding("find this", kind="query")
    await embeddings.get_embeddings_batch(["a", "b"], kind="query")
    assert inputs == [
        ("query", "Instruct: find notes\nQuery: find this"),
        ("query", "Instruct: find notes\nQuery: a"),
        ("query", "Instruct: find notes\nQuery: b"),
    ]


async def test_documents_never_carry_the_instruction(monkeypatch):
    monkeypatch.setattr(settings, "embedding_query_instruction", "INSTR ")
    inputs = _recording_provider(monkeypatch)
    await embeddings.get_embeddings_batch(["doc one", "doc two"], kind="document")
    assert inputs == [("document", "doc one"), ("document", "doc two")]


async def test_an_unknown_kind_is_refused(monkeypatch):
    _recording_provider(monkeypatch)
    with pytest.raises(ValueError):
        await embeddings.get_embedding("x", kind="passage")


async def test_embed_note_sends_documents_and_semantic_search_sends_a_query(monkeypatch):
    """One `embed_note` and one `semantic_search`, the instruction set: the
    chunks go out as documents without it, the query as a query with it."""
    monkeypatch.setattr(settings, "embedding_query_instruction", "INSTR ")
    inputs = _recording_provider(monkeypatch)

    class _Session:
        def __init__(self):
            self.added = []

        async def execute(self, *_a, **_k):
            return SimpleNamespace(fetchall=lambda: [], rowcount=1, scalar=lambda: None)

        async def flush(self):
            pass

        def add(self, row):
            self.added.append(row)

    note = SimpleNamespace(
        id=1, file_path="n.md", title="Note", tags=["t"],
        content_hash="h", embedded_content_hash=None,
    )
    await embeddings.embed_note(_Session(), note, "# Head\nbody text\n")
    await embeddings.semantic_search(_Session(), "the query", limit=3)
    kinds = [k for k, _ in inputs]
    assert kinds == ["document", "query"]
    assert inputs[0][1] == "Note > Head\ntags: t\n\nbody text\n"
    assert "INSTR" not in inputs[0][1]
    assert inputs[1][1] == "INSTR the query"


async def test_the_prewarm_and_the_startup_warm_up_embed_queries(monkeypatch):
    from src import main
    from src.services import indexer

    inputs = _recording_provider(monkeypatch)
    monkeypatch.setattr(settings, "embedding_provider", "ollama")
    await main._warm_embedding_model()
    assert inputs and inputs[-1][0] == "query"

    calls = []

    async def fake_get_embedding(text, *, kind):
        calls.append(kind)
        return [0.0]

    monkeypatch.setattr(embeddings, "get_embedding", fake_get_embedding)

    async def no_index(_session):
        return False

    monkeypatch.setattr(indexer, "_hnsw_index_exists", no_index)

    class _S:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(indexer, "async_session", lambda: _S())
    await indexer._prewarm_once()
    assert calls == ["query"]


# ── the setting ─────────────────────────────────────────────────────────────


def _settings(**env):
    base = dict(
        secret_key="x" * 64,
        embedding_allow_plaintext=True,
    )
    base.update(env)
    return Settings(**base)


def test_the_instruction_defaults_to_empty():
    assert Settings.model_fields["embedding_query_instruction"].default == ""


def test_a_multi_line_instruction_is_accepted():
    s = _settings(embedding_query_instruction="Instruct: x\n\tQuery: ")
    assert s.embedding_query_instruction == "Instruct: x\n\tQuery: "


def test_an_instruction_over_1000_characters_is_refused():
    with pytest.raises(ValidationError):
        _settings(embedding_query_instruction="x" * 1001)


@pytest.mark.parametrize("bad", ["a\x00b", "a\rb", "a\x1bb"], ids=["nul", "cr", "esc"])
def test_a_control_character_is_refused_naming_the_setting(bad):
    with pytest.raises(ValidationError) as exc:
        _settings(embedding_query_instruction=bad)
    assert "EMBEDDING_QUERY_INSTRUCTION" in str(exc.value)


# ── fingerprints ───────────────────────────────────────────────────────────


def test_the_stored_fingerprint_ignores_the_instruction(monkeypatch):
    monkeypatch.setattr(settings, "embedding_query_instruction", "")
    plain = embedding_fingerprint()
    monkeypatch.setattr(settings, "embedding_query_instruction", "Instruct: q\nQuery: ")
    assert embedding_fingerprint() == plain
    verdict = compare_fingerprint(plain, embedding_fingerprint())
    assert verdict.status is FingerprintStatus.MATCH


def test_the_retrieval_fingerprint_tells_the_instruction_apart(monkeypatch):
    monkeypatch.setattr(settings, "embedding_query_instruction", "")
    plain = json.loads(retrieval_fingerprint())
    monkeypatch.setattr(settings, "embedding_query_instruction", "Instruct: q\nQuery: ")
    instructed = json.loads(retrieval_fingerprint())
    assert plain != instructed
    assert plain["query_instruction_sha256"] is None
    assert len(instructed["query_instruction_sha256"]) == 16
    assert plain["chunking_version"] == embeddings.CHUNKING_VERSION
    stored = json.loads(embedding_fingerprint())
    for key, value in stored.items():
        assert plain[key] == value == instructed[key]


def test_the_retrieval_fingerprint_is_never_stored():
    import inspect

    source = inspect.getsource(index_state)
    assert "retrieval_fingerprint" not in source.split("def retrieval_fingerprint")[0]
    assert not any(
        name.startswith("KEY_") and "retrieval" in str(getattr(index_state, name))
        for name in dir(index_state)
    )


def test_startup_logs_the_digest_and_never_the_text(monkeypatch, caplog):
    from src import main

    secret = "Instruct: very-specific-secret-task\nQuery: "
    monkeypatch.setattr(settings, "embedding_query_instruction", secret)
    with caplog.at_level(logging.INFO):
        main._log_retrieval_fingerprint()
    lines = [r.getMessage() for r in caplog.records if "Embedding retrieval:" in r.getMessage()]
    assert len(lines) == 1
    assert "very-specific-secret-task" not in lines[0]
    assert json.loads(retrieval_fingerprint())["query_instruction_sha256"] in lines[0]
