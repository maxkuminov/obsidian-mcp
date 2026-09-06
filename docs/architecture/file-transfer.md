# File transfer

> Deep rationale extracted from `CLAUDE.md`. Read before touching `src/transfer/`, `src/services/vault_fs.py`, or the mint/redeem tools.

## File transfer

No MCP client can hand a tool the bytes of a file the user is looking at, and
the server cannot reach the user's disk. Five tools plus a public route family
close that gap. The whole design is one idea: **a capability pins everything it
may do at mint time, and the route acts on nothing else.**

**Tools** (`src/mcp_server/tools.py`, registered in `server.py`):
- `request_upload(path, overwrite=False, expires_in=None)` — readwrite. Mints a single-use upload capability; returns `upload_id` (opaque `public_id`, never the row id), a `…/transfer/upload#<token>` URL, `expires_at` and `max_bytes`.
- `check_upload(upload_id)` — `pending` | `uploading` | `unknown` | `revoked` | `completed{path,size,sha256,mime,completed_at}` | `expired`, scoped to the minting **principal** *and* user — the API key itself, or, for OAuth, the whole grant family behind the presented access token. Another principal's handle is `not found`. See "check_upload answers for the vault, not for the row" below.
- `request_download(path, expires_in=None)` — read is enough. Multi-use within its TTL; bound to the file's exact bytes at mint.
- `import_from_url(url, path, overwrite=False)` — readwrite. Server-side fetch under the SSRF policy, straight through the same capped, anchored publish.
- `delete_file(path, permanent=False)` — readwrite. See "File-access tools" in [vault-tools.md](vault-tools.md).

**Routes** (`src/transfer/routes.py`, mounted in `src/main.py`): `GET|HEAD /transfer/{upload,download}` (static pages), `GET|HEAD /transfer/{upload,download}/info`, `PUT /transfer/upload`, `GET|HEAD /transfer/download/file`. Rate-limited 30/min for pages and info, 10/min for the two that move bytes. Any other method is a 405.

### Fragment, not query string
The token travels in the URL **fragment**, which browsers never send, so Traefik and Uvicorn access logs see only `/transfer/upload`. It is redeemed *only* from `Authorization: Bearer`; a token in the path or query is ignored, so pasting a link into a URL bar cannot be replayed out of a log. Two operator constraints follow: **Traefik header logging must stay at its default `drop`**, and no APM may capture request headers. `_tracked` allow-lists log `upload_id`, `path`, `expires_in`, `overwrite` and — for imports — the URL *host* only. `check_upload` additionally validates the `upload_id` **shape** (22 URL-safe characters, exactly what `token_urlsafe(16)` produces) before `_tracked` sees it: an agent that pastes the whole `…#<token>` URL or the token itself would otherwise write a live capability into `usage_logs`, so anything off-shape is logged as `<invalid>` and answered `not found` without a lookup.

### Token state machine
`pending → claimed → completed`, with `consumed` as the dead end.
- **`claim_upload` is one committed conditional `UPDATE … RETURNING`, run before a single body byte is read.** Zero rows → the uniform 404. That is what makes single-use linearizable and what stops an unknown token from streaming gigabytes to disk.
- Handled pre-publication failures (413, 409, disconnect, dead identity) **release** the claim: nothing was published, so the human may retry the same link.
- Deadline or idle timeout **consumes** it: the request died mid-stream and a retry should mint afresh.
- A failure *after* publication — `PostPublishFailure` — leaves it **`claimed`**, forever. Never release there: from that state we cannot prove nothing landed, and a replayable token over an already-written path is the worse failure.
- **The deadline is re-checked inside the locked gate, immediately before `publish`** (`_refuse_if_past_deadline` in `stream_to_vault`). `_drain` bounds the *body*; the gate runs afterwards and can wait unboundedly on `SELECT … FOR UPDATE`, so a body that finished a second inside the deadline could otherwise publish — an overwrite included — long after the capability expired, at a moment when `check_upload` was already reporting `unknown` for it. It raises the existing `Timeout` on purpose: the route maps that to **consume**, which is what the state machine says about a deadline overrun, and it is unambiguously pre-publication so the "`PostPublishFailure` is the only exception after the bytes land" contract is intact. So the deadline is enforced twice — at drain time and again inside the locked gate — but it is honoured **to within the publish latency** (`vault_fs.publish`'s own `open_parent` walk plus, for an overwrite, the incumbent's fingerprint re-hash, bounded by `MAX_FILE_WRITE_BYTES`), not to the syscall: a literal no-write-after-deadline guarantee would need a pre-mutation callback threaded inside `vault_fs.publish`, and that coupling was judged not worth it, because the write that lands late is the consented, fingerprint-verified one and so is not a destructive write on anything unintended.

### `check_upload` answers for the vault, not for the row

Three rules, each of which was violated by reading `state` and `expires_at` and nothing else:

