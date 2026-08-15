# Phase 1 hand-off (sections 1–3)

Coordination issue #50. Branch `binary-file-transfer`, based on
`deps-refresh-2026-08`. Sections 1, 2 and 3 of `tasks.md` are done and ticked;
sections 4–7 (routes, tools, docs, ship) are Phase 2.

Read this before writing the routes: it is the public API you will call, plus
the four places where the implementation deviates from the literal wording of
`tasks.md` and why.

---

## 1. Data model & config

### `src/models/db.py` — `TransferToken`

Table `transfer_tokens`, migration **012** (`alembic/versions/012_transfer_tokens.py`).

| column | type | notes |
| --- | --- | --- |
| `id` | int PK | this is the `upload_id` the tools return |
| `token_hash` | str(64) unique | SHA-256; the token itself is never stored |
| `direction` | str(16) | `upload` \| `download`, CHECK-constrained |
| `state` | str(16) | `pending` \| `claimed` \| `completed` \| `consumed`, CHECK-constrained |
| `path` | str(1024) | canonical vault-relative |
| `vault_root` | str(1024) | absolute root at mint |
| `overwrite` | bool | |
| `expected_fingerprint` | JSONB null | `{dev,inode,size,mtime_ns,ctime_ns,sha256\|null}` |
| `key_id` / `oauth_token_id` / `user_id` | int null FK | **ON DELETE CASCADE** |
| `created_at` / `expires_at` / `claimed_at` / `completed_at` | timestamptz | `expires_at` indexed |
| `size` / `sha256` / `mime` | bigint / str(64) / str(255) | filled on completion |

No ORM `relationship()` was added from `User`/`APIKey`/`OAuthToken` to
`TransferToken` — the cascade is database-enforced, and adding a relationship
would make SQLAlchemy load and delete children in Python during the panel's
`session.delete(api_key)`, which is exactly the flow task 1.1 asks us not to
disturb.

### `src/config.py`

New settings (all env-overridable by upper-cased name, and all added to
`tests/conftest.py::SETTINGS_ENV_KEYS` — the hermeticity test enforces that):

- `transfer_token_ttl_seconds: int = 600` — `Field(ge=60, le=3600)`, so an
  operator cannot configure a default outside the window the per-call
  `expires_in` is clamped to.
- `transfer_max_upload_seconds: int = 600` (`ge=1`)
- `transfer_max_concurrent_uploads: int = 4` (`ge=1`)
- `import_allow_http: bool = False`

`Settings.public_base_url -> str | None` — `base_url` when `MCP_HOSTNAME` or
`BASE_URL` was operator-supplied, else `None`. Implemented with a
`PrivateAttr` `_public_origin_explicit` set by the `_record_public_origin`
model validator, which **must stay textually above `_derive_public_urls`**
(pydantic runs `mode="after"` validators in definition order). Mint tools call
this and refuse, naming both settings, when it is `None`.

---

## 2. `src/services/vault_fs.py` — anchored filesystem

Exceptions: `VaultFSError` (base) → `UnsafePath`, `Conflict`,
`UnsupportedFilesystem`. Absence is never `UnsafePath`: it is `None`
(fingerprint) or `FileNotFoundError`.

```python
open_root(root: Path | str) -> int
open_dir_beneath(root_fd: int, rel_dir: str | Path, *, create: bool = False) -> int
open_parent(root_fd: int, rel_path: str | Path, *, create: bool = False) -> tuple[int, str]
create_temp(dir_fd: int) -> tuple[int, str]              # (fd, ".tmp-<32hex>"), 0600
fingerprint(dir_fd: int, name: str, *, hash_up_to: int | None) -> dict | None
publish(dir_fd, tmp_name, final_name, *, overwrite: bool,
        expected_fingerprint: dict | None,
        dst_dir_fd: int | None = None) -> Published      # (name, published, temp_removed)
discard_temp(dir_fd: int, name: str) -> bool             # never raises
remove(root_fd: int, rel_path) -> None                   # permanent unlink
soft_delete(root_fd, rel_path, trash_dir=".trash") -> str  # returns ".trash/<created name>"
probe_filesystem(root_fd, trash_dir=".trash") -> None     # raises UnsupportedFilesystem
check_filesystem_support(root: Path | str) -> None       # cached wrapper
reset_filesystem_probe_cache() -> None
```

