## Context

The native TEI provider (#425, archived `2026-11-08-native-tei-embeddings`) makes three kinds of request to `TEI_URL`, and all of them use a client built by `transport_security.embedding_http_client` (`trust_env=False`, `follow_redirects=False`, pinned trust). Read on `aafb9f0`:

| Call site | Request | Client |
| --- | --- | --- |
| `TeiProvider._post` → `_post_with_retries(headers=None, …)` | `POST {TEI_URL}/embed` | `_tei_client()` (shared, 60 s) |
| `_tei_info_exchange` (from `verify_tei_server`, lifespan and gate) | `GET {TEI_URL}/info` | `_tei_client()` |
| control panel settings page, `provider == "tei"` | `GET {TEI_URL}/health` | `embedding_http_client(5.0)`, per request |

A repo-wide search for `tei_url`, `_tei_client` and `embedding_http_client(` finds no other request to `TEI_URL`, in `src/` or `scripts/`. The app's own `/health` does not contact TEI. `OpenAIProvider` builds its header per request (`"Authorization": f"Bearer {settings.openai_api_key}"`). `OPENAI_API_KEY` is a plain `str | None`, and the panel shows it masked.

`Settings` already sets `hide_input_in_errors=True`. Without that, pydantic would print the raw input (the whole env dict, for a model validator) in every `ValidationError`, as checked against pydantic 2.14 while writing this design.

TEI documents `--api-key` / `API_KEY` as "the requests must have the Authorization header set with the api key as Bearer token". The docs do not say whether `/health` and `/info` are exempt (TC1).

## Goals / Non-Goals

**Goals:** an authenticated TEI works; the key never leaves the process except in the header to `TEI_URL`; a missing or wrong key is diagnosable from the log without showing the key; nothing changes for a deployment that does not set it.

**Non-Goals:** a reranker key; key rotation without restart; any change to the identity check's verdict rules beyond the wording and logging of 401/403; any panel UI change.

## Decisions

### D1. One header helper, three call sites, per request

`_tei_auth_headers() -> dict[str, str] | None` in `embeddings.py` returns `{"Authorization": "Bearer <key>"}` when the key is set and `None` otherwise. `/embed` passes it as `headers=`, `/info` passes `headers=`, and the panel probe imports and passes it. The header is set **per request**, never as client default headers and never in `embedding_http_client`. The factory is shared with the Ollama and OpenAI clients, and the reranker has its own factory, so a client-level header would be one refactor away from reaching another endpoint.

Unset means `headers=None`, which is what `/embed` passes today. `/info` and `/health` pass no `headers` today, and `headers=None` produces the same request in httpx. The test compares the captured header set with and without the change, not only the absence of `Authorization`.

The header goes on `/info` and `/health` even if TEI turns out to exempt them (TC1). A gateway in front of TEI may protect every path, and an exempt route ignores the header.

**A source sweep pins the scope.** `tei_api_key` may appear only in `src/config.py` (the field and its validation), `src/services/embeddings.py` (the helper and the 401/403 wording) and `src/services/transport_security.py` (the plaintext warning, which reads only whether the key is set). `routes.py` uses the helper and does not read the setting. `rerank.py` and the Ollama and OpenAI code paths never call the helper.

### D2. Unset, blank and malformed values

- **Type.** `tei_api_key: SecretStr | None`, so `repr(settings)` and `str(settings.tei_api_key)` print `**********`. The value is read with `get_secret_value()` in the helper and nowhere else.
- **Blank.** A `before` validator maps `None`, `""` and any whitespace-only string to `None`.
- **Format.** Otherwise surrounding whitespace (space, tab, CR, LF) is stripped. A Secret created from a file usually ends in a newline, and TEI compares the token exactly, so stripping it is the useful reading. The remainder must consist only of visible ASCII characters (0x21–0x7E). An inner space, a control character or a non-ASCII character refuses startup with `TEI_API_KEY must be visible ASCII with no spaces or control characters`. The message never contains the value. A header holding a newline would be a header-injection vector, and httpx would refuse it on every request anyway. Refusing at boot turns a per-request failure into one clear message.
- **When it is checked.** The format check runs only when `EMBEDDING_PROVIDER=tei` and not under `MCP_SANDBOX_MODE`, following the rule that an inactive provider's URL is not validated. Under another provider the value is ignored and never sent.
- **Why the check may live in `Settings`.** The alembic init container imports `Settings`. The check is pure, does no I/O, and accepts an absent key, so an init container that is not given the Secret still starts. In the shipped Kubernetes base both containers read the same Secret through `envFrom`, so a malformed value refuses both. That is consistent: it is one broken configuration.
- **Leak through the validation error.** `hide_input_in_errors=True` stays (a test pins it), and the check is a model validator whose message is fixed text.

### D3. 401 and 403: what each request does, and what the operator sees

**`GET /info`** (`_judge_tei_info`). A 401 or 403 stays `inconclusive`. It can never become `mismatch`, so it never exits the process, and embedding stays withheld with the existing 30 s re-check. The reason becomes one of:
- `GET /info answered HTTP 401; TEI_API_KEY is not set` (or `403`), when the key is unset;
- `GET /info answered HTTP 401; the server rejected TEI_API_KEY` (or `403`), when it is set.

Other statuses keep today's reason.

**The CRITICAL line must show the auth reason.** Today an inconclusive verdict is deduplicated on the status alone, so a TEI that was first unreachable while starting and then answered 401 would never log the 401 line. An auth refusal therefore counts as its own verdict, `("inconclusive-auth", reason)`. It logs one CRITICAL when it first appears or its reason changes, and nothing more while it repeats. Every other inconclusive reason keeps today's single verdict.

**`POST /embed`.** A 401 or 403 raises `RuntimeError` with `TEI /embed answered HTTP 401; TEI_API_KEY is not set`, or `…; the server rejected TEI_API_KEY`. It is raised after one request and never retried, because neither status is retryable today and a wrong key does not heal in 3 s. The message carries no URL, header or body. `EmbedNoteFailure.capture` records it, so the pass outcome is `PROVIDER_FAILED`, previous vectors stay intact and the note stays uncertified. One provider ERROR line with the same text is logged on the first such failure. The line is suppressed while the failures repeat and re-armed by the next 200 from `/embed`, so the provider adds one line per run of failures, not one per note. The existing loggers stay as they are: `embed_note`'s per-note WARNING (`Failed to embed <path>: …`, which now carries the auth wording) and the indexer's pass-summary ERROR line. A failed 50-note pass therefore logs one provider ERROR line, one pass-summary ERROR line and 50 WARNING lines. The process is not un-verified: an auth failure says nothing about model identity.

The statuses are handled through a TEI-only hook in `_post_with_retries`, evaluated where `input_limit` is today (before the retry decision). OpenAI passes no hook. Its behaviour stays pinned by #425's characterization tests.

**Panel probe.** A 401 or 403 on `/health` shows "not reachable", as today. The panel does not change.

### D4. A key on a plaintext non-loopback hop: start, and warn once

Plaintext `http` to a non-loopback host is admitted only by `EMBEDDING_ALLOW_PLAINTEXT=true`, the operator's written decision that this hop is trusted. That decision already sends the full text of every note and every search query across the hop in clear, which is more sensitive than a key whose only power is to call that embedding server. `OPENAI_API_KEY` is already sent on an admitted plaintext hop without comment. Refusing would push an operator with an in-cluster `http://tei` behind a NetworkPolicy to turn TEI authentication off, which is worse than a bearer token on a private hop.

So the server starts. When the key is set, the provider is `tei`, and the endpoint is `http` to a non-loopback host, `log_embedding_transport` logs one WARNING after the transport line: `TEI_API_KEY is sent in clear over http to a non-loopback host (admitted by EMBEDDING_ALLOW_PLAINTEXT); use an https TEI_URL to protect it`. Loopback `http` and `https` log nothing new. No new security event is added. The existing `internal_transport_plaintext` event already marks the hop.

### D5. Redirects

The factory client does not follow redirects, so a 3xx is returned to the caller and the header is never sent to a `Location`. A test serves a 307 from `/embed`, `/info` and `/health` pointing at a second listener, and asserts that the second listener receives no request. httpx would also drop `Authorization` on a cross-origin redirect, but the design does not rely on that.

### D6. Not in either fingerprint

The key is not a property of the vectors, so it is in neither the stored embedding fingerprint nor the logged retrieval fingerprint. Setting, changing or removing it needs no reset.

## Secrecy test

`tests/test_tei_api_key.py` sets `TEI_API_KEY` to a sentinel such as `omcp-sentinel-7f3a9c`. With caplog capturing at DEBUG, it then exercises:
- settings construction and `repr(settings)`;
- the lifespan's transport line and plaintext warning;
- `verify_tei_server` against 200 match, 200 mismatch, 401 and 403;
- `/embed` answering 200, 401, 403 and 503 three times, through `embed_note`, with the stored `EmbedNoteFailure` message;
- the panel settings page HTML;
- an index pass whose `/embed` answers 401, then the persisted `indexer_runs.error` row and the rendered `/admin/health` page (run history and error ring);
- the app's `/health` body;
- `embedding_fingerprint()` and `retrieval_fingerprint()`;
- every emitted security event;
- a `ValidationError` from a malformed sentinel (`omcp-sentinel` plus a space, plus `x`).

The sentinel must not occur in any captured log record (message, args and formatted exception), in any response body or in any exception string. The positive control: the mock TEI receives `Authorization: Bearer omcp-sentinel-7f3a9c` exactly.

## Claims to confirm

- **TC1.** Whether TEI 1.8.x exempts `/health` and `/info` from `--api-key`. The design works either way (D1). The result goes into the docs' wording only.
- **TC2.** A TEI started with `--api-key K` answers 401 to `/embed` without the header, and 200 with `Authorization: Bearer K`. To be run against `ghcr.io/huggingface/text-embeddings-inference:cpu-1.8`, as #425 task 7 did.

**Outcome (from source, not a live run).** Read in upstream `router/src/http/server.rs` at tag `v1.8.3` (commit `78502d8`, the version #425 ran):
- `/info`, `/embed` and the other inference routes are in `routes`. The auth middleware is layered on `routes` only, and answers `401 UNAUTHORIZED` unless the `Authorization` header equals `Bearer <api_key>` exactly.
- `/health`, `/`, `/ping` and `/metrics` are in `public_routes` and are not authenticated.

So TC1 is "`/info` is guarded, `/health` is public". TC2 is supported by the source. TEI itself never answers 403; a 403 can only come from a gateway. No TEI was run against this change, because there is no locally cached model (task 5.1).

## Accepted limitations

- **L1. Restart to rotate.** The key is read at settings construction. A rotated key needs a restart, like every other setting, and until then every request fails as D3 describes.
- **L2. Plaintext hop.** With `EMBEDDING_ALLOW_PLAINTEXT` the key crosses a non-loopback hop in clear (D4). This is warned about at startup, not refused.
- **L3. No reranker key.** A TEI used as `RERANK_URL` behind `--api-key` still cannot be used as a reranker. That is a separate setting for a separate hop, and it would be its own change.
- **L4. The panel does not say whether a key is configured.** A panel template change brings a browser pass and the CSP bar, which is out of proportion here. The startup log and the 401/403 wording carry the diagnosis.
- **L5. The key is in process memory and in the environment** of the container, like `OPENAI_API_KEY`, `SECRET_KEY` and `DATABASE_URL`. Anyone who can read `/proc/<pid>/environ` or `kubectl get secret` can read it.
- **L6. Surrounding whitespace is stripped** (D2). A TEI deliberately started with a key that ends in a space cannot be matched. Such a key is not a usable bearer token anyway.

## Migration Plan

None. The setting is new, defaults to unset, and unset is today's behaviour.
