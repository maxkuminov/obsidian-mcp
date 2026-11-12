"""`TEI_API_KEY` (#426): the bearer token, where it goes, where it never goes.

The two properties written first: a sentinel value never surfaces anywhere the
server writes or answers, and with the key unset every request to `TEI_URL`
is the request it was before the setting existed.
"""

from __future__ import annotations

import ast
import asyncio
import logging
import pathlib

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr, ValidationError

from _tei_stubs import INFO, URL, answer_info, answer_per_input, row, tei  # noqa: F401
from src import main
from src.config import Settings, settings
from src.control_panel import routes as panel_routes
from src.database import get_session
from src.services import embeddings, index_state
from src.services import transport_security as ts
from src.services.embeddings import (
    EmbedNoteFailure,
    OllamaProvider,
    OpenAIProvider,
    TeiProvider,
    verify_tei_server,
)
from tests._rerank_support import fake, fake_server  # noqa: F401
from tests.test_issue_309_panel_confirm_empty import _Session, _user

SENTINEL = "omcp-sentinel-7f3a9c"
BEARER = f"Bearer {SENTINEL}"
ROOT = pathlib.Path(__file__).resolve().parent.parent

#: Each request's header list on `aafb9f0`, before the setting existed,
#: captured from the same mocks (design D1). `user-agent` follows httpx.
_UA = f"python-httpx/{httpx.__version__}"
_BASE = [
    ("host", "tei"),
    ("accept", "*/*"),
    ("accept-encoding", "gzip, deflate"),
    ("connection", "keep-alive"),
    ("user-agent", _UA),
]
BEFORE = {
    "info": _BASE,
    "embed": [*_BASE, ("content-length", "36"), ("content-type", "application/json")],
    "health": _BASE,
}


@pytest.fixture(autouse=True)
def _fresh_auth_log(monkeypatch):
    monkeypatch.setattr(embeddings, "_TEI_AUTH_LOG", embeddings._TeiAuthLog())


@pytest.fixture
def keyed(tei, monkeypatch):
    monkeypatch.setattr(settings, "tei_api_key", SecretStr(SENTINEL))
    return settings


@pytest.fixture
def unkeyed(tei, monkeypatch):
    monkeypatch.setattr(settings, "tei_api_key", None)
    return settings


@pytest.fixture
def slept(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _sleep)


def _headers(route, i=0) -> list[tuple[str, str]]:
    return list(route.calls[i].request.headers.multi_items())


def _make(**kw) -> Settings:
    kw.setdefault("secret_key", "a-real-test-key")
    kw.setdefault("embedding_provider", "tei")
    kw.setdefault("tei_url", "https://tei.internal.example")
    return Settings(_env_file=None, **kw)


# ── The panel, offline ──────────────────────────────────────────────────────


@pytest.fixture
def panel(monkeypatch):
    """`/admin/settings` as an admin over a fake session, its TEI probe going
    through the **real** factory with a mock transport swapped in."""
    seen: list[httpx.Request] = []
    answer = {"response": Response(200, text="ok")}

    def _handler(request: httpx.Request) -> Response:
        seen.append(request)
        return answer["response"]

    real = panel_routes.embedding_http_client

    def _factory(timeout):
        client = real(timeout)
        client._transport = httpx.MockTransport(_handler)
        return client

    monkeypatch.setattr(panel_routes, "embedding_http_client", _factory)
    monkeypatch.setattr(settings, "embedding_provider", "tei")
    monkeypatch.setattr(settings, "multi_user_mode", False)
    admin = _user(1, admin=True)
    overrides = dict(main.app.dependency_overrides)

    async def _get_session():
        yield _Session([admin], counts={None: 5, 1: 5}, strict=False)

    async def _panel_user():
        return admin

    main.app.dependency_overrides[get_session] = _get_session
    main.app.dependency_overrides[panel_routes.require_user_panel] = _panel_user
    client = TestClient(main.app, base_url="https://localhost")

    def load(response: Response | None = None) -> str:
        if response is not None:
            answer["response"] = response
        return client.get("/admin/settings").text

    load.seen = seen
    yield load
    main.app.dependency_overrides.clear()
    main.app.dependency_overrides.update(overrides)


