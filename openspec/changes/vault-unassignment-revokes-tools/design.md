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
indexer) is deliberately left add-only. Making it authoritative would mean
dropping entries that a concurrent per-request warm had just written for a user
created after the bulk query ran — a spurious refusal for a legitimate caller.
It is not needed: every consumer of `_vault_root(user_id)` (MCP tools, the
panel's vault browser, the auth routes) warms that user individually first.

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
