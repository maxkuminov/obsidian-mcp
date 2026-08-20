# Design — vault unassignment revokes every tool

## Where the gate goes, and why not in the tools

Issue #66 offered two fixes. Deleting the user's `notes_metadata` rows on the
NULL transition (embeddings and links cascade) revokes the data but destroys an
index a reassignment would have to rebuild from scratch — for a 2,500-note
vault that is a full re-embed. It also leaves the *authorisation* question
unanswered: the key still authenticates, and the next tool that grows a
DB-backed path is leaky again by default.

The gate therefore lives in `_tracked`, the decorator every tool impl already
wears, and the index is preserved.

Putting it in the individual tools was rejected for the reason the bug exists:
the leaky tools are exactly the ones that had no reason to call `_vault_root`,
so a per-tool check is a list somebody must remember to extend. One gate in the
shared decorator is enforced by construction — a new tool cannot be registered
without it, because registration goes through `_tracked`.

## Fail closed, with an empty exemption list

The rule applied: *if the user has no vault, no tool that reads or writes vault
content or metadata may run.* Every `_tracked` tool does one of those, so the
exemption list is empty. The two candidates worth naming:

- `get_vault_guide` — returns a static primer **plus the vault's own
  `CLAUDE.md`**. It reads vault content. Gated.
- `check_upload` — reports `path`, `size`, `sha256`, `mime` for a completed
  upload into the vault. That is vault metadata for a vault the caller no
  longer holds. Gated. (Its own identity scoping is unchanged; the gate runs
  first.)

The remaining transfer tools (`request_upload`, `request_download`,
`import_from_url`, `delete_file`) all mint or exercise a capability over a
vault root, so gating them is not a judgement call.

`_tracked` decorates nothing but MCP tool impls, so the gate has no other
blast radius.

## The shared dict is not enough — the request keeps its own answer

An earlier draft of this change stopped at "make the per-request warm evict".
Adversarial review found that admission was still not fail-closed under
concurrency, and it is worth writing down exactly why, because the fix looks
redundant otherwise.

`_user_vault_cache` is process-global, and *two* writers touch it: the
per-request warm in `APIKeyMiddleware` and the indexer's bulk warm, which is
add-only. Ordered interleaving:

1. the indexer's bulk `SELECT` is issued; its snapshot still shows user 42 at
   `/vaults/a`;
2. the admin commits `vault_path = NULL` for user 42;
3. an MCP request authenticates: its warm reads NULL and **evicts** 42;
4. the older bulk query finally returns and **re-inserts** `/vaults/a`;
5. the request, still in flight, reaches `_tracked` — which reads the restored
   root and admits the call. `edit_note` then writes into a vault the caller no
   longer holds.

Eviction cannot fix this on its own: the losing writer is a query that was
already in flight, so there is no ordering the dict can enforce after the fact.

So `APIKeyMiddleware` binds the root **it** read to the request —
`current_vault_root = (user_id, Path | None)`, a ContextVar next to
`current_user_id` in `src/auth/session.py` — and `_vault_root` prefers that
snapshot whenever it is set for the user being resolved. A ContextVar bound in
this request's context cannot be written by the indexer task, so step 4 is
inert. The shared dict remains the fallback for non-request contexts (the
indexer itself, panel routes, tests).

Two constraints held:

- **`user_id is None` never consults the snapshot.** Single-user and sandbox
  mode answer from `settings.vault_path` before the snapshot is read at all.
- **The snapshot is keyed by user id.** A context carrying another user's
  snapshot falls through to the dict rather than answering for the wrong vault.

The alternative Codex offered — a version counter with compare-and-set on the
dict — would also close the race, but it makes every reader depend on a
monotonic source the DB does not currently provide, and it still leaves the
shared dict as the authority for a decision that is per-request. The snapshot
is smaller and the failure mode of a bug in it is a spurious refusal.

`test_stale_bulk_warm_really_does_repopulate_the_shared_dict` pins step 4 as a
real effect (the negative control), and
`test_stale_bulk_warm_cannot_readmit_a_revoked_user` plus
`test_api_key_middleware_binds_the_snapshot_and_the_tool_refuses` pin the
refusal — the latter through the real middleware, with the stale bulk warm
landing mid-request.

## Cost: the hot path stays a dict lookup

`_vault_root(uid)` is a `dict.get`. It must stay one — a DB round trip per tool
call would be a new query on the hottest path in the server. What makes a pure
cache lookup *correct* is that `APIKeyMiddleware` already re-reads
`users.vault_path` from the database on **every** authenticated MCP request
(`warm_user_vault_cache(session, api_key.user_id)`, and the same on the OAuth
branch). The gate is therefore reading a value at most one request old.