# ══════════════════════════════════════════════════════════════════════════
# Written first: the sentinel never surfaces, and unset changes nothing
# ══════════════════════════════════════════════════════════════════════════


#: The three unset spellings, each taken through settings construction so the
#: value the provider sees is the one an operator's environment produces.
_UNSET = [None, "", "   "]


@pytest.mark.parametrize("value", _UNSET, ids=repr)
async def test_unset_requests_are_byte_identical(tei, monkeypatch, value):
    monkeypatch.setattr(settings, "tei_api_key", _make(tei_api_key=value).tei_api_key)
    with respx.mock(base_url=URL) as mock:
        info = answer_info(mock)
        embed = mock.post("/embed").mock(side_effect=answer_per_input)
        await verify_tei_server()
        await TeiProvider().embed_one("hello", kind="query")
    assert _headers(info) == BEFORE["info"]
    assert _headers(embed) == BEFORE["embed"]


@pytest.mark.parametrize("value", _UNSET, ids=repr)
def test_unset_panel_probe_is_byte_identical(tei, panel, monkeypatch, value):
    monkeypatch.setattr(settings, "tei_api_key", _make(tei_api_key=value).tei_api_key)
    panel()
    (request,) = panel.seen
    assert request.url == httpx.URL(f"{URL}/health")
    assert list(request.headers.multi_items()) == BEFORE["health"]


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", "\n", " \t\r\n ", "\f", "\v", " ", "  \f\v\t "],
    ids=repr,
)
def test_blank_means_unset(value):
    """Any whitespace-only value, form feed, vertical tab and U+00A0 (the
    no-break space) included."""
    assert _make(tei_api_key=value).tei_api_key is None


async def test_the_sentinel_never_surfaces(keyed, slept, panel, monkeypatch, caplog):
    """Design §"Secrecy test". Everything the server writes or answers along
    the way is collected, and the sentinel must be in none of it — while the
    mock TEI did receive it (the positive control)."""
    caplog.set_level(logging.DEBUG)
    texts: list[str] = []
    received: list[str] = []

    def _seen(route):
        received.extend(c.request.headers.get("authorization", "") for c in route.calls)

    # Startup: the transport line and the plaintext-key warning.
    monkeypatch.setattr(settings, "embedding_allow_plaintext", True)
    ts.log_embedding_transport()

    # The identity check under every verdict.
    for answer in (
        Response(200, json=INFO),
        Response(200, json={**INFO, "model_id": "other/model"}),
        Response(401, json={"error": "unauthorized"}),
        Response(403, text="forbidden"),
    ):
        embeddings._TEI_VERIFICATION.verified = False
        with respx.mock(base_url=URL) as mock:
            info = mock.get("/info").mock(return_value=answer)
            result = await verify_tei_server()
        _seen(info)
        texts.append(result.reason)

    # `/embed` under every outcome, as `embed_note` records it.
    embeddings._TEI_VERIFICATION.verified = True
    for answers in (
        [Response(200, json=[row()])],
        [Response(401, text=f"echo {SENTINEL}")],
        [Response(403)],
        [Response(503), Response(503), Response(503)],
    ):
        with respx.mock(base_url=URL) as mock:
            embed = mock.post("/embed")
            embed.side_effect = answers
            try:
                await TeiProvider().embed_batch(["x"], kind="document")
            except Exception as exc:  # noqa: BLE001 - every failure is collected
                texts.extend([str(exc), repr(exc)])
                texts.append(EmbedNoteFailure.capture(exc, requested=1).message)
        _seen(embed)

    # The panel settings page, the app's /health, both fingerprints.
    texts.append(panel())
    received.extend(r.headers.get("authorization", "") for r in panel.seen)
    texts.append(TestClient(main.app).get("/health").text)
    texts.append(index_state.embedding_fingerprint())
    texts.append(index_state.retrieval_fingerprint())

    # Every log record: message, raw args, and any formatted exception.
    formatter = logging.Formatter()
    for record in caplog.records:
        texts.append(record.getMessage())
        texts.append(repr(record.args))
        if record.exc_info:
            texts.append(formatter.formatException(record.exc_info))

    leaked = [t for t in texts if SENTINEL in t]
    assert leaked == []
    assert received and set(received) == {BEARER}, "the positive control"
    assert any("TEI_API_KEY" in t for t in texts)