Contracts worth knowing before you use them:

- **`open_dir_beneath` always returns a new fd**, including for `rel_dir=""`,
  so you can `os.close()` the result unconditionally without risking the
  anchor. `..`, absolute paths, symlinked ancestors and non-directories all
  raise `UnsafePath`; a missing component without `create=True` raises
  `FileNotFoundError`.
- **`publish` owns the temp file from the moment it is called.** Every exit
  path unlinks it, including a rejected `final_name`. Do not unlink it
  yourself afterwards.
- **`overwrite=True` with `expected_fingerprint=None` takes the no-clobber
  path** — the null is the expected-absence sentinel, so the target must still
  be absent (`Conflict` otherwise). This is the D5 rule; do not "optimise" it
  into skipping the comparison.
- **`Published.published`** is what decides completion. A failing trailing
  temp unlink after a successful `link`/`replace` sets `temp_removed=False`,
  is logged, and must **not** release the claim.
- **`publish` takes separate source and destination descriptors.** `dir_fd`
  anchors the staged temp file, `dst_dir_fd` the destination directory
  (defaulting to `dir_fd`). Both must be on the same device — holding both
  inside the vault root guarantees it.
- **`probe_filesystem` is not wired into startup yet** — that is Phase 2's
  `src/main.py` lifespan work. Call `check_filesystem_support(root)` once at
  startup (it caches per root) and have the transfer tools surface
  `UnsupportedFilesystem` as a stable tool error. `reset_filesystem_probe_cache()`
  exists for tests and for vault-root reassignment.

`src/services/vault.py` was **not** modified. Adopting this helper for
`_atomic_write` remains the recorded follow-up.

---

## 3. `src/services/transfer.py`

Exceptions: `TransferError` (base) → `TooLarge`, `Timeout`,
`PrePublishAborted`, `SSRFError`. Note `vault_fs.Conflict` and
`vault_fs.UnsafePath` also escape `stream_to_vault` unchanged — the route has
to map those to 409 / refusal itself.

### 3.1 Token lifecycle

```python
@dataclass(frozen=True)
class Identity:  key_id: int|None; oauth_token_id: int|None; user_id: int|None

@dataclass(frozen=True)
class LockedRows: token: TransferToken; credential: APIKey|OAuthToken; user: User|None

hash_token(token) -> str
new_token() -> str                                  # secrets.token_urlsafe(32)
clamp_expires_in(expires_in: int | None) -> int     # [60, 3600], None → setting
canonical_vault_root(path) -> str                   # str(Path(x)) — matches _vault_root

await mint_token(session, direction, path, *, overwrite, identity, vault_root,
                 expected_fingerprint, expires_in=None) -> (token, TransferToken)
await claim_upload(session, token) -> TransferToken | None
await release_claim(session, row) -> bool
await consume(session, row) -> bool
await complete_upload(session, row, size, sha256, mime, *, commit=True) -> bool
await lookup_token(session, token, *, direction) -> TransferToken | None
await lookup_upload(session, token)   -> TransferToken | None
await lookup_download(session, token) -> TransferToken | None
await resolve_identity_ok(session, row, *, need_write: bool) -> bool
await resolve_root_ok(session, row) -> bool
await lock_for_publish(session, token_id) -> LockedRows | None
locked_rows_ok(locked, *, need_write: bool) -> bool     # sync
```

Behaviour Phase 2 depends on:

- `claim_upload` commits. It returns `None` for **every** unusable case
  (unknown / expired / wrong direction / already claimed / completed /
  consumed / lost the race) — map all of them to the one uniform 404.
- `lookup_*` only ever return `state='pending'`, unexpired rows. A completed
  or claimed upload token is invisible there, which is what makes replay a 404
  without any extra branch.
- `complete_upload(..., commit=False)` is the form to use inside the locked
  publish transaction, so completion and the `usage_logs` row commit together.
- `lock_for_publish` locks **token → credential → user in that fixed order**
  (no publisher can deadlock against another) and returns `None` when the row
  was cascade-deleted or is no longer `claimed`. Call it at the *start* of its
  own transaction (`async with session.begin():`). Follow it with
  `locked_rows_ok(locked, need_write=True)` — that re-runs the D4 predicates
  against the locked rows, including the vault-root comparison.