- **A claimed token is answered before expiry, and never as "never used".** `claimed` past its TTL is reached by exactly one path — `PostPublishFailure` — and that path runs *after* the bytes are in the vault. The expiry branch used to fire first and say the link "was never used", about a file sitting at the path; with a ten-minute TTL that was the answer an agent was most likely to see (#75). Inside `min(expires_at, claimed_at + TRANSFER_MAX_UPLOAD_SECONDS)` the answer is `uploading` and it names that deadline; past it, `unknown` — the bytes may be there, go `list_files`/`read_file` the path before re-minting, the same thing `import_from_url` says for the same outcome. `consumed` is the one mid-flight end state that *is* provably empty (the timeout paths raise before `publish`), and it says so.
- **One deadline, and one clock.** The arithmetic lives once, in `transfer.upload_stream_deadline`, which returns an *absolute UTC instant*; `routes._upload_deadline` returns that instant unchanged and the route hands it to `stream_to_vault`, which measures it through `transfer._deadline_remaining` against `transfer.now_utc()` — the same function `check_upload` compares with. Both halves are load-bearing. A second copy of the arithmetic would drift. Converting to `time.monotonic()` at claim time (which is what the route used to do) keeps the arithmetic shared but splits the *clock*: a realtime step then moves the tool and not the route, and the tool reports a stream live that the route has already killed. The accepted trade-off is that a backward realtime step extends an upload — two surfaces that agree beats two that disagree, because the disagreement is what an agent relays to a human. `import_from_url` still passes a monotonic float: its fetch budget is private and no surface reports on it. **Nothing under `src/transfer/` may define its own "now".**
- **Liveness is re-checked, inside the open session.** `lookup_by_public_id` filters on public_id/direction/principal only, while `PUT /transfer/upload` also requires `resolve_identity_ok(need_write=True)` and `resolve_root_ok`. So after an OAuth scope downgrade or a vault reassignment the tool asserted "pending" about a link every redemption would 404 (#71). Both predicates now run for `pending`/`claimed` rows and produce a `revoked` answer naming the cause. **`completed` rows are deliberately not re-checked** — that transfer already happened, and a later revocation must not turn a true report of a landed file into a false "revoked". For a `claimed` row the dead reason is *appended* to the ambiguity, never substituted for it: revocation does not un-publish bytes.

Precision here is the design, not an exception to it — this side is authenticated and identity-scoped. None of the branches may put a token or any other secret into `usage_logs`; the `upload_id` shape check still runs before `_tracked` sees the argument.

### The handle belongs to the principal, not to the credential row

`lookup_by_public_id` scoped an OAuth-minted transfer to the exact
`oauth_tokens.id` that minted it. An access token lives one hour and rotation
mints a **new row** for the same user, the same client and the same consent, so
an hour later the agent's own `check_upload` answered "no upload link with id …
was minted by this identity" — the message reserved for a genuinely foreign
handle — about a `completed` upload whose sha256 `request_upload` had told it
to come back for (#74).

The scope is the stable principal instead. An API key *is* one; an OAuth access
token is one hour of one, and the principal behind it is the **grant family**
(`oauth_tokens.grant_id`, migration 014). The lookup is one statement with a
correlated `EXISTS` joining the *minting* token to the *presenting* token on
`grant_id` — no column on `transfer_tokens`, no migration, and nothing to keep
in sync: `grant_id` never changes after insert, and the row cascades away with
its minting token anyway. The `user_id` comparison stays on top of it.

What deliberately did **not** widen:

- **A different grant is still `not found`** — another client, or a second
  `/authorize` approval by the same user for the same client. Two consents are
  two things the operator can revoke independently.
- **Redemption stays bound to the minting credential row.** `resolve_identity_ok`
  and the publish gate still load the exact `oauth_tokens` row named on the
  token. That cannot make `check_upload` lie, because `plan_mint_window` clamps
  every capability's expiry to that credential's own (see below): the minting
  access token outlives every link it minted unless it is revoked, and a
  revocation *should* kill the link. Widening redemption to "any live token of
  the family" would bind an already-minted capability to credentials that did
  not exist when it was minted — strictly more than the operator agreed to, for
  no case that is currently wrong.
- **The API-key path is untouched.** A key does not rotate; a second key of the
  same user is a different principal.

The `EXISTS` compares `client_id` as well as `grant_id`. One `grant_id` belongs
to exactly one `(client_id, user_id)` by invariant, not by constraint, and this
predicate is the access control — so a family that somehow spanned two clients
still cannot leak between them.

**Accepted limitation — pre-014 families are approximate.** 014's backfill
groups pre-existing rows by `(client_id, user_id)`, which #64 accepted as the
best available guess (nothing in the old schema recorded which consent a row
came from). Two consents by the same user *for the same client* made before 014
therefore share one family, so a token from either can read `check_upload`
status — path, size, sha256, mime — for a handle minted by the other. Same
user, same client, read-only status on a handle that authorises nothing, and it
shrinks as those tokens age out; every grant issued after 014 is exact. Not
worth inventing a consent boundary the database never recorded.

### A link never outlives the credential that minted it

Redemption re-checks the credential (`_credential_ok`), so `transfer_tokens.expires_at` alone was never the effective lifetime: an OAuth access token lives one hour, and `expires_in=3600` on that path is therefore *always* divergent (#73). `transfer.plan_mint_window` computes `min(requested TTL, credential expiry)` and the row stores that, so the tool result, `/transfer/*/info` and both pages all show a deadline enforcement agrees with — clamping once instead of teaching three surfaces the same arithmetic. **`mint_token` calls it itself, in its own transaction, immediately before the INSERT, and takes no window parameter** — it *returns* the window so the tools can report a clamp. Do not add one back: a caller-supplied deadline is a caller-supplied security boundary, and a stale window (computed before a revocation, or by a path that forgot) is exactly the divergence this exists to remove. The same call re-validates the credential with `_credential_ok`, the redemption predicate, so a key revoked, downgraded, deactivated or reassigned between the tool's permission check and the INSERT mints nothing rather than a row whose only future is the 404. **That re-validation is an unlocked `SELECT`, and deliberately so:** a revocation committing between it and the INSERT yields a capability every redemption rejects and `check_upload` reports as `revoked` — fail-closed, the same optimistic level declared elsewhere here, and the locked re-check that actually matters is the publish gate.

Two details are load-bearing. A null `expires_at` means "never expires" for an `APIKey` and "already dead" for an `OAuthToken` (`_credential_ok` refuses the latter outright), so `credential_expires_at` maps them differently — getting that backwards mints links against dead tokens. And under `MIN_MINT_TTL_SECONDS` (30 s) of runway — or with no credential row behind the call at all — the mint is **refused** rather than shortened: an error tells the agent to re-authenticate, whereas a two-second link tells it to hand a human a URL that will 404. The threshold sits below the 60 s floor `expires_in` already clamps to, so it only ever fires because the credential is nearly spent.

### The consent page states the mode

`transfer_upload.html` shows Destination, **Mode**, Maximum size and Link expires. Mode is `overwrite` from the info payload — "Replaces the existing file at `<path>`" versus "Creates a new file" — and an overwrite link gets a destructive button label and destructive status copy. That page press is the consent step for the only session-less write path in the app; until this, a link that replaces a file rendered identically to one that creates one (#72). The flag was already on the wire; the fix is display plus the matching spec requirement. The page stays self-contained and nonce-guarded — no new asset, nothing path-specific rendered server-side, everything into the DOM through `textContent`.

### Uniform 404
Unknown, expired, consumed, claimed, revoked credential, downgraded permission, inactive user, reassigned vault root, **an ownerless identity while `MULTI_USER_MODE` is on** — one status, one body.

**`user_id IS NULL` is normal in single-user mode and is nobody in multi-user mode.** `_credential_ok` compares `cred.user_id == row.user_id`, and `None == None` passed; `resolve_root_ok` and `locked_rows_ok` then authorised `settings.vault_path` outright. So a capability minted by an ownerless key *before* an operator enabled multi-user mode stayed redeemable afterwards — able to replace a file in whatever vault that setting names — even once `APIKeyMiddleware` had started rejecting the same key. `_ownerless_in_multi_user` is consulted in all three predicates, not just the credential one: the two root checks are the other half of a defensive pair, so neither is the only thing standing between a stale capability and the global root. Single-user behaviour is unchanged. `_not_found()` is the only way for a bearer-protected endpoint to say no. Precise status comes from the *authenticated* side, via `check_upload`. The uniformity is of the *response*; the branches do different amounts of work and none of this is constant-time, so do not claim timing indistinguishability for it.

### The refusal is recorded even though the response never says why (#192)

**The log is the only place the reason exists.** The response stays the sole
external answer, byte-identical in status, headers and body across every cause,
and `_not_found()` / `NOT_FOUND_BODY` are untouched. Adding a reason to the
response would hand an unauthenticated caller the oracle the uniform 404 exists
to withhold; withholding it from the *operator* as well is how token
enumeration stayed invisible on this surface. So every `_not_found()` return
goes through `_refuse`, which emits one `transfer_refused` record carrying the
reason, the route, the method, the trusted client address, and — only where a
token was actually presented — `redacted_token_tag(token)`, the `sha:` plus
eight hex characters that correlates a burst without writing credential
material. A request with no `Authorization` header carries **no tag at all**:
`sha:` of the empty string is a constant that reads like a tag, and an operator
would correlate on it.

**The diagnosis runs after the decision, behind the permit, and never touches
the admission path.** `lookup_token` and `claim_upload` are each one filtered
query — hash, direction, `state = pending`, `expires_at > now` — and that is
the linearizability argument for single-use redemption. **They were
deliberately not re-shaped.** Splitting either into a sequence of probes so the
route could name the reason would rewrite that argument for a log line. Instead
`transfer.classify_token_refusal` is a separate, **read-only** helper that
selects by token hash alone and derives the reason under a total precedence —
`unknown_token` → `wrong_direction` → state (`already_claimed` /
`already_completed` / `already_consumed`) → `expired` → `claim_lost` — so a row
matching several conditions always yields the same one. State beats expiry on
purpose: a consumed token that has since aged out was *used*, which is the more
interesting of the two true facts.

The order in `_refuse` is the contract:

1. the caller has **already** decided to refuse, and nothing below can change
   that;
2. acquire the suppression permit first, keyed on the **trusted client
   address** — the only subject computable before the diagnosis, and the one a
   caller cannot mint by rotating bogus bearer tokens. Two tenants behind one
   NAT share that bucket; that affects records only, never responses or
   `usage_logs` rows, and the summary states the withheld count;
3. only with a permit, and only when the reason is not already known, issue the
   diagnosis read.

So an **accepted** redemption issues no diagnosis query at all, and a refusal
whose source is already at its allowance issues none either — the extra read
cannot amplify a flood, because it is bounded by the same budget as the record
it exists to fill in.

**A diagnosis failure may not change the response.** That read happens on a
path whose entire contract is a fixed 404, so a dead connection or an exhausted
pool there must not turn a refusal into a 500. It is wrapped in its own
`try/except Exception` *outside* the admission decision: on failure the record
is emitted best-effort with `reason = "diagnosis_failed"` and the exception's
class in `error_type`, and the endpoint returns the same `_not_found()` it
would have returned anyway.

**Two reasons are known without a read.** `_load_valid` returns a
`TransferRefusal` for the three predicates it evaluates itself
(`credential_invalid`, `root_reassigned`, `path_invalid`) instead of collapsing
them into a bare `None`, and the upload route's re-validation splits its `or`
chain into the same three; evaluation order and short-circuiting are unchanged,
only the value carrying "no" got more specific. The post-claim gate at the
publish barrier is the opposite case: `GateHandle` exposes only `ok`, so it
emits the single generic `prepublish_revalidation_failed` and nothing finer.
**Accepted limitation:** for the seconds-long window between claim and publish
an operator cannot tell a key revocation from a permission downgrade from a
root reassignment. Giving `GateHandle` a typed cause means touching the locked
publish gate, which is a change of its own.

The 503 refusals — mount boundary, unsupported filesystem — keep their own
bodies and their own records; they are not part of the uniform 404 and never
were.

### A quarantined owner's capability is refused at redemption (#199)

A capability is a **delayed** write — or a delayed read — into a vault root:
authorised at mint and redeemed later on the public `/transfer/*` routes, which
carry no OAuth chain and never call `vault._vault_root`. So when the vault-root
overlap guard quarantines a tenant, every MCP tool refuses and an outstanding
capability would still redeem: the token pins a `vault_root` that is *still,
byte for byte*, the owner's current assignment, so every existing predicate
here agrees. Refusing the tools while leaving the capability redeemable would
leave the cross-tenant write reachable through the one path designed to outlive
the session that created it.

- **The test goes where the owner is already re-read**, all three of them:
  `resolve_root_ok` (the unlocked entry check — upload *and* download),
  `locked_rows_ok` (the locked publish path, evaluated **before** the link or
  the rename, so nothing is published), and `_identity_publish_ok` (the
  `import_from_url` gate). Same place and same manner as an inactive owner or a
  changed root.
- **`owner_quarantined` is refuse-only and costs one attribute read plus a dict
  lookup** — no session, no statement, no syscall — so it may be called from a
  diagnosis path that must not take a connection. Both quarantine reasons
  refuse, and so does the **never-published** state, for the same reason
  `_vault_root` refuses in it: a redemption served before the first detection
  is served against roots nothing has checked. Single-user mode is untouched;
  in multi-user mode an ownerless row is nobody and stays
  `_ownerless_in_multi_user`'s refusal.
- **The response does not change.** `_not_found()` stays byte-identical for
  every cause — the uniform 404 above is the anti-oracle the whole surface
  rests on — and only the server-side reason differs: `root_unverified` (no
  snapshot published in this process) or `owner_quarantined`, carried on the
  bounded `transfer_refused` event by `root_refusal`.
- **Nothing is logged from inside the predicate.** It used to emit two
  `logger.warning` lines from a service helper on the redemption path, which is
  exactly the unbounded flood channel the permit exists to close — an
  unauthenticated caller replaying one dead capability drives that branch as
  fast as it can open connections. Worse, the bounded record beside it called
  the condition `root_reassigned`, which sends an operator to look at an
  assignment nobody changed. The reason now travels to the one permitted
  emission, and the operator detail that names the overlapping roots lives on
  the surfaces built for it: the panel's quarantine block, the affected user's
  `indexer_runs` row, and the ERROR line the detection writes once per pass.
- **Precedence is the predicate's own order, written down.** Quarantine is
  evaluated before the stored root is compared to the owner's assignment, and
  `root_unverified` precedes `owner_quarantined` — with no snapshot there is no
  entry to have found. A quarantined owner whose assignment *also* changed is
  still a quarantine: the root is shared or unverifiable, and reporting the
  reassignment would tell an operator to check a column that is doing its job.

### Fingerprint binding — and its two honest limits
An overwrite upload and every download record `{dev, inode, size, mtime_ns, ctime_ns, sha256}` of the target at mint. At publish (or before a download's first byte) the incumbent is `fstat`ed and, when the mint recorded a hash, re-hashed **from the descriptor**. Mismatch → 409 / 404.
- **Optimistic, not linearizable.** `stat` → `replace` is check-then-act; a writer landing in that window is still overwritten. Same guarantee level as `edit_note(expected=…)`, declared rather than implied. The no-clobber path (`overwrite=False`) *is* kernel-linearizable — it is `link()`.
- **Metadata-only above `MAX_FILE_WRITE_BYTES`.** Hashing multi-GB media at mint is not acceptable tool latency, so `sha256` is null there and only the metadata part binds.
- **A null `expected_fingerprint` on an overwrite token is the expected-*absence* sentinel** — the target must still be absent. It never means "skip the check".

### The publish gate
`before_publish()` yields a `GateHandle` (`ok`, `session`, `complete`). Its transaction takes `SELECT … FOR UPDATE` on token → credential → user *in that fixed order* and **holds those locks across the filesystem publish**; `stream_to_vault` calls `gate.complete(result, published=…)` the instant the bytes are in place, so completion and the `usage_logs` row commit with the locks still held. A revocation, downgrade, reassignment or cascade delete needs the same rows, so it either waits for the publisher or beats it — there is no interleaving that publishes under a revoked identity.

**`import_from_url` goes through a gate too** — `transfer.lock_identity_for_publish(session, identity, vault_root=…)`, which locks the *caller's own* credential and user rows (credential → user, the same relative order) and re-checks the database's current root against the root captured when the tool started. It has no token, but it holds a network stream open for up to 30 s, which is ample time for the key to be revoked or repointed; without the gate the bytes would land under whatever the identity looked like when the tool began. Its `complete()` is a no-op: there is no token row to finish, and `_tracked` writes the usage log after the tool returns.

**`stream_to_vault` raises `PostPublishFailure` and nothing else after the bytes land.** That is the contract the upload route leans on to decide between releasing the claim and stranding it: any *other* exception — `ENOSPC` while staging, a database error opening the gate — is demonstrably pre-publication, so the claim is released and the human may retry the same link.

### No session is held across a wait or a stream

**A pooled database connection is a shared, 15-deep resource on a single worker, and the upload route used to hold one for the whole request.** `PUT /transfer/upload` opened one `async_session()` around the entire handler. `claim_upload` committed — which does hand the connection back — but `resolve_identity_ok` and `resolve_root_ok` ran immediately afterwards, autobegan a fresh transaction that was never committed, and re-checked out a connection that then stayed pinned across the semaphore wait **and** the whole body stream. The publish gate opened a second session on top, so a streaming upload held two. With `pool_size=5, max_overflow=10`, fifteen slow uploads from one tenant emptied the pool and every other caller — MCP tools, OAuth `/token`, the panel — got `TimeoutError` → 500 after `pool_timeout` (#208). `statement_timeout` cannot help: those backends are idle *in transaction*, not running a statement.

`download_file` already had the right shape — it commits, closes its session, and only then returns the `StreamingResponse` — so upload was the outlier rather than a considered exception. The rule, for everything under `src/transfer/`:

- **Phase 1 holds a connection; nothing after it does.** Phase 1 is the claim, `resolve_identity` (`resolve_identity_ok`'s verdict half, called directly here because the credential it returns is where the write bucket's minting principal comes from — see below), `resolve_root_ok`, `_path_ok`, the write-bucket take, and `check_publication_support` — including their refusal branches, which release the claim on that same session. It commits and exits before the route waits for a slot or reads a body byte.
- **Every later database action opens its own short-lived session.** `release_claim`, `consume` and the publish gate each take one and give it straight back. The gate always did; what changed is that it is no longer *nested inside* a longer-lived one.
- **The claimed row is used detached.** Safe, and not by luck: `TransferToken` has no relationships and no deferred columns, `claim_upload` commits under `expire_on_commit=False`, and `close()` expunges without expiring — so every attribute the route reads afterwards (`id`, `path`, `vault_root`, `overwrite`, `expected_fingerprint`, `claimed_at`, `expires_at`) is a plain column already populated. Two tests hold that premise up: one asserts the *model* carries nothing lazy, the other records every attribute the route reads after the session closes and checks it against the mapper's columns. Add a relationship to `TransferToken` and the first one fails, which is much better than a `DetachedInstanceError` 500 on the write path.

### Redemption spends the *minting* principal's write bucket (#194)

`PUT /transfer/upload` publishes into the vault and **never passes through
`_tracked`**, so bounding only the eight write tools left the write rate
escapable in the obvious way: mint capabilities at the general rate, then
redeem them without limit. Redemption therefore takes one token of the write
bucket (`MCP_WRITE_RATE_LIMIT_PER_MINUTE`, scope `principal_write`) — the same
bucket the write tools spend, not a second control. The general bucket is
**not** charged here: this is not a tool call, and the write bucket is the one
that follows the bytes. See [rate limits](rate-limits.md).

**The principal is the one that *minted* the capability, not the one presenting
it.** Otherwise a capability would be a way to spend somebody else's allowance,
and a drained principal could keep writing by handing its links to a fresh
credential. It costs no extra query: `resolve_identity` was split out of
`resolve_identity_ok` so it returns `(verdict, credential)`, and
`minting_principal(row, credential)` reads `("api_key", row.key_id)` or
`("oauth", grant_id)` off the `OAuthToken` row the identity check **already
loaded**. `ck_transfer_tokens_one_credential` makes the two branches exclusive;
a row naming neither credential (single-user, sandbox) resolves to no principal
and is **exempt**, matching `_tracked`'s "a control with no key is exempt
rather than a crash" rule.

**Where it sits is load-bearing, and it is inside phase 1.** The take happens
on the phase-one session, *after* the re-validation ladder and *before*
`check_publication_support` — so before any request body byte is read and
before anything is staged, and still inside the window that holds a connection,
which the rule above requires:

- **After the re-validation**, because a token that is no longer usable is a
  404 and must not *also* cost its minter a token. A capability the server was
  never going to honour cannot be charged for.
- **Before `check_publication_support`**, because that is a filesystem probe:
  a rate control that admits work has to precede the work. Same order
  `_tracked` uses.

**A refusal releases the claim; it never consumes it.** This mirrors the
`QueueTimeout` → 503 path exactly, and for the same reason — *a capability the
server declined to serve right now is a promise still outstanding*. The token
is **fine**: it has not expired, nothing was read, staged or published, and the
same link must be redeemable once the bucket refills. That is precisely what
distinguishes it from a **deadline overrun**, which consumes (`Timeout` → 408,
retry mints afresh): there the capability's own window is gone, so there is
nothing left to hand back. Getting these two backwards tells a caller whose
link is perfectly good that it expired.

The answer is **429 with `Retry-After`**, with its own body rather than the
uniform 404 — the token is fine and stays claimable, and telling a legitimate
redeemer their link had died would make them mint another, which is more load,
not less. Like the failed-authentication budget's 429 it is a **transport**
refusal, outside the in-band `MCP-REFUSAL` sentinel contract every gate inside
`_tracked` obeys: there is no tool call to answer.

The record is a `security_events` event (`transfer_refused_rate_limited`,
`reason` = the bucket scope, so an operator sees one control and not two) and
deliberately **not** a bare `logger.warning`: this is a refusal a caller can
drive at whatever rate it likes, so it has to pass the suppressor's allowance
or the control that bounds writes would open an unbounded log channel of its
own. Attribution comes off the **token row** (`key_id` / `oauth_token_id` name
the minter), as everywhere else on this route. There is no `usage_logs` row —
this route writes one only on a *completed* upload, and the refusal coalescer
lives in `_tracked`, which this path does not pass through.

**No double charge.** `request_upload`, `request_download` and `check_upload`
touch capability rows only and are not write-class — billing both the mint and
the redemption would count one write twice. `import_from_url` keeps consuming
the write bucket at its own tool call, as an ordinary write tool, and is not
charged again at redemption.

### The slot wait is bounded, sliced, and 408 beats 503

The wait for one of the `TRANSFER_MAX_CONCURRENT_UPLOADS` streaming slots was `async with upload_semaphore():` — unbounded, with the deadline consulted only *after* the slot was won. A queued upload therefore waited indefinitely, which is how the connection it used to hold became an unbounded hold. `stream_to_vault` now takes `slot_timeout` (30 s by default; `import_from_url` and every direct caller inherit it) and acquires through `_acquire_upload_slot`.

- **The acquire stays inside `stream_to_vault`.** Not in the route: at `TRANSFER_MAX_CONCURRENT_UPLOADS=1` a route-side acquire plus this one self-deadlocks, `import_from_url` would silently lose its bound, and the concurrency ceiling is a property of the writer, not of one caller.
- **Sliced at one second, not a single `wait_for`.** `asyncio.wait_for(sem.acquire(), remaining)` would convert the wall-clock deadline into a monotonic budget once, at the top of the wait — exactly the clock split `now_utc` exists to prevent (see "One deadline, and one clock" above). Instead each slice re-derives the remainder through `_deadline_remaining`, so a realtime step moves this wait and `check_upload` together. The slot timeout itself *is* monotonic, deliberately: like `idle_timeout` it is this request's private patience and no other surface reports on it.
- **Deadline first when the wait ends without a slot.** An overrun raises the existing `Timeout` → 408, token **consumed**, retry mints afresh: the capability's window is gone and the state machine has one answer for that. Only a wait cut short by `slot_timeout` *with deadline remaining* raises `QueueTimeout` → 503 with `Retry-After: 5` and the claim **released** to `pending`, because nothing was staged and the link is still good. Getting the precedence backwards tells a caller whose link expired to retry it, and a caller whose link is fine that it expired. `QueueTimeout` is deliberately not a subclass of `Timeout` for the same reason.
- **The 503 is reached only after the claim and the re-validation succeeded**, so it says nothing about a token that was never usable — the uniform 404 still covers every one of those.
- The "deadline already overrun before the wait" case cannot happen in production (`claim_upload` guarantees a positive remainder the instant phase 1 ends). It is produced in tests by advancing `transfer.now_utc`, the endorsed hook, because a monotonic stand-in could not express it at all.

### The note cap binds markdown transfers

The indexer treats **any** `.md` as a note, so the byte cap follows the extension rather than the tool. `PUT /transfer/upload` (and `import_from_url`) caps a `.md` destination at `min(MAX_NOTE_BYTES, MAX_FILE_WRITE_BYTES)` — case-insensitively — and aborts at cap+1 with a 413, staged bytes discarded and the claim released, exactly as an oversized file does. The smaller of the two binds so an operator who lowers `MAX_FILE_WRITE_BYTES` below 10 MiB does not discover that markdown became the *more* permissive destination. `/transfer/upload/info` reports the same number through the same function: the consent page prints it as "Maximum size", and a page advertising 25 MiB over a route that refuses at 10 MiB would be worse than no number at all.

### `pool_timeout` is written down

`src/database.py` sets `pool_timeout=30` explicitly. It is already SQLAlchemy's default; writing it down is the whole change, because that number decides how long a pool-exhaustion outage takes to become 500s for every tenant, and an assessment had to go read SQLAlchemy's source to find it. **No `idle_in_transaction_session_timeout`** — it was considered and rejected: the steady-state index pass holds one transaction from its first select to its commit across the whole synchronous walk (minutes on a large vault), the embed pass calls the provider before its first statement, and the link backfill scans the vault before its first insert. A server-side timeout would kill all three on the COMMIT, on every tick.

### Anchored filesystem (`src/services/vault_fs.py`)
Every transfer write and `delete_file` resolves its directories with one `openat2(RESOLVE_BENEATH | RESOLVE_NO_SYMLINKS | RESOLVE_NO_MAGICLINKS)` from an open root descriptor (#87), so a symlink anywhere in the chain raises instead of being followed and the kernel proves containment for the whole path inside the single call. The read side inherits it too — `_fingerprint_of`, `_head_bytes` and the download route's `_open_bound_file` all reach it through `open_parent`. **There is no fallback to a per-component walk**, and a `lifespan` probe beside `_check_pgvector_version` refuses to start where the syscall is unavailable (kernel < 5.6, or a container seccomp profile that blocks it); the call site raises `UnsupportedFilesystem` regardless, which is what a `MCP_SANDBOX_MODE` process — the one configuration that skips the probe — hits. On that path the download route keeps its **uniform 404**: a distinguishable status there would report a server property to an unauthenticated bearer. Bytes stage in **`<root>/.transfer-tmp/`** — as an `O_TMPFILE` inode with **no directory entry** wherever the publication probe proves that works (#92 item 1), so nothing in that directory can be observed, replaced or raced while a body streams, and an abandoned upload leaves nothing for the sweep. The directory itself stays: `O_TMPFILE` takes a *directory* to choose the filesystem the inode is allocated on. The destination parent is re-walked *inside the gate* — a descriptor opened before a minutes-long stream keeps pointing at the same directory after a rename, and publishing through it would follow the move.

**`soft_delete` is one `renameat2(RENAME_NOREPLACE)` — it never unlinks anything and never pre-creates the destination.** `rename_noreplace()` in `vault_fs.py` reaches the syscall through `ctypes` (the glibc wrapper, present since 2.28; a `syscall()` fallback exists for older glibc). It buys two guarantees at once, and both are load-bearing:

- the trash name is *created or refused* — `EEXIST` means somebody else holds it, and the delete retries under a fresh random suffix rather than overwriting;
- whichever inode is at the source when the call runs is what moves, so a writer that replaced the source ends up in `.trash` intact rather than destroyed.

Two earlier shapes were each wrong at one end. `link`+`unlink` could unlink a *different* inode than it had copied. `O_EXCL` placeholder + `os.rename` fixed that end and not the other — **`os.rename` replaces** — so between reserving the placeholder and renaming onto it, anything that took that pathname over was silently destroyed, and the error path would have unlinked that file too while tidying up "our" placeholder. `RENAME_NOREPLACE` has no reservation window and nothing to clean up. **Do not "simplify" it back to `os.rename`**; a filesystem that cannot do the non-replacing form raises `UnsupportedFilesystem` (`probe_trash` exercises the same primitive at first use, so it fails at startup rather than at the first delete), and there is no safe fallback.

The `lstat` symlink/directory refusal still runs first. A **symlink** swapped in afterwards is deliberately not re-checked — it is moved into `.trash` intact, never followed — but a **directory** is: the moved name is `lstat`ed in the trash and, if it is one, put back with a second `RENAME_NOREPLACE` and the delete refused, because a subtree carries files nobody asked to delete. If the rollback loses the source name the directory stays in `.trash` and the error names that location.

**Publication is recorded the instant `publish()` returns, before any cleanup.** In `transfer.py`, `_publish_into_current_parent` invokes an `on_published` callback before it closes anything, and every close on the publish path goes through `_close_quietly`. The reason is narrow and expensive: a bare `os.close` raising `EIO` after the bytes landed discarded the `Published` outcome and surfaced as a generic `OSError` — which the upload route reads as "demonstrably pre-publication" and answers by **releasing the claim**, handing back a replayable token over a path that already holds the file, while `import_from_url` reported "could not write" for a file it had written. `PostPublishFailure` is the only exception `stream_to_vault` may raise once the bytes are in place; anything you add between `publish` and the return must preserve that.

**Both write paths flush, and they fail in opposite directions (#97).** Every publish makes two things durable: the staged payload before publication, and the destination directory — plus the parent of every directory the call created, outward to the first pre-existing one — after it. Without the pair a crash leaves the vault contradicting something the server already said: a transfer recorded `completed` whose file is absent or truncated at a path an agent was told to read back by `sha256`, or a `create_note` that reported success and left no note.

- **Transfer.** `stream_to_vault` flushes the staged payload immediately after `_drain` returns, beside the `os.fchmod`, **before `before_publish()`** and **off the event loop** (`asyncio.to_thread`): a flush of up to `MAX_FILE_WRITE_BYTES` must not run while the gate holds `SELECT … FOR UPDATE` on the token, the credential and the user, and unlike `_drain`'s per-chunk `_write_all` a single `fsync` waits on the whole body reaching the device. A failure there is pre-publication — the claim is released and the human may retry the same link. The directory flush lives in `_publish_into_current_parent`, **after `on_published` has recorded the publication**, which is the whole of its classification: `_stream_locked` then sees `state["published"]` true and converts it to `PostPublishFailure`, so the token strands `claimed` and `check_upload` answers `uploading`/`unknown` — "the bytes may be there, go look before re-minting". Flush *before* the callback and the same failure escapes as a bare `OSError`, which the route reads as "nothing was published" and answers by handing back a replayable token over a written path. `import_from_url` inherits all of it through the shared helper.
- **Note.** `vault._flush_publication` does the same two flushes after `_atomic_write_at` publishes, and **logs every failure rather than reporting one (D18)**. That is deliberately the opposite direction, and the reason is retry safety: a stranded upload capability costs a re-mint and the human is told to look at the path first, while a note tool that reports a false failure gets *retried* — and `edit_note(append=True)` retried after a write that actually landed appends the same block twice. On this path a false failure manufactures a destructive outcome; on the transfer path it merely wastes a link. The payload is durable either way, so what is unconfirmed is only a directory entry, and the previous content survives regardless. Every `_atomic_write_at` caller inherits both flushes — `write_file` in both its no-clobber and `overwrite=True` modes included, not just the note tools.
- **Every publication flushes, not only the staged-payload helper.** `move_note`'s `renameat2` writes *two* directory entries — the new name and the source's removal — so `move_file_no_clobber` flushes both parents (plus any chain the destination created), and the rollback in `_verify_the_moved_inode` gets it free by calling the same helper with the targets swapped. `soft_delete_at` flushes the source's parent and `.trash` once the directory refusal has had its say (flushing earlier would make an intermediate state durable and then undo it), and `_refuse_a_moved_directory` flushes both after a rollback that lands. `vault_fs.remove` flushes after the permanent unlink, and so does `delete_note(permanent=True)` — an entry that survives a crash resurrects a note the agent was told is gone. All of these go through `vault_fs.flush_dir_quietly`, which is D18's direction in a function: the rename already happened, and a tool that reported it as failed would be **retried** — against a source that is no longer there, so the retry either contradicts the vault or acts on whatever has since taken the name. `flush_dir_fd` is the raising form and has exactly one caller: the transfer path's post-publication flush.
- `probe_publication` exercises **both** flushes, not just the hard link. A filesystem that links happily and rejects a directory `fsync` would otherwise pass the probe, mint a token, take a whole body, publish it and only then strand the claim on the one failure the transfer path cannot undo. `EINVAL`/`ENOSYS`/`EOPNOTSUPP`/`EPERM`/`EACCES`/`EROFS` become `UnsupportedFilesystem`; `EIO` stays an `OSError`, because a sick device is not a filesystem to be swapped out.

**The probes are split by capability and run only where that capability is used.** `check_publication_support(root)` links root→root, allocates an unnamed inode and publishes it by descriptor, and flushes both a staged file and a directory descriptor (uploads, imports, `PUT /transfer/upload`) — **and returns the staging mode that root will use**, which is the only place that decision is made; `check_trash_support(root)` renames a temp file into `.trash` (only a `delete_file` soft delete — `permanent=True` probes nothing). Both cache per root; the tools and routes surface `UnsupportedFilesystem` as a stable error. **No read path probes at all** — a probe writes, and `request_download`/`check_upload`/the download route must not create files on a read-only capability. The first publication probe per root also prunes `.transfer-tmp/.tmp-*` files older than 24 h, which is where a crashed upload's staged bytes would otherwise sit forever.

**A cached probe result is bound to the root's *identity*, not to its pathname.** The key is the configured string, but the entry carries `(st_dev, st_ino, mount_id)` of the root that was actually probed, and every hit re-reads it and re-probes on a mismatch. A configured root is a *name*, and the root is the one path this module resolves by name — repointing a symlinked root from a filesystem where the fallback's primitives work to one that refuses a directory `fsync` would otherwise reuse the first verdict *and its staging mode* for a filesystem nothing ever probed: mint a token, stage a whole body under a name, publish it, and strand the claim on the first directory flush. The mount id is optional here and degrades to `None` on a kernel without `STATX_MNT_ID`; `same_mount` refuses in that case instead, because there a fallback to `st_dev` would answer the wrong question rather than a weaker one.

**Publication into a mount beneath the vault root is refused, and the two halves promise different things** (D23). `link(2)` and `rename(2)` both return `EXDEV` across a mount boundary, and an upload stages in a root-level `.transfer-tmp` and publishes out of it — so a destination on a nested mount cannot receive one. `vault_fs.same_mount` compares `statx`'s `STATX_MNT_ID`, **never `st_dev`**: a bind mount of a directory of the same filesystem beneath the root reports the same `st_dev` on both sides (measured: 66306) and different mount ids (653 vs 6036), so an `st_dev` preflight passes and the publish fails after the body has streamed. The ids are read and compared inside one call and never persisted — plain `STATX_MNT_ID` is reused once its mount is gone, and not spanning time is the only thing that makes it sufficient without `STATX_MNT_ID_UNIQUE`. `STATX_MNT_ID` is Linux **5.8** — a **transfer-write minimum, not the server floor**, which stays at `openat2`'s 5.6. The asymmetry is deliberate: `openat2` is a containment guard, and without it every write would anchor to a descriptor nobody checked, so `lifespan` `sys.exit(1)`s. `STATX_MNT_ID` guards one refusal on one feature — without it `request_upload`, `import_from_url` and `PUT /transfer/upload` refuse (the safe direction) while reads, search, the note tools, downloads, the panel and OAuth are unaffected. Killing all of that to defend a transfer-only check is the false-positive direction, so `_check_mount_identity_support` logs one WARNING naming exactly what is degraded and starts. `/health` reports it as `transfer_mount_check_available` (`null` = the probe never ran, e.g. sandbox mode). Both startup probes are read-only and create nothing.
  - **At mint or fetch start** (`request_upload`, `import_from_url`, through `_mint_preflight`) a boundary that is *already there* is refused before any body is read, staged or published — comparing the staging directory with the **deepest existing** destination ancestor, since a directory created beneath it lands on that ancestor's mount. This is the only half that spares the bytes.
  - **Inside the publish gate**, after the authoritative destination lookup and before the link or rename, a boundary established *afterwards* is refused. Still pre-publication — nothing is written and the claim is released — but by then the body has streamed in full. **Do not describe the pair as "refused before any body is streamed."**
  - The residual `EXDEV` mapping stays in front of both, because the preflight is check-then-act: `_link_no_clobber`, `link_staged_inode` and the overwrite `renameat` all raise `MountBoundary` (a subclass of `UnsupportedFilesystem`, so every surface answers it without a new branch) naming the boundary. Previously the no-clobber branch blamed hard-link support — false, the filesystem has them — and the overwrite branch let `EXDEV` escape as a bare `OSError` into the route's generic handler, giving a server error where the other mode gave a 503.
  - **The destination *file* is checked too, for an overwrite.** A bind mount on the target itself leaves the parent comparing equal and still refuses the rename — with `EBUSY`, not `EXDEV`. The leaf's mount id is read `O_PATH|O_NOFOLLOW` (so no symlink is followed and the file is never opened for I/O) at both points, and only for a publication that *replaces*: a no-clobber publish onto an existing name is `EEXIST` whatever the mount layout, and "target already exists" is the accurate refusal there. A leaf whose mount id cannot be read is **not** treated as a boundary — inventing one would refuse a publish that would have worked. `EBUSY` out of the rename is reclassified as a boundary **only** after a fresh leaf check establishes that cause, because `EBUSY` has other sources. In practice the fingerprint check usually gets there first: binding a *different* file over the target changes the inode, so only a self-bind reaches the rename.
  - **What is unaffected:** reads, note writes on the mount (they stage in the destination's own directory), permanent unlinks, and moves that stay on one side. **What still fails, with issues open:** the soft delete (`.trash` is opened beneath the *root*, so `EXDEV`) and `move_note` across the boundary. Neither is covered here — group 4 is transfer publication only, and it compares the staging directory with the destination parent, not the two parents of a move.
  - **The upload route says which refusal it is.** `MountBoundary` subclasses `UnsupportedFilesystem`, so its handler must come **first** or it is unreachable — and the generic body ("the filesystem does not support atomic no-clobber publication") is false for a mount boundary and flatly false for an `overwrite=True` link, which never uses that publish. The mount body is **path-free**: that route is unauthenticated beyond the bearer token, and precision about which path comes from the mint tools and `check_upload`. Unknown, expired and consumed tokens stay on the uniform 404.

**`vault` has adopted these helpers** (#59): `_atomic_write_at` stages and publishes against the parent descriptor `open_mutable` opened, `move_note` publishes with `rename_noreplace`, and `delete_note` soft-deletes through `soft_delete_at` (the anchored form of `soft_delete`, taking a parent fd the caller already holds plus a `stamp`/`label`). **Every successful publication flushes the complete ancestor chain from the destination parent up to the vault root** — a note write, a transfer publish, a move, and a soft delete (whose destination parent is `.trash`). Flushing a directory persists its *contents*; the entry that names it is a separate write, so without this a crash could durably remove `Folder/note.md` and lose the whole `.trash` directory with the only copy of the note inside it.

**It is the whole chain and not this call's creations, and that distinction is the fix.** Attributing the flush to whoever ran the `mkdir` looks precise and does not survive an *abort*: a call that creates `New/Folder` and then dies before publication flushes nothing — correctly, it published nothing — and the retry finds both directories there, records no creation, and would leave the entry naming `New` durable nowhere while reporting the file `completed`. The obligation outlives the call that incurred it and outlives the *process*, so no in-memory provenance can discharge it; a durable record would be a journal for something that costs one `fsync` per path component. Vault paths are two or three deep and a directory `fsync` is metadata-only, so the conservative rule is the cheap one. `vault_fs.publication_flush_dirs` is the one definition; `created` is still passed and is currently always a subset, kept so a caller that creates a directory *outside* the destination's chain is a covered case rather than a silent hole. **Do not "optimise" this back to the created list.** Direction stays D18's: raising on the transfer path (`PostPublishFailure`), swallowed-and-logged on the note path.

**A link rewrite is a publication and owes the same chain — which is what makes `move_note`'s descriptor budget load-bearing here.** `move_note(rewrite_links=True)` pins one target per backlink source from its preflight read until its post-move write, and the source count is unbounded, so each target used to hand back its root descriptor to hold one fd instead of two. But a target with no root cannot look its ancestors up: `_flush_target_dirs` caught the missing-root `RuntimeError`, logged it, and flushed only the leaf's parent — every backlink rewrite silently exempt from the chain rule. The fix is **one shared root descriptor for the whole rewrite phase** (`MutableTarget.share_root`), `dup`ed from a root the kernel already proved rather than opened from the root *pathname* — re-resolving that name is the substitution surface #59 closes, and a `dup` resolves nothing. `share_root` verifies the shared descriptor names the same inode the target's parent was proved beneath **before** swapping, which is why it must be called *instead of* `release_root` and never after it: a target whose root is already gone cannot prove which root it was validated against. A mismatch means the vault root was repointed mid-call (`VaultRootMismatch`) and aborts the whole move while that is still free — the preflight has not mutated anything. The budget arithmetic is explicit in `max_move_rewrite_sources()`: one fd per planned rewrite plus `MOVE_REWRITE_SHARED_ROOT_FDS` (1) for the phase. **Giving each target its own root is the trap** — same correctness, but two descriptors per source, which halves the cap (384 rather than 767 planned rewrites at a 1024 soft limit) to hold N duplicate descriptors of one directory. `release_root` survives only for a target that will not publish. `check_trash_support(root, root_fd=…)` takes the caller's anchored root so the probe cannot create `.trash` in a directory the root pathname has since been repointed at. `vault` keeps its own staging — a `.tmp-<name>-…` file in the *destination* directory rather than `.transfer-tmp/` — because a note write completes in one call: there is no minutes-long stream to survive, and staging beside the destination keeps the publish a same-directory rename.

**Follow-up:** `usage_logs.key_id` still has no `ON DELETE` — a usage-log row written by an upload blocks its key's delete. Pre-existing, not regressed here.

### Path canonicalisation — do not "simplify" this
`validate_visible_path` runs (it is the shared traversal and dot-dir guard, and it is what refuses a link pointing out of the vault) but its **return value is the resolved path, and resolving follows symlinks**. The vault-relative path a transfer acts on is normalised *lexically* in `tools._vault_context`. Taking it from the resolved result silently retargets the operation: `delete_file("Attachments/alias.png")` where `alias.png` links to `secret.png` resolved to `secret.png` and deleted **that**, reporting success for a path nobody named. Keeping the caller's own components means the anchored walk is what meets the symlink — and refuses it.

### SSRF policy for `import_from_url`
The host is folded to canonical ASCII **first** (NFKC, the alternative full stops `。．｡`, then `idna.encode(uts46=True)`) and every check runs on that form — checking before normalising let `svc.prod。internal` past a suffix check and then resolved it as `svc.prod.internal`. Then: https only (`IMPORT_ALLOW_HTTP` for http), no userinfo, no zone ids, no single-label or `.localhost`/`.local`/`.internal`/`.home.arpa` names, no ambiguous numeric hosts, scheme-paired ports (443/8443, 80/8080). Every resolved address must pass an **explicit deny list** — loopback, RFC 1918, ULA, link-local, CGNAT, `0.0.0.0/8`, `240/4`, `198.18/15`, `192.0.0.0/24`, documentation, multicast, unspecified, reserved, IPv4-mapped/compat, NAT64, 6to4, Teredo (embedded IPv4 extracted and re-checked) — *and then* `is_global`. `is_global` alone is not enough; it admits IPv6 multicast and the NAT64 prefix. The connection is pinned to the validated address with `Host` and SNI kept as the original name, a new client per hop, `trust_env=False`, `http2=False`, ≤ 5 manual redirects with every rule re-applied, and one 30 s deadline over the whole thing.

### Declared filesystem semantics
Case-sensitive, non-normalising filesystems (ext4/xfs — the production bind mount) on Linux. Hard links must work within the root, and `.trash` must accept a same-device `renameat2(RENAME_NOREPLACE)`; the probes refuse otherwise rather than degrading to an overwriting move. Since #59 this covers the **note** tools too — `create_note`/`write_file` no-clobber publication needs the hard link, and `move_note`/`delete_note` need `renameat2` — so a mount without them loses those tools with a named error, not silently. Case-insensitive or normalising mounts are out of scope, as is any platform without `renameat2` — the non-replacing move has no portable fallback.

