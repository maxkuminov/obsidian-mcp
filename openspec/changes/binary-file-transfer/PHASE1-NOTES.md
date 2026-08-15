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
rename_noreplace(src_dir_fd, src_name, dst_dir_fd, dst_name) -> None  # renameat2(RENAME_NOREPLACE)
soft_delete(root_fd, rel_path, trash_dir=".trash") -> str  # returns ".trash/<created name>"
probe_publication(root_fd) -> None                       # link within the root
probe_trash(root_fd, trash_dir=".trash") -> None          # rename_noreplace into .trash
prune_stale_staging(root_fd, *, max_age_seconds=86400) -> int
check_publication_support(root: Path | str) -> None      # cached; also prunes on first use
check_trash_support(root: Path | str) -> None            # cached
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
- **The probes run on first use per root, not in the lifespan** (Phase-2
  decision; see open question 1 below), and are **split by capability**:
  `check_publication_support(root)` for anything that publishes,
  `check_trash_support(root)` only for a soft delete. Read paths call neither —
  a probe writes. `reset_filesystem_probe_cache()` exists for tests and for
  vault-root reassignment.

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
   *(Superseded by Phase-2 fix 1 below: the inode check was replaced by an
   atomic rename.)*
   `link` then `unlink` is two syscalls; a writer that replaced the source name
   in between had its file unlinked with no trash copy of it. The unlink is now
   **inode-verified**: after the link, the source name is reopened
   `O_NOFOLLOW`, `fstat`ed, and unlinked only if it is still the inode that was
   linked. Otherwise the trash link is removed and the caller gets a `Conflict`.
   Still optimistic — this note believed `renameat2(RENAME_NOREPLACE)` had no
   Python binding, which is only true of the *stdlib*; Phase-3 fix 1 reaches it
   through `ctypes` — but losing the race can no longer delete uncopied data.
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

5. **MINOR — the probe missed a cross-device `.trash`.** The probe now covers
   the trash as well as the root. *(Phase-2 fix 5 split this into
   `probe_publication` and `probe_trash`; the trash half tests `rename`.)*
   Test: `test_probe_trash_catches_a_cross_device_trash`.

6. **MINOR — the fetch deadline surfaced a builtin.** `fetch_url_guarded`
   translates `asyncio.timeout`'s `TimeoutError` into the service's own
   `Timeout` after cleanup; the redirect loop moved into `_fetch_hops` so the
   wrapper stays legible.
   Test: `test_deadline_covers_the_whole_fetch` (now asserts `Timeout`).

Also added by Phase 2 per supervisor decision: `TransferToken.public_id`
(`secrets.token_urlsafe(16)`, unique, set by `mint_token`, added to migration
012 in place) — the opaque `upload_id` the tools return.

## Phase-2 audit fixes

A headless Codex review of the finished change returned FAIL. All ten findings
are fixed on this branch, each with a regression test. Where they change
behaviour recorded above, this section wins.

