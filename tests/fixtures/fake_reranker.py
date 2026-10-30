"""A fake Jina-compatible `/v1/rerank` server for the reranker tests (#367, D14).

A tiny ASGI app served by uvicorn on `127.0.0.1:<ephemeral>` in a background
thread (its own event loop), so it serves both `asyncio.run` tests and
pytest-asyncio ones. Loopback `http` passes the `RERANK_URL` policy with no
override. Offline: nothing leaves the host.

`server.mode` selects one behaviour per request (see `MODES`); every request
body is recorded in `server.requests`, so tests can assert the documents,
`top_n` and `model`. `server.max_in_flight` is the highest number of requests
the app was serving at once.
"""
from __future__ import annotations

import asyncio
import json
import re
import socket
import threading
import time

import uvicorn

from src.services.rerank import PROBE_QUERY

MODES = (
    "correct", "inverted", "position", "tie", "drip", "sleep:<ms>", "status:<code>",
    "redirect", "not_json", "no_results", "short", "long", "dup_index", "oob_index",
    "bool_index", "nan", "inf", "string_score", "oversize", "sorted", "scores:<json>",
    "gzip", "cl_oversize", "chunked_oversize",
)

_WORD = re.compile(r"\w+")


def _overlap(query: str, doc: str) -> float:
    q = {w.lower() for w in _WORD.findall(query)}
    d = {w.lower() for w in _WORD.findall(doc)}
    return float(len(q & d))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeReranker:
    def __init__(self, port: int | None = None):
        self.port = port or free_port()
        self.mode = "correct"
        self.requests: list[dict] = []
        #: Each request's headers, lower-cased names, as received.
        self.request_headers: list[dict[str, str]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self.redirect_target_hits = 0
        #: For `scores:<json>`-less tests: a callable (query, docs) -> scores.
        self.scorer = None
        self._server: uvicorn.Server | None = None
        self._thread: threading.Thread | None = None
        self.disconnects = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/rerank"

    # ── the ASGI app ──────────────────────────────────────────────────────

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            while True:
                msg = await receive()
                if msg["type"] == "lifespan.startup":
                    await send({"type": "lifespan.startup.complete"})
                elif msg["type"] == "lifespan.shutdown":
                    await send({"type": "lifespan.shutdown.complete"})
                    return
        if scope["path"] == "/elsewhere":
            self.redirect_target_hits += 1
        self.request_headers.append(
            {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])})
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if not msg.get("more_body"):
                break
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await self._respond(body, send, receive)
        except Exception:  # noqa: BLE001 - the client went away mid-drip
            self.disconnects += 1
        finally:
            self.in_flight -= 1

    async def _respond(self, raw: bytes, send, receive):
        try:
            req = json.loads(raw)
        except ValueError:
            req = {}
        self.requests.append(req)
        docs = req.get("documents") or []
        query = req.get("query") or ""
        n = len(docs)
        mode = self.mode

        async def reply(status: int, payload, headers=()):
            data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            await send({"type": "http.response.start", "status": status,
                        "headers": [(b"content-type", b"application/json"), *headers]})
            await send({"type": "http.response.body", "body": data})

        def results(scores):
            return {"results": [{"index": i, "relevance_score": s} for i, s in enumerate(scores)]}

        # A test's scorer never answers the startup probe: the probe pair is
        # scored by keyword overlap, as a correct reranker would.
        if self.scorer is not None and mode == "correct" and query != PROBE_QUERY:
            return await reply(200, results(self.scorer(query, docs)))
        if mode == "correct":
            return await reply(200, results([_overlap(query, d) for d in docs]))
        if mode == "sorted":
            scored = sorted(((_overlap(query, d), i) for i, d in enumerate(docs)), reverse=True)
            return await reply(200, {"results": [{"index": i, "relevance_score": s}
                                                 for s, i in scored]})
        if mode == "inverted":
            return await reply(200, results([-_overlap(query, d) for d in docs]))
        if mode == "position":
            return await reply(200, results([1.0 if i == 0 else 0.0 for i in range(n)]))
        if mode == "tie":
            return await reply(200, results([0.5] * n))
        if mode.startswith("sleep:"):
            await asyncio.sleep(int(mode.split(":")[1]) / 1000)
            return await reply(200, results([_overlap(query, d) for d in docs]))
        if mode.startswith("status:"):
            return await reply(int(mode.split(":")[1]),
                               b"upstream says: secret-response-text " + query.encode())
        if mode == "redirect":
            loc = f"http://127.0.0.1:{self.port}/elsewhere".encode()
            return await reply(302, b"", headers=[(b"location", loc)])
        if mode == "drip":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            payload = json.dumps(results([1.0] * n)).encode()
            for i in range(len(payload) * 50):
                await send({"type": "http.response.body", "body": b" ", "more_body": True})
                try:
                    msg = await asyncio.wait_for(receive(), 0.1)
                except asyncio.TimeoutError:
                    continue
                if msg["type"] == "http.disconnect":
                    self.disconnects += 1
                    return
            await send({"type": "http.response.body", "body": payload})
            return
        if mode == "not_json":
            return await reply(200, b"<html>not json</html>")
        if mode == "no_results":
            return await reply(200, {"data": []})
        if mode == "short":
            return await reply(200, results([1.0] * (n - 1)))
        if mode == "long":
            return await reply(200, results([1.0] * (n + 1)))
        if mode == "dup_index":
            items = [{"index": 0, "relevance_score": 1.0}] * n
            return await reply(200, {"results": items})
        if mode == "oob_index":
            items = [{"index": i + 1, "relevance_score": 1.0} for i in range(n)]
            return await reply(200, {"results": items})
        if mode == "bool_index":
            items = [{"index": True if i == 1 else i, "relevance_score": 1.0} for i in range(n)]
            return await reply(200, {"results": items})
        if mode == "nan":
            return await reply(200, b'{"results": [' + b",".join(
                b'{"index": %d, "relevance_score": NaN}' % i for i in range(n)) + b"]}")
        if mode == "inf":
            return await reply(200, b'{"results": [' + b",".join(
                b'{"index": %d, "relevance_score": Infinity}' % i for i in range(n)) + b"]}")
        if mode == "string_score":
            items = [{"index": i, "relevance_score": "0.9"} for i in range(n)]
            return await reply(200, {"results": items})
        if mode == "oversize":
            pad = b" " * (2 << 20)
            return await reply(200, json.dumps(results([1.0] * n)).encode() + pad)
        if mode == "gzip":
            import gzip as _gzip

            data = _gzip.compress(json.dumps(results([1.0] * n)).encode())
            return await reply(200, data, headers=[(b"content-encoding", b"gzip")])
        if mode == "cl_oversize":
            # Declares more than 1 MiB and then sends nothing: the client must
            # refuse on the header alone, before any body byte.
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"content-length", str((1 << 20) + 1).encode())]})
            while (await receive())["type"] != "http.disconnect":
                pass
            self.disconnects += 1
            return
        if mode == "chunked_oversize":
            await send({"type": "http.response.start", "status": 200,
                        "headers": [(b"content-type", b"application/json")]})
            chunk = b" " * 65536
            for _ in range(40):   # 2.5 MiB, no Content-Length
                await send({"type": "http.response.body", "body": chunk, "more_body": True})
            await send({"type": "http.response.body", "body": b""})
            return
        if mode.startswith("scores:"):
            return await reply(200, results(json.loads(mode[len("scores:"):])))
        raise AssertionError(f"unknown fake mode {mode!r}")

    # ── lifecycle ─────────────────────────────────────────────────────────

    def start(self) -> "FakeReranker":
        config = uvicorn.Config(self, host="127.0.0.1", port=self.port, log_level="error",
                                lifespan="off", loop="asyncio")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._thread.start()
        deadline = time.monotonic() + 10
        while not self._server.started:
            if time.monotonic() > deadline:
                raise RuntimeError("fake reranker did not start")
            time.sleep(0.01)
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._server.force_exit = True
        if self._thread is not None:
            self._thread.join(timeout=10)