def test_the_settings_representation_masks_it():
    s = _make(tei_api_key=SENTINEL)
    assert SENTINEL not in repr(s) and SENTINEL not in str(s)
    assert SENTINEL not in str(s.tei_api_key)
    assert s.tei_api_key.get_secret_value() == SENTINEL


def test_a_validation_error_does_not_echo_it():
    assert Settings.model_config.get("hide_input_in_errors") is True
    with pytest.raises(ValidationError) as info:
        _make(tei_api_key=f"{SENTINEL} x")
    text = str(info.value)
    assert "TEI_API_KEY" in text and SENTINEL not in text


# ══════════════════════════════════════════════════════════════════════════
# The header, its three call sites, and nowhere else
# ══════════════════════════════════════════════════════════════════════════


async def test_info_and_every_embed_slice_carry_it(keyed):
    with respx.mock(base_url=URL) as mock:
        info = answer_info(mock)
        embed = mock.post("/embed").mock(side_effect=answer_per_input)
        await verify_tei_server()
        await TeiProvider().embed_batch([f"t{i}" for i in range(20)], kind="document")
    assert info.calls[0].request.headers["authorization"] == BEARER
    assert embed.call_count == 2
    assert [c.request.headers["authorization"] for c in embed.calls] == [BEARER] * 2
    # Nothing else changed: the header set is today's plus Authorization.
    names = [k for k, _ in _headers(info)]
    assert names == [k for k, _ in BEFORE["info"]] + ["authorization"]


def test_the_panel_probe_carries_it(keyed, panel):
    panel()
    (request,) = panel.seen
    assert request.headers["authorization"] == BEARER


async def test_retries_carry_it(keyed, slept):
    embeddings._TEI_VERIFICATION.verified = True
    with respx.mock(base_url=URL) as mock:
        embed = mock.post("/embed")
        embed.side_effect = [Response(503), Response(200, json=[row()])]
        await TeiProvider().embed_one("x", kind="query")
    assert [c.request.headers["authorization"] for c in embed.calls] == [BEARER] * 2


async def test_surrounding_whitespace_is_stripped(tei, monkeypatch):
    s = _make(tei_api_key="k-123\n")
    monkeypatch.setattr(settings, "tei_api_key", s.tei_api_key)
    with respx.mock(base_url=URL) as mock:
        info = answer_info(mock)
        await verify_tei_server()
    assert info.calls[0].request.headers["authorization"] == "Bearer k-123"