- Every ORM read in this module uses `execution_options(populate_existing=True)`.
  Without it a session that already holds the row in its identity map (and
  `expire_on_commit=False`, which is how `async_session` is configured) hands
  back stale attributes and the locked re-read proves nothing.
- Identity predicates additionally require `cred.user_id == row.user_id`, so a
  key reassigned to another user loses the capability.
- **Extra helper not in tasks.md:** `lookup_upload` / the generic
  `lookup_token`. `tasks.md` names only `lookup_download`, but
  `GET /transfer/upload/info` needs the same read for the upload direction.

### 3.2 Streaming writer

```python
await stream_to_vault(row, chunks, *, max_bytes, content_length=None,
                      deadline: float, idle_timeout: float = 30.0,
                      before_publish=None) -> {"size", "sha256", "mime"}
upload_semaphore() -> asyncio.Semaphore
```

- `row` is **duck-typed**: it needs only `vault_root`, `path`, `overwrite`,
  `expected_fingerprint`. Pass the `TransferToken` for uploads; build a tiny
  object for `import_from_url`.
- `deadline` is a **`time.monotonic()` value, not a duration**. The route
  computes `min(expires_at, claimed_at + settings.transfer_max_upload_seconds)`
  and converts.
- Raises before touching the disk when `content_length > max_bytes`; aborts at
  `cap + 1` otherwise; `Timeout` distinguishes stall ("stalled") from deadline
  ("deadline") in its message.
- Cleans up on every path: no temp file survives a failure.
- **Deviation (1 of 4): `before_publish` is an async-context-manager factory,
  not `Callable[[], Awaitable[bool]]`.** `tasks.md` gives that type but then
  requires the transaction to "stay open across `publish`", which an awaitable
  returning a bool cannot do. It is called as
  `async with before_publish() as ok:` — `__aenter__` returns whether identity
  and root are still valid (False → `PrePublishAborted`, nothing published),
  and `__aexit__` runs with the file already in place, which is where you
  commit `complete_upload(commit=False)` + the usage-log row. `tests/
  test_transfer_service.py::test_gate_stays_open_across_the_publish` pins it.
- **Deviation (2 of 4): the semaphore is per-event-loop**, not a single module
  global. Same bound, keyed by the running loop in a `WeakKeyDictionary`, so a
  test's loop cannot inherit a semaphore another loop is waiting on. Use
  `upload_semaphore()`; `transfer._upload_semaphores.clear()` resets it.

### 3.3 SSRF-guarded fetch

```python
canonicalise(url, *, allow_http: bool) -> UrlParts   # scheme, host, port, url, literal_ip
is_forbidden_address(ip) -> bool
is_global_address(ip) -> bool
default_address_policy(ip) -> bool
await default_resolver(host, port) -> list[str]
await resolve_and_check(parts: UrlParts, *, resolver=None, policy=None) -> str
class PinnedTransport(httpx.AsyncHTTPTransport)      # (address=, host_header=, **kw)
async with fetch_url_guarded(url, *, allow_http=None, max_bytes, deadline=30.0,
                             resolver=None, policy=None, max_redirects=5) as result:
    result.chunks / result.final_url / result.content_type
```

- **Deviation (3 of 4): `fetch_url_guarded` is an `@asynccontextmanager`**, not
  a function returning `(chunks, final_url, content_type)`. The body is a
  stream over a live connection; a plain tuple gives the caller no way to
  guarantee the client is closed. `result.chunks` is valid only inside the
  `with` block — so `import_from_url` should nest `stream_to_vault` inside it
  (see `test_fetched_body_streams_into_the_vault` for the exact shape).
- **Deviation (4 of 4): `resolve_and_check` takes the `UrlParts`**, not a bare
  host string, because it also has to handle the IP-literal case (no DNS at
  all) and needs the port for `getaddrinfo`.
- `allow_http=None` means "read `settings.import_allow_http`".
- `resolver` and `policy` are the injection points for tests. `policy(ip) ->
  bool`; the production default is `default_address_policy`.
- Beyond the ranges, `canonicalise` rejects: non-http(s) schemes, userinfo,
  `%` anywhere in the netloc (zone ids), single-label hosts, `localhost` /
  `*.localhost` / `*.local` / `*.internal` / `*.home.arpa`, hosts whose last
  label is all-digits or `0x…` (`2130706433`, `0177.0.0.1` — `getaddrinfo`
  resolves those to 127.0.0.1 while `ipaddress` refuses them), and any port
  not scheme-paired (https → 443/8443, http → 80/8080).