1. **BLOCKER — `soft_delete` was still not atomic (`vault_fs.py`).**
   *(Half superseded by Phase-3 fix 1 below: the `O_EXCL` placeholder is gone
   and the move is `renameat2(RENAME_NOREPLACE)`. The "never unlink, move
   whichever inode is at the source" half stands.)*
   The Phase-1 fix made the unlink inode-verified, which narrowed the window
   but did not close it, and it turned a lost race into a `Conflict` with a
   half-finished delete to clean up. It became **one `rename`**: a unique
   `<ts>-<basename>-<8 hex>` name reserved in `.trash` with
   `O_CREAT|O_EXCL|O_NOFOLLOW`, with the source renamed onto that placeholder.
   Nothing is ever unlinked, so a concurrent replacement is *moved to the
   trash* rather than destroyed, and no verification step is needed.
   `_unlink_if_same_inode` is gone. The `lstat` symlink/directory refusal still
   runs first; a symlink swapped in after it is moved intact, never followed.
   Tests: `test_soft_delete_moves_a_replacement_rather_than_destroying_it`,
   `test_soft_delete_reports_a_source_that_vanished_before_the_move`,
   `test_concurrent_soft_deletes_of_the_same_basename_get_distinct_names`,
   `test_soft_delete_maps_an_unusable_trash`.

2. **MAJOR — `delete_file`'s `.md` refusal read the caller's string
   (`tools.py`).** `path.lower().endswith(".md")` let `note.md/.`, `note.md/`
   and `a//note.md` through to the anchored walk, which happily deleted the
   note. The canonical path from `_vault_context` is computed **first** and the
   refusal runs on its final component, case-insensitively.
   Test: `test_delete_file_refuses_markdown_however_it_is_spelled`.

3. **MAJOR — `import_from_url` published without a gate (`tools.py`,
   `transfer.py`).** The fetch runs for up to 30 s; a key revoked, downgraded,
   deleted or repointed inside that window still got its bytes published. New
   `transfer.lock_identity_for_publish(session, identity, *, vault_root,
   need_write=True)` — an async CM that `SELECT … FOR UPDATE`s the caller's
   credential and user rows (credential → user), re-runs D4's predicates and
   the root comparison against them, and yields a `GateHandle` held across the
   publish. `complete()` is a no-op for imports. Tests (Postgres gate):
   `test_import_publishes_nothing_when_the_key_is_{revoked,downgraded}_mid_fetch`,
   `test_import_publishes_nothing_when_the_root_is_reassigned_mid_fetch`,
   `test_import_completes_under_an_untouched_identity`.

4. **MAJOR — `check_upload` logged whatever it was given (`tools.py`).** An
   agent that pasted the upload URL or the token in place of the handle wrote a
   live capability into `usage_logs`. The argument is now shape-checked
   (`transfer.is_public_id`: 22 URL-safe characters) before the lookup *and*
   before `_tracked` logs it — off-shape values log as `<invalid>` and return
   `not found`. Tests:
   `test_check_upload_refuses_an_off_shape_id_before_the_lookup`,
   `test_a_pasted_url_or_token_never_reaches_usage_logs`.

5. **MAJOR — read paths ran a probe that writes (`vault_fs.py`, `tools.py`,
   `routes.py`).** `check_filesystem_support` created a temp file, a hard link
   and `.trash` — so `request_download` with a read-only key wrote to the vault.
   Split into `probe_publication` (link within the root) and `probe_trash`
   (rename into `.trash`), with `check_publication_support` /
   `check_trash_support` cached per `(root, kind)`. Publication is probed by
   `request_upload`, `import_from_url` and `PUT /transfer/upload`; the trash
   only by a `delete_file` soft delete; reads probe nothing. `probe_trash`
   tests `rename` rather than `link`, because that is what the delete uses.
   Tests: `test_a_read_only_mint_writes_nothing_to_the_vault`,
   `test_request_download_does_not_probe_the_filesystem`,
   `test_permanent_delete_does_not_create_the_trash`,
   `test_probe_trash_tolerates_a_filesystem_without_hard_links`.

6. **MINOR — the download route leaked a descriptor (`routes.py`).** Every
   early return after `_open_bound_file` — fingerprint mismatch, hash mismatch,
   `HEAD` — left the fd open; a token bound to a changed file can be retried at
   the rate limit, so that is slow fd exhaustion. A `handed_off` flag plus
   `finally` closes it on every path except the streaming response, which owns
   it. Tests: `test_refused_downloads_do_not_leak_descriptors`,
   `test_head_downloads_do_not_leak_descriptors`.

7. **MINOR — pre-publication failures stranded the claim (`routes.py`).** The
   catch-all left the token `claimed` for its whole TTL after failures that
   provably published nothing. `stream_to_vault` guarantees `PostPublishFailure`
   is the *only* error raised once the bytes are in place (now stated in its
   docstring), so everything else releases the claim. Tests:
   `test_a_full_disk_mid_stream_releases_the_claim`,
   `test_a_gate_entry_failure_releases_the_claim`.

8. **MINOR — abandoned staged uploads were never swept (`vault_fs.py`).**
   `prune_stale_staging` removes `.transfer-tmp/.tmp-*` files with an mtime
   older than 24 h (strictly older, so an in-flight upload cannot be swept),
   run once per root from the first publication probe. Tests:
   `test_prune_stale_staging_removes_only_the_old_ones`,
   `test_the_first_publication_probe_sweeps_stale_staging`.

9. **MINOR — the "no timing-distinct branches" claim was not true and not
   tested.** Removed from `design.md`, `CLAUDE.md` and the route docstring; the
   uniform-404 guarantee is about the status and body only. No code change.

10. **NIT — `test_body_is_not_read_before_the_claim` counted nothing.** It now
    asserts `consumed == 0`, which is the property the test is named for.

## Phase-3 audit fixes

A second headless Codex review returned two findings. Where they change
behaviour recorded above, this section wins.

1. **BLOCKER — the `O_EXCL` placeholder was still clobber-able
   (`vault_fs.py`).** Phase-2 fixed the *source* end of the soft delete and
   left the *destination* end open. `os.rename` **replaces**: between the
   placeholder being reserved and the rename landing on it, a writer with
   access to `.trash` could take that pathname over and have its file silently
   destroyed — and the error path would then unlink that writer's file while
   cleaning up "our" placeholder. Reserving a name does not own it.

   New primitive `rename_noreplace(src_dir_fd, src_name, dst_dir_fd, dst_name)`
   calls `renameat2(2)` with `RENAME_NOREPLACE` through `ctypes` (the glibc
   wrapper, present since 2.28; a `syscall()` fallback keyed by
   `platform.machine()` covers older glibc, and an unlisted architecture is
   treated as "unavailable" rather than guessed at). Errno mapping lives in one
   place: `EEXIST` → `FileExistsError`, which `_rename_into_trash` retries
   under a fresh random suffix (bounded, `_TRASH_ATTEMPTS`);
   `EINVAL`/`ENOSYS`/`EOPNOTSUPP`/`EXDEV` → `UnsupportedFilesystem`, with **no**
   fallback to a replacing rename; `EISDIR`/`ENOTDIR` → `UnsafePath`. The
   placeholder reservation (`_reserve_trash_name`) is gone entirely — there is
   nothing to create, and therefore nothing to clean up on the error paths.

   `probe_trash` now goes through the same `_rename_into_trash` helper, so a
   filesystem that renames happily but rejects the flag is caught at first use
   instead of failing every delete. `_renameat2_raw` is the single seam the
   tests drive.

   Tests: `test_soft_delete_never_replaces_a_name_somebody_else_holds`,
   `test_rename_noreplace_refuses_an_existing_destination`,
   `test_rename_noreplace_moves_across_directories`,
   `test_soft_delete_moves_a_replacement_rather_than_destroying_it`,
   `test_rename_noreplace_maps_unsupported_errnos`,
   `test_rename_noreplace_maps_kind_errnos`,
   `test_rename_noreplace_propagates_unrelated_errnos`,
   `test_rename_noreplace_without_the_syscall_is_unsupported`,
   `test_renameat2_is_available_on_this_platform`,
   `test_soft_delete_uses_a_no_replace_rename`,
   `test_soft_delete_maps_an_unusable_trash`,
   `test_probe_trash_catches_an_unusable_trash`,
   `test_probe_trash_uses_a_no_replace_rename`.

2. **MAJOR — publication was recorded *after* cleanup could fail
   (`transfer.py`, `tools.py`).** `_publish_into_current_parent` closed the
   destination descriptor in a `finally` before its return value reached
   `_stream_locked`, and `_stream_locked` closed the staging and root
   descriptors bare in its own `finally`. Any of those three raising `EIO`
   after the bytes had landed discarded the `Published` outcome and surfaced as
   a generic `OSError` — which the upload route's catch-all treats as
   *demonstrably pre-publication* and answers by releasing the claim, handing
   back a replayable token over a path that already holds the file. On the
   import side the same error returned "Could not write …" for a file that had
   been written, which an agent acts on by retrying.

   `_publish_into_current_parent` now takes an `on_published` callback and
   fires it the instant `publish()` returns with `published=True`, before it
   closes anything. Every close on the publish path goes through the new
   `_close_quietly` (logged, never raised) — deliberately *not* the staged temp
   file's own descriptor, where a failing close genuinely means the data may
   not have reached the disk and must fail pre-publication. Both the gated and
   the gateless branch now share one `try` whose handler converts anything
   raised after publication into `PostPublishFailure`. `import_from_url` grew
   an explicit `PostPublishFailure` handler (it is not an `OSError`, so it used
   to escape the tool entirely) that tells the agent the file **is** in place.

   Tests: `test_a_failing_close_after_publication_does_not_hide_the_publish`,
   `test_a_failing_close_after_publication_never_raises_a_bare_oserror`,
   `test_a_failing_close_after_a_gateless_publish_still_succeeds`,
   `test_a_failing_close_after_publication_keeps_the_token_completed`,
   `test_a_failing_close_plus_a_failing_commit_still_strands_the_claim`,
   `test_import_survives_a_failing_close_after_publication`,
   `test_import_reports_a_post_publish_failure_as_written`.

## Open questions for Phase 2

1. **Where does the startup probe live?** *Answered:* first use per root, never
   the lifespan (there is a root per user in multi-user mode), split into
   `check_publication_support` and `check_trash_support`, and never on a read
   path — see Phase-2 fix 5.
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