async def test_ollama_and_openai_never_send_it(keyed, monkeypatch):
    monkeypatch.setattr(embeddings, "_OLLAMA_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(embeddings, "_OPENAI_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(settings, "ollama_url", "http://ollama:11434")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.test/v1")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    with respx.mock() as mock:
        ollama = mock.post("http://ollama:11434/api/embed").mock(
            return_value=Response(200, json={"embeddings": [row()]})
        )
        openai = mock.post("https://api.openai.test/v1/embeddings").mock(
            return_value=Response(200, json={"data": [{"index": 0, "embedding": row()}]})
        )
        await OllamaProvider().embed_one("x", kind="query")
        await OpenAIProvider().embed_one("x", kind="query")
    assert "authorization" not in ollama.calls[0].request.headers
    assert openai.calls[0].request.headers["authorization"] == "Bearer sk-test"
    for call in (*ollama.calls, *openai.calls):
        assert SENTINEL not in repr(call.request.headers.multi_items())


async def test_the_reranker_never_sends_it(fake, monkeypatch):
    from src.services import rerank

    monkeypatch.setattr(settings, "tei_api_key", SecretStr(SENTINEL))
    monkeypatch.setattr(settings, "embedding_provider", "tei")
    await rerank.rerank_candidates("q", [("t", "x"), ("u", "y")])
    assert fake.request_headers
    for headers in fake.request_headers:
        assert "authorization" not in headers
        assert SENTINEL not in repr(headers)


def test_the_setting_is_read_in_three_files_only():
    readers = sorted(
        str(p.relative_to(ROOT))
        for p in (ROOT / "src").rglob("*.py")
        if "tei_api_key" in p.read_text()
    )
    assert readers == [
        "src/config.py",
        "src/services/embeddings.py",
        "src/services/transport_security.py",
    ]
    # Only the helper reads the value; only TEI paths call the helper.
    emb = (ROOT / "src/services/embeddings.py").read_text()
    assert emb.count("get_secret_value()") == 1
    assert "_tei_auth_headers" not in (ROOT / "src/services/rerank.py").read_text()
    callers = [
        node
        for node in ast.walk(ast.parse(emb))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "_tei_auth_headers"
    ]
    assert len(callers) == 2, "the /info exchange and TeiProvider._post"
    assert "default_headers" not in (ROOT / "src/services/transport_security.py").read_text()


async def test_a_redirect_does_not_forward_it(keyed, panel):
    elsewhere = "http://elsewhere:9"
    with respx.mock(assert_all_called=False) as mock:
        target = mock.route(host="elsewhere").mock(return_value=Response(200, json=INFO))
        redirect = Response(307, headers={"location": f"{elsewhere}/x"})
        mock.get(f"{URL}/info").mock(return_value=redirect)
        mock.post(f"{URL}/embed").mock(return_value=redirect)
        result = await verify_tei_server()
        embeddings._TEI_VERIFICATION.verified = True
        with pytest.raises(httpx.HTTPStatusError):
            await TeiProvider().embed_one("x", kind="query")
    assert result.status == "inconclusive"
    assert target.call_count == 0
    # The panel's probe: the factory client returns the 307 and goes nowhere.
    panel(Response(307, headers={"location": f"{elsewhere}/health"}))
    assert [r.url for r in panel.seen] == [httpx.URL(f"{URL}/health")]


# ══════════════════════════════════════════════════════════════════════════
# Format check (design D2)
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "value",
    ["abc def", "abc\rdef", "abc\tdef", "café-key", "abc\x7fdef"],
    ids=["space", "cr", "tab", "non-ascii", "del"],
)
def test_a_malformed_key_refuses_startup_without_the_value(value):
    with pytest.raises(ValidationError) as info:
        _make(tei_api_key=value)
    text = str(info.value)
    assert "TEI_API_KEY" in text
    assert "abc" not in text and "caf" not in text


def test_another_provider_ignores_it():
    s = _make(embedding_provider="ollama", tei_url="http://tei:80", tei_api_key="abc def")
    assert s.embedding_provider == "ollama"


def test_sandbox_ignores_it():
    _make(mcp_sandbox_mode=True, tei_api_key="abc def")


async def test_sandbox_sends_no_header_on_any_of_the_three(tei, panel, monkeypatch):
    """Sandbox skips the format check, so the key is not sent either."""
    unchecked = _make(mcp_sandbox_mode=True, tei_api_key="abc def").tei_api_key
    monkeypatch.setattr(settings, "tei_api_key", unchecked)
    monkeypatch.setattr(settings, "mcp_sandbox_mode", True)
    with respx.mock(base_url=URL) as mock:
        info = answer_info(mock)
        embed = mock.post("/embed").mock(side_effect=answer_per_input)
        await verify_tei_server()
        await TeiProvider().embed_one("hello", kind="query")
    panel()
    requests = [info.calls[0].request, embed.calls[0].request, *panel.seen]
    assert len(requests) == 3
    assert all("authorization" not in r.headers for r in requests)


@pytest.mark.parametrize(
    "value", ["key\fx", "key x", " key"], ids=["ff-inside", "nbsp-inside", "nbsp-lead"]
)
def test_other_whitespace_inside_a_key_still_refuses(value):
    with pytest.raises(ValidationError, match="TEI_API_KEY"):
        _make(tei_api_key=value)


def test_an_absent_key_never_refuses():
    assert _make().tei_api_key is None