- `PinnedTransport` sets the `Host` header from the request's **netloc**
  (port included) and `extensions["sni_hostname"]` from the bare host name.
  Do not "simplify" the Host header to the bare name — a non-default port must
  appear in it.

---

## Tests

| file | contents |
| --- | --- |
| `tests/test_transfer_config.py` | `public_base_url` both states, TTL window, knobs |
| `tests/test_vault_fs.py` | 48 cases: anchoring, symlinks, races, fingerprints, trash, probe, fd leaks |
| `tests/test_transfer_service.py` | 131 cases: streaming writer, gate, semaphore, SSRF matrix, local HTTP + TLS servers |
| `tests/integration/test_transfer_pg.py` | 34 cases: cascade, mint/claim/release/consume/complete, concurrent-claim barrier, identity & root predicates, `FOR UPDATE` barrier, pool-size-1 sanity |

**How the tests get identities and sessions.** There is no fake-session
harness for the token lifecycle — every DB-backed assertion runs against real
Postgres in `tests/integration/test_transfer_pg.py`, whose
`_seed_identity(session, *, permission=…, vault_path=…)` returns
`(user, key, oauth)` with an `OAuthClient` behind the token. `_mint(...)` wraps
`transfer.mint_token`. The `clean` fixture truncates and returns the
sessionmaker. The unit suite instead uses `FakeRow` (a four-field dataclass) for
`stream_to_vault` and `RecordingGate` (an async CM) for `before_publish` — see
`tests/test_transfer_service.py`.

**The integration module fails, not skips,** when `PGVECTOR_TEST_ADMIN_URL` is
unset, unless `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1`. It also passes
`SECRET_KEY` into the `alembic upgrade head` subprocess, which
`tests/integration/test_pgvector_search.py` does **not** — that module is
currently broken on a checkout without a `.env`; fixing it is out of Phase 1's
scope but worth a one-line follow-up.

The local-server SSRF tests bind **8080** (http) and **8443** (https) because
those are the only non-default ports the scheme-paired rule admits; they skip
with an explicit reason if the port is busy. The TLS test generates a throwaway
CA and a `transfer.test` leaf with the `openssl` CLI and skips if it is absent.

Results at the end of Phase 1:

- `pytest -q` with `OMCP_ALLOW_SKIP_TRANSFER_INTEGRATION=1` → **568 passed, 44 skipped**
- `pytest -q` with `PGVECTOR_TEST_ADMIN_URL` pointing at a throwaway pg16 →
  **607 passed, 5 skipped** (the 5 are `test_fts_integration.py`, which wants
  `TEST_DATABASE_URL`)

---

---

## Phase-1 audit fixes (folded in during Phase 2)

A headless Codex review of Phase 1 returned FAIL. All six findings are fixed on
this branch, each with a regression test.

1. **BLOCKER — `soft_delete` could destroy a replacement (`vault_fs.py`).**
   `link` then `unlink` is two syscalls; a writer that replaced the source name
   in between had its file unlinked with no trash copy of it. The unlink is now
   **inode-verified**: after the link, the source name is reopened
   `O_NOFOLLOW`, `fstat`ed, and unlinked only if it is still the inode that was
   linked. Otherwise the trash link is removed and the caller gets a `Conflict`.
   Still optimistic (`renameat2(RENAME_NOREPLACE)` has no Python binding) but
   losing the race can no longer delete uncopied data.
   Tests: `test_soft_delete_does_not_unlink_a_replacement`,
   `test_soft_delete_succeeds_when_the_source_vanished_after_the_link`.

2. **MAJOR — the gate could not record inside the locked transaction
   (`transfer.py`).** `before_publish()` now yields a **`GateHandle`**
   (`ok: bool`, `session`, `async complete(result, *, published)`) instead of a
   bare bool. `stream_to_vault` calls `gate.complete(result, published=…)`
   immediately after `publish` and still inside the context, so the completion
   row and the usage-log row are written by the transaction holding the
   `FOR UPDATE` locks. A failure after the bytes landed raises the new
   **`PostPublishFailure`**, which callers must never treat as "nothing
   happened" (the route leaves the claim in place).
   Tests: `test_gate_stays_open_across_the_publish` (extended),
   `test_a_commit_failure_after_publication_is_its_own_error`.

