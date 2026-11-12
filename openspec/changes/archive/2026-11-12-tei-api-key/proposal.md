Refs #426 (follow-up to #425, the native TEI provider).

## Why

`TeiProvider` sends no `Authorization` header. A Text Embeddings Inference server started with `--api-key` (or `API_KEY`), or one behind a gateway that wants a bearer token, answers 401 to every request, so the `tei` provider cannot be used with an authenticated server at all. The identity check then reports `GET /info answered HTTP 401`, which says nothing about the missing key, and the `/embed` failure is an `HTTPStatusError` that does not say either.

The OpenAI provider already sends `OPENAI_API_KEY`. TEI needs the same, and the key must be handled as a secret.

## What Changes

- **One new optional secret setting, `TEI_API_KEY`.** Empty or whitespace-only means unset. When it is set and the provider is `tei`, every request the server makes to `TEI_URL` carries `Authorization: Bearer <key>`. There are three such requests: `POST /embed`, the identity check's `GET /info`, and the control panel's `GET /health` reachability probe. Nothing else sends it: not the Ollama or OpenAI providers, not the reranker, and no other provider's client.
- **Unset is byte-identical to today.** The same requests go out with the same headers, and the same log lines are written. With the key unset, behaviour changes only when TEI answers 401 or 403: the failure wording now names `TEI_API_KEY`, and the log lines change as described in the next bullet.
- **401 and 403 name the setting, never the value.** From `GET /info` they stay *inconclusive*: the process never exits and embedding stays withheld. The reason now says whether `TEI_API_KEY` is unset or was rejected. From `/embed` they fail the call as a non-retried provider failure with the same wording, and one ERROR line is logged per run of such failures.
- **The value is never shown.** Not in a log line, `/health`, the panel, an exception message, a stored failure message, a security event, a settings `repr`, a validation error, or either embedding fingerprint. A sentinel-value test proves this.
- **Format check at settings construction**, only with the `tei` provider and outside sandbox mode. Surrounding whitespace is stripped (a trailing newline from a Secret file is the common case). What remains must be visible ASCII with no space, otherwise startup is refused with a message that names the setting and not the value. An absent key is always valid, so the migration init container is unaffected unless it is given a malformed value.
- **Plaintext hop.** When the key is set and `TEI_URL` is plaintext `http` to a non-loopback host, which is only possible with `EMBEDDING_ALLOW_PLAINTEXT=true`, the server starts and logs one startup WARNING naming both settings. It does not refuse (design D4).
- **No redirect can carry the header.** The factory client never follows redirects. A test pins this for a 3xx from `/embed`, `/info` and `/health`.
- **Docs:** README settings table and the TEI section, `.env.example`, `DEPLOYMENT.md`, `docs/deployment-kubernetes.md` (the key goes in the Secret, never the ConfigMap), `server.json` (`isSecret: true`), and the indexing architecture note.

Out of scope: a key for the reranker (`RERANK_URL`), which may also be a TEI server (L3); showing "key configured" on the panel (L4).

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `embedding-providers`: the "TEI requires no API key" scenario becomes "with `TEI_API_KEY` unset". New requirements cover the header and its three call sites, the empty/unset rule, secrecy, the 401/403 classification and log, the format check, and the plaintext warning.

`internal-transport-security` is unchanged: the factory, the redirect rule, the URL policy and the transport line all stay as they are. The plaintext warning is a separate line from the transport line.

## Impact

- **Code:** `src/config.py` (one `SecretStr | None` field, the blank normalisation and the format check), `src/services/embeddings.py` (one header helper used by `/embed` and `/info`, the 401/403 reasons, the auth-aware CRITICAL dedup), `src/control_panel/routes.py` (the `/health` probe sends the header), `src/services/transport_security.py` (the plaintext WARNING beside the transport line). No migration, no change to any tool, schema or description, and no fingerprint change. A key change needs no reset.
- **Tests:** a new `tests/test_tei_api_key.py`, the `TEI_API_KEY` entry in `tests/conftest.py`'s isolated-environment list, and the existing TEI and transport tests unchanged and green.
- **Operators:** nothing changes until the key is set. Rotating the key needs a restart of the app, like every other setting.