# ══════════════════════════════════════════════════════════════════════════
# 401 and 403 (design D3)
# ══════════════════════════════════════════════════════════════════════════


def _criticals(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]


def _errors(caplog) -> list[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.levelno == logging.ERROR and "TEI_API_KEY" in r.getMessage()
    ]


@pytest.mark.parametrize("status", [401, 403])
async def test_info_auth_without_a_key(unkeyed, caplog, status):
    with respx.mock(base_url=URL) as mock:
        mock.get("/info").mock(return_value=Response(status))
        result = await verify_tei_server()
    assert result.status == "inconclusive"
    assert result.reason == f"GET /info answered HTTP {status}; TEI_API_KEY is not set"
    (line,) = _criticals(caplog)
    assert result.reason in line and "withheld" in line


@pytest.mark.parametrize("status", [401, 403])
async def test_info_auth_with_a_rejected_key(keyed, status):
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        mock.get("/info").mock(return_value=Response(status))
        embed = mock.post("/embed").mock(side_effect=answer_per_input)
        result = await verify_tei_server()
        with pytest.raises(RuntimeError, match="withheld"):
            await TeiProvider().embed_one("x", kind="query")
    assert result.status == "inconclusive"
    assert result.reason == (
        f"GET /info answered HTTP {status}; the server rejected TEI_API_KEY"
    )
    assert embed.call_count == 0


async def test_info_auth_never_exits_the_lifespan_path(keyed):
    """The lifespan exits only on `mismatch`; a 401 is not one."""
    with respx.mock(base_url=URL) as mock:
        mock.get("/info").mock(return_value=Response(401))
        assert (await verify_tei_server()).status != "mismatch"
    source = (ROOT / "src/main.py").read_text()
    assert 'tei_check.status == "mismatch"' in source


async def test_the_auth_line_is_not_swallowed(keyed, caplog):
    with respx.mock(base_url=URL) as mock:
        info = mock.get("/info")
        info.side_effect = [httpx.ConnectError("refused"), Response(401), Response(401)]
        await verify_tei_server()
        await verify_tei_server()
        await verify_tei_server()
    lines = _criticals(caplog)
    assert len(lines) == 2
    assert "TEI_API_KEY" not in lines[0] and "TEI_API_KEY" in lines[1]


async def test_other_inconclusive_reasons_keep_their_logging(unkeyed, caplog):
    with respx.mock(base_url=URL) as mock:
        info = mock.get("/info")
        info.side_effect = [httpx.ConnectError("refused"), Response(503), Response(404)]
        for _ in range(3):
            await verify_tei_server()
    assert len(_criticals(caplog)) == 1


async def test_recovery_after_the_key_is_fixed(keyed, monkeypatch):
    with respx.mock(base_url=URL) as mock:
        info = mock.get("/info")
        info.side_effect = [Response(401), Response(200, json=INFO)]
        assert (await verify_tei_server()).status == "inconclusive"
        monkeypatch.setattr(settings, "tei_api_key", SecretStr("right-key"))
        assert (await verify_tei_server()).status == "ok"


@pytest.mark.parametrize("status", [401, 403])
async def test_embed_auth_is_one_request_and_names_the_setting(
    keyed, slept, caplog, status
):
    embeddings._TEI_VERIFICATION.verified = True
    with respx.mock(base_url=URL, assert_all_called=False) as mock:
        embed = mock.post("/embed").mock(
            return_value=Response(status, text=f"{URL} details")
        )
        info = mock.get("/info")
        for _ in range(50):
            with pytest.raises(RuntimeError) as exc:
                await TeiProvider().embed_batch(["x"], kind="document")
            assert str(exc.value) == (
                f"TEI /embed answered HTTP {status}; the server rejected TEI_API_KEY"
            )
    assert embed.call_count == 50, "one request per slice, never retried"
    assert info.call_count == 0
    assert embeddings._TEI_VERIFICATION.verified, "the verified state is untouched"
    assert len(_errors(caplog)) == 1