## Why the warm had to become authoritative

The warm was add-only: `if row is not None: cache[id] = …`. With a NULL
`vault_path` it did nothing, so a previously cached root survived. The panel
compensates by calling `clear_user_vault_cache(target.id)`, but that only
clears the cache **in the worker process that handled the panel POST**, and it
only happens on that one code path. Under more than one worker, the other
processes would keep serving from a stale entry indefinitely.

Making the single-user form of the warm evict when the row is absent closes
that: the per-request warm is now the authoritative refresh, so a mid-session
unassignment is visible from the next tool call in every process, whether or
not `clear_user_vault_cache` was called.

The bulk form (`warm_user_vault_cache(session)` with no user id, used by the
indexer) is deliberately left add-only — but **that is only safe because of the
request-scoped snapshot above**, not on its own. Making it authoritative would
mean dropping entries that a concurrent per-request warm had just written for a
user created after the bulk query ran, and it would still not fix the ordered
race, whose losing writer is a query already in flight. Eviction in the
per-request warm is therefore defence in depth for non-request contexts (the
indexer, panel routes, a process with no snapshot bound); the snapshot is what
makes admission itself fail closed.

## Cold cache is a refusal, not a 500

`_vault_root` raises the same `RuntimeError` for "no assignment" and "never
warmed". Both refuse, with the same message. A cold cache is not permission to
serve stale rows, and a fresh process must not 500 on it. The operator-facing
distinction is a `logger.warning("tool_refused_no_vault", extra={"user_id": …})`.

## What the refusal says and logs

A fixed string: no path, no query echo, no note content — the point of the fix
is that this caller learns nothing further about the vault. The usage log gets
`params["error"] = "no_vault_assigned"` alongside the existing allow-listed
params; both are already-known values (the tool name and user id are columns),
so the refusal is not a new disclosure channel.

## Residual, stated

The refusal is per tool call and reads a value refreshed at authentication
time. An in-flight call that passed the gate microseconds before the admin
saved the unassignment completes. That is the same optimistic level as every
other revocation in this server except the transfer publish gate, which holds
`FOR UPDATE` locks across the filesystem write precisely because it can.


## Ownerless credentials: `user_id is None` means two different things

`user_id IS NULL` on an `api_keys` / `oauth_tokens` row is the *single-user*
shape, and single-user mode is where `_vault_root(None)` returning
`settings.vault_path` is correct. The problem is that the NULL outlives the
configuration it belonged to:

- a key minted while `MULTI_USER_MODE=false` keeps `user_id = NULL`;
- the bootstrap backfill in `src/auth/routes.py` adopts every NULL row for the
  first administrator, but it only runs while `users` is **empty** — it is
  guarded by `_users_table_empty(session)` under an advisory lock;
- so flipping the flag *after* users exist leaves those NULLs unclaimed
  forever.

Such a key then authenticated (the middleware only checks activity and expiry),
skipped the warm (`if api_key.user_id is not None`), left `current_user_id` at
None, and got the global vault from `_vault_root(None)` — including for
`readwrite`, i.e. `edit_note` over the whole vault with no owner.

Both layers are fixed, deliberately:

- **`APIKeyMiddleware` 401s it** (both branches, `reason=ownerless_credential`,
  the same response body as any other rejected credential — which check failed
  is not disclosed). This is the gate.
- **`_vault_root(None)` raises when `settings.multi_user_mode`.** This is the
  invariant. The middleware can be bypassed by a future caller that resolves a
  root outside a request; the resolver cannot.

The bootstrap flow is unaffected: it is a panel `POST /admin/auth/register`
handled by FastAPI, never routed through `APIKeyMiddleware`, and it does not
resolve a vault root before the admin row exists. Single-user mode is
unaffected in both layers — the checks are conditioned on
`settings.multi_user_mode`.

## Warm-then-resolve is the same bug in a second place

`vault_page` did:

```python
await warm_user_vault_cache(session, user.id)
vault = _vault_root(user.id)      # <- re-reads the shared dict
```

which reopens exactly the window the tool gate closes: the stale bulk warm can
land between the two statements. The fix is the same idea as the ContextVar —
**use the value the warm returned** — expressed in the simplest form available
to a handler that already has it in hand:

```python
vault = await warm_user_vault_cache(session, user.id)
if vault is None:
    vault_error = vault_unassigned_error(user.id)
```

The message comes from `vault_unassigned_error()` so the panel and the agent
are told the same thing. `vault.html` is untouched — the `vault_error` empty
state it already renders is the refusal.

Any future caller that warms and then resolves has this bug. The rule is in
CLAUDE.md: use the return value.