3. **MAJOR — SSRF checks ran before IDNA normalisation (`transfer.py`).**
   `svc.prod。internal` (U+3002) passed the suffix check and then resolved as
   `svc.prod.internal`. `canonicalise` now folds the host to canonical ASCII
   **first** — NFKC, the three alternative full stops, then
   `idna.encode(uts46=True)` — and every structural, suffix and numeric check
   runs against that form, followed by an explicit LDH shape check. `idna` is
   now a direct dependency in `requirements.txt`.
   Tests: `test_alternative_full_stops_cannot_smuggle_a_forbidden_name`,
   `test_fullwidth_numeric_hosts_are_refused`,
   `test_fullwidth_public_name_is_folded_not_rejected`,
   `test_hosts_that_are_not_ldh_are_refused`.

4. **MAJOR — the destination fd was opened before the stream (`transfer.py`).**
   Renaming the destination directory mid-stream redirected the publish through
   the stale descriptor. Bytes are now staged in **`<root>/.transfer-tmp/`**
   (`vault_fs.STAGING_DIR`), and the destination parent is re-walked from the
   root descriptor **inside the gate** before `publish(src_dir_fd=staging,
   dst_dir_fd=destination)`. Same device by construction. A cheap up-front walk
   still rejects `..`/symlinks/non-directories before any bytes are read.
   Tests: `test_the_destination_is_resolved_at_publish_time_not_at_open_time`,
   `test_bytes_are_staged_outside_the_destination_folder`.

5. **MINOR — the probe missed a cross-device `.trash`.** `probe_filesystem`
   now links root→root *and* root→`.trash` (creating `.trash` through the root
   fd), so a separate-mount trash is caught at probe time rather than at the
   first `delete_file`.
   Test: `test_probe_filesystem_catches_a_cross_device_trash`.

6. **MINOR — the fetch deadline surfaced a builtin.** `fetch_url_guarded`
   translates `asyncio.timeout`'s `TimeoutError` into the service's own
   `Timeout` after cleanup; the redirect loop moved into `_fetch_hops` so the
   wrapper stays legible.
   Test: `test_deadline_covers_the_whole_fetch` (now asserts `Timeout`).

Also added by Phase 2 per supervisor decision: `TransferToken.public_id`
(`secrets.token_urlsafe(16)`, unique, set by `mint_token`, added to migration
012 in place) — the opaque `upload_id` the tools return.

## Open questions for Phase 2

1. **Where does the startup probe live?** `check_filesystem_support(root)` is
   written and cached but nothing calls it. In multi-user mode there is a root
   per user, so it probably belongs at first use per root rather than only in
   the lifespan — decide and document.
2. **`upload_id` is `transfer_tokens.id`** (a small integer). It is not a
   secret, but it is enumerable, which is why `check_upload` must scope its
   lookup to the calling identity. If you would rather have an opaque public
   id, now is the moment — it is a column addition, not a redesign.
3. **`usage_logs.key_id` has no `ondelete`.** Transfer rows cascade, but a
   `usage_logs` row written by the upload route will still block a key delete
   the way today's rows do. Unchanged behaviour, just worth knowing before the
   route starts inserting them.
4. **`Timeout` vs `consume` mapping** is the route's call: `stream_to_vault`
   raises one `Timeout` for both stall and deadline (the message distinguishes
   them) and both map to `consume` per the spec, so nothing to decide unless
   you want different responses.
5. **Rate limiting** (task 4.6) was not touched at all in Phase 1. On the
   middleware question, one thing is already settled by reading
   `src/main.py`: `APIKeyMiddleware` is **not** an app-level middleware — it
   wraps only the `/mcp` mount (`src/main.py:231`), and
   `RootMCPProxyMiddleware` only rewrites requests whose path is exactly `/`
   or `""` (`src/main.py:262`). So `/transfer/*` is not intercepted today and
   task 4.6's "add an explicit prefix exclusion if needed" should resolve to
   "not needed" — but assert it in a route test rather than trusting this note.
   What *does* apply app-wide is `GZipMiddleware` (`src/main.py:160`), which
   is why `download/file` has to set `Content-Encoding: identity` (D10).