async def test_embed_auth_without_a_key(unkeyed, caplog):
    embeddings._TEI_VERIFICATION.verified = True
    with respx.mock(base_url=URL) as mock:
        mock.post("/embed").mock(return_value=Response(401))
        with pytest.raises(RuntimeError, match="^TEI /embed answered HTTP 401; TEI_API_KEY is not set$"):
            await TeiProvider().embed_one("x", kind="query")


async def test_the_error_line_rearms_after_a_200(keyed, caplog):
    embeddings._TEI_VERIFICATION.verified = True
    with respx.mock(base_url=URL) as mock:
        embed = mock.post("/embed")
        embed.side_effect = [Response(401), Response(200, json=[row()]), Response(401)]
        for _ in range(3):
            try:
                await TeiProvider().embed_one("x", kind="query")
            except RuntimeError:
                pass
    assert len(_errors(caplog)) == 2


async def test_openai_401_is_unchanged(monkeypatch):
    monkeypatch.setattr(embeddings, "_OPENAI_CLIENT", embeddings._SharedClient())
    monkeypatch.setattr(settings, "openai_base_url", "https://api.openai.test/v1")
    monkeypatch.setattr(settings, "openai_api_key", "sk-test")
    with respx.mock(base_url="https://api.openai.test/v1") as mock:
        route = mock.post("/embeddings").mock(return_value=Response(401))
        with pytest.raises(httpx.HTTPStatusError):
            await OpenAIProvider().embed_one("x", kind="query")
    assert route.call_count == 1


# ══════════════════════════════════════════════════════════════════════════
# The plaintext hop (design D4) and the fingerprints (D6)
# ══════════════════════════════════════════════════════════════════════════

_PLAINTEXT_LINE = (
    "TEI_API_KEY is sent in clear over http to a non-loopback host "
    "(admitted by EMBEDDING_ALLOW_PLAINTEXT); use an https TEI_URL to protect it"
)


def _transport_lines(caplog, monkeypatch, *, url, key) -> list[str]:
    monkeypatch.setattr(settings, "embedding_provider", "tei")
    monkeypatch.setattr(settings, "tei_url", url)
    monkeypatch.setattr(settings, "embedding_allow_plaintext", True)
    monkeypatch.setattr(settings, "tei_api_key", SecretStr(key) if key else None)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        ts.log_embedding_transport()
    return [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]


def test_a_key_on_a_plaintext_remote_hop_warns_once(caplog, monkeypatch):
    lines = _transport_lines(caplog, monkeypatch, url="http://tei:80", key=SENTINEL)
    assert lines.count(_PLAINTEXT_LINE) == 1


@pytest.mark.parametrize(
    "url,key",
    [
        ("http://127.0.0.1:8080", SENTINEL),
        ("https://tei.internal.example", SENTINEL),
        ("http://tei:80", None),
    ],
    ids=["loopback", "tls", "unset"],
)
def test_no_warning_otherwise(caplog, monkeypatch, url, key):
    lines = _transport_lines(caplog, monkeypatch, url=url, key=key)
    assert _PLAINTEXT_LINE not in lines


def test_unset_key_logs_what_it_logged_before(caplog, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "tei")
    monkeypatch.setattr(settings, "tei_url", "http://tei:80")
    monkeypatch.setattr(settings, "embedding_allow_plaintext", True)
    monkeypatch.setattr(settings, "tei_api_key", None)
    with caplog.at_level(logging.DEBUG):
        ts.log_embedding_transport()
    messages = [r.getMessage() for r in caplog.records]
    assert messages[0] == (
        "Embedding transport: provider=tei scheme=http host=tei port=80 "
        "verify=n/a plaintext_override=True"
    )
    assert not any("TEI_API_KEY" in m for m in messages)


def test_the_fingerprints_ignore_the_key(tei, monkeypatch):
    monkeypatch.setattr(settings, "embedding_provider", "tei")
    monkeypatch.setattr(settings, "tei_api_key", None)
    before = (index_state.embedding_fingerprint(), index_state.retrieval_fingerprint())
    monkeypatch.setattr(settings, "tei_api_key", SecretStr(SENTINEL))
    after = (index_state.embedding_fingerprint(), index_state.retrieval_fingerprint())
    assert before == after
