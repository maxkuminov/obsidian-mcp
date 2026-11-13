Refs #426. Read `proposal.md`, `design.md` (D1–D6, the accepted limitations, TC1–TC2) and `specs/` before starting. The archived `openspec/changes/archive/2026-11-08-native-tei-embeddings/` is the source of every TEI invariant this builds on, and `docs/architecture/indexing-and-embeddings.md` ("The embedding hop's transport", "TEI server identity and the embedding gate") must be read before editing.

**Base check:** `openspec/changes/tei-api-key/` exists, and `src/services/embeddings.py` defines `verify_tei_server`. One slice; no migration; no change to `tests/fixtures/tools_list_full_v2.json` or any tool text.

## 1. Settings

- [x] 1.1 `src/config.py`: `tei_api_key: SecretStr | None = None` beside the TEI fields, with a comment in the style of the TEI block (what it is, the three requests that carry it, that it belongs in a secret store, that a change needs no reset). A `before` validator maps `None`, `""` and whitespace-only to `None` and strips surrounding whitespace otherwise.
- [x] 1.2 The format check (D2) as a model validator: only under `tei` and outside sandbox, visible ASCII U+0021 to U+007E only, a fixed message naming `TEI_API_KEY`. No I/O.
- [x] 1.3 Keep `hide_input_in_errors=True`, and add a test that pins it.
- [x] 1.4 `tests/conftest.py`: add `TEI_API_KEY` to the isolated-environment list beside `TEI_URL`.

## 2. Provider, gate and panel

- [x] 2.1 `src/services/embeddings.py`: `_tei_auth_headers()`, the only reader of `get_secret_value()`. Pass it as `headers=` on `/embed` (via `_post_with_retries`) and on `_tei_info_exchange`'s `GET /info`. Unset passes `None`.
- [x] 2.2 `_judge_tei_info`: the 401/403 reasons (D3), still `inconclusive`.
- [x] 2.3 `verify_tei_server`: the auth reason is its own verdict for CRITICAL deduplication. Other inconclusive reasons stay unchanged.
- [x] 2.4 `_post_with_retries`: a TEI-only status hook evaluated where `input_limit` is, raising the `/embed` 401/403 `RuntimeError` (no URL, header or body) with no retry. OpenAI passes none, and #425's characterization tests must pass unchanged.
- [x] 2.5 The latched ERROR line for `/embed` auth failures, re-armed by a 200. It does not touch the verified state.
- [x] 2.6 `src/control_panel/routes.py`: the TEI `/health` probe passes `headers=_tei_auth_headers()`. No template change.
- [x] 2.7 `src/services/transport_security.py`: the plaintext-key WARNING in `log_embedding_transport`, after the transport line (D4). It reads only whether the key is set.

## 3. Tests (`tests/test_tei_api_key.py`, new)

- [x] 3.1 Header on all three call sites and on retries, with the exact value, using an `httpx.MockTransport` or the local stub server in `tests/_tei_stubs.py`.
- [x] 3.2 Byte-identical unset: capture each request's header set for `/info`, `/embed` and `/health` with the key unset, `""` and `"   "`, and compare it with the set captured on `aafb9f0`'s code under the same mock (record the expected set as a literal in the test).
- [x] 3.3 Trailing-newline stripping, and the format refusals (inner space, CR, tab, U+00E9), each message naming `TEI_API_KEY` and not the value. Another provider ignores a malformed value. Absent key with `tei` constructs.
- [x] 3.4 No other sender: under `ollama` and `openai`, and for a rerank request, no captured header contains the key. Add a source sweep: `tei_api_key` occurs only in the three files D1 names, and `rerank.py` does not reference `_tei_auth_headers`.
- [x] 3.5 Redirect: a 307 from each of the three paths to a second listener, which receives nothing.
- [x] 3.6 401/403 classification: `/info` reasons (both wordings), never `mismatch`, no exit from the lifespan path; the connection-error-then-401 CRITICAL sequence; `/embed` 401 under `embed_note` gives `PROVIDER_FAILED`, one request per slice, one ERROR line per run, and re-arming after a 200. (Offline through `TeiProvider`; the `embed_note`/`PROVIDER_FAILED` path through a real pass is in 3.8a's `tests/integration/test_tei_api_key_pg.py`.)
- [x] 3.7 Plaintext-key WARNING: present once for `http://tei:80` with the override and a key, absent for loopback, `https` and an unset key.
- [x] 3.8 The sentinel secrecy test of design §"Secrecy test", including the positive control.
- [x] 3.8a An index pass against a verified TEI answering 401 with the sentinel key: read the persisted `indexer_runs.error` row and render `/admin/health`; the sentinel is absent from both and the row names `TEI_API_KEY` (a database-backed integration test).
- [x] 3.9 Fingerprints: both are unchanged by setting the key.
- [x] 3.10 Run the TEI, transport and config modules (`tests/test_tei_provider.py`, `tests/test_tei_identity.py`, `tests/test_internal_transport_*.py`, `tests/test_config_validation.py`, `tests/test_perf_provider_batching.py`, `tests/test_issue_309_panel_confirm_empty.py`), then the whole offline suite once.

## 4. Documentation

- [x] 4.1 `README.md`: a `TEI_API_KEY` row in the settings table ("Secret. Bearer token for a TEI started with `--api-key`; sent only to `TEI_URL`"), and one paragraph in the TEI section saying what a 401 or 403 logs. Point out that the OpenAI provider against TEI's `/v1` already uses `OPENAI_API_KEY`.
- [x] 4.2 `.env.example`: replace "No API key." in the TEI block with `TEI_API_KEY=` and its comment.
- [x] 4.3 `DEPLOYMENT.md`: the key on the TEI path, the plaintext warning (D4) beside the internal-transport section, and restart to rotate (L1).
- [x] 4.4 `docs/deployment-kubernetes.md`: a `TEI_API_KEY` row marked **Secret**, added to the `kubectl create secret` example as an optional literal, with a note that it never goes in the ConfigMap. `deploy/kubernetes/base/configmap.yaml`: if its comment lists every setting, list `TEI_API_KEY` as Secret-only and give it no value.
- [x] 4.5 `server.json`: a `TEI_API_KEY` entry with `isSecret: true`, `isRequired: false`.
- [x] 4.6 `docs/architecture/indexing-and-embeddings.md`: a short subsection under the TEI section covering D1–D4 and L1–L6.
- [x] 4.7 `CLAUDE.md`: one clause in the "A TEI server must prove its identity" entry: 401/403 is inconclusive and names `TEI_API_KEY`, and the key goes on `/embed`, `/info` and the panel `/health` only.
- [x] 4.8 Grep check: every doc that lists `TEI_URL` as a setting also lists `TEI_API_KEY`.

## 5. Verification

- [ ] 5.1 TC2 (and TC1 for the docs' wording) against `ghcr.io/huggingface/text-embeddings-inference:cpu-1.8` started with `--api-key`: without the key, `/info` answers 401 and the server keeps serving with embedding withheld; with the key, verified and embedding. **Not run against a live server on this host:** the image is present, but no model is cached and starting TEI would download one. The documented behaviour (`/info` and `/embed` answer 401 without the exact `Bearer <key>`; `/health` is public) is taken from upstream `router/src/http/server.rs` at tag `v1.8.3`, commit `78502d8` (design, "Outcome (from source, not a live run)").
- [x] 5.2 `openspec validate tei-api-key --strict`.
- [ ] 5.3 `openspec-verifier`, then one adversarial Codex pass focused on disclosure of the key and on any request that carries it outside `TEI_URL`.
