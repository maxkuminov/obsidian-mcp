# Control panel

> Deep rationale extracted from `CLAUDE.md`. Read before touching panel templates, flash messages, the admin guards, or the Danger zone.

## No CSP, and vendored assets

- Jinja2 control panel. htmx and Chart.js are **vendored** under
  `src/control_panel/static/vendor/` and served from `/admin/static` — no CDN,
  no `integrity` to keep in sync (#130). There is no Tailwind; the styling is
  hand-written CSS in `base.html`. Google Fonts is the one remaining remote
  origin and ships no executable code. **No CSP on the panel**, deliberately:
  every template drives its controls with inline `onclick`/`onsubmit` and htmx
  attributes, so the only policy they survive is one carrying `unsafe-inline`
  for scripts — which permits exactly the injection a CSP is bought to stop.


## Theming: one token source, dark canonical

- **Every color the panel renders comes from a custom property defined in
  `src/control_panel/templates/_theme.html`.** That partial is included from
  the `<head>` of `base.html`, `auth_base.html` *and* `authorize.html` — the
  three roots of this surface — and none of them keeps a palette of its own.
  They used to: `auth_base.html` carried a second copy of the token block and
  `authorize.html` an entirely separate set of literals a couple of shades
  off the panel's, which is exactly how the two drifted apart. The consent
  page's historical shades survive as the `--consent-*` group in the shared
  partial, so there is still one physical palette; under light they fold onto
  the panel's own values, which is where the surfaces finally converge.
  A page template may add a scoped token; it may not define a palette.

- **Dark is canonical; light is the override set.** Bare `:root` holds the
  dark values, byte-identical to the literals they replaced —
  `:root[data-theme="light"]` overrides, and a `prefers-color-scheme: light`
  block guarded on `:root:not([data-theme="dark"])` supplies the OS default.
  Inverting that (light-first tokens) would have re-based every tested page
  for no benefit. The light-first ordering that *is* correct for the transfer
  pages is a different surface — see below.

- **Glows, overlays and shadows get explicit per-theme values; they are never
  derived from the accent.** `rgba(155,130,232,0.16)` reads as a halo on a
  near-black ground and as dirt on a white one, and `rgba(0,0,0,0.7)` is
  depth on `#070910` and a smear on `#ffffff`. That is why the token list is
  long (103 properties, up from 18): each distinct role and strength is its
  own token so light can move it independently.

- **Control boundaries have their own tokens, deliberately separate from
  `--border`/`--border-2`.** WCAG 1.4.11 governs the boundary of a *control*
  at 3:1; card outlines, table dividers and progress-bar tracks are
  decorative and are not. So `--control-border` (inputs, selects, ghost
  buttons, both icon toggles), `--control-border-strong` (their hover
  borders), `--consent-control-border` / `--consent-control-border-hover`
  (the `/authorize` radio cards and Deny button) and `--scrollbar-thumb` all
  exist to be darkened on light without turning every card outline in the
  panel into a hard rule. `--error-border-strong` is the extreme case:
  `.btn-danger`'s fill is about 1.1:1 against the card, so its border is the
  only boundary the control has and has to carry the whole 3:1 by itself.
  Same reasoning for `--disabled-opacity`: `.btn:disabled` fades the whole
  button, its label included, so the value that reads as "disabled" on dark
  puts the label under 3:1 on light. **Adding a control means checking its
  boundary, not reusing `--border` because it looks right in dark** — the
  full matrix is enumerated per control in the change's `checks/contrast.py`.

- **`<meta name="theme-color">` keeps a static default *and* a
  `data-theme-color-token` attribute.** The bootstrap overwrites the content
  on every load, but the static value is what a JS-off visitor gets, so it
  must equal the dark value of the token it names; `checks/token_coverage.py`
  pins the two together. The literal there is not a stray — it is a mirror,
  and it is checked as one.

- **An OS theme flip re-dispatches `themechange`.** CSS re-evaluates a media
  query on its own; a Chart.js canvas does not. Without the
  `matchMedia('(prefers-color-scheme: dark)')` listener, a visitor with no
  stored choice who flips their OS theme gets a re-themed page around a chart
  still painted in the old palette. The listener stays quiet once an explicit
  choice is stored, because that choice outranks the OS.

- **CSS cannot share one declaration list between a selector and a
  media-guarded selector**, so the light palette is written out twice —
  once under `:root[data-theme="light"]`, once inside the
  `prefers-color-scheme: light` block. The `panel-light-mode`
  change ships `checks/token_coverage.py`, which asserts the two copies are identical and that every dark token has a light
  value. Edit one, edit the other.

- **The pre-paint bootstrap stamps `data-theme` only when a choice is
  stored.** With no stored choice the attribute stays absent and the media
  block decides, which is what keeps the OS default working with JavaScript
  off; a stored choice is applied in `<head>` before first paint, so there is
  no flash of the other theme. Storage access is wrapped in try/catch
  throughout: when it throws, the toggle still switches the current document
  and the next load simply follows the OS. The script also copies the active
  `--bg` into `<meta name="theme-color">`, because no stylesheet can reach a
  meta tag.

- **The bootstrap is an inline `<script>` with no nonce, and that is
  consistent, not an oversight.** There is deliberately no CSP on the panel
  (above); every control here already runs from an inline `onclick`. A theme
  bootstrap *must* be inline and synchronous in `<head>` — an external file
  is a network round trip in front of first paint, which is the flash the
  script exists to prevent. If a CSP is ever added to the panel, this script
  needs a nonce along with every other inline handler; it is not a special
  case.

- **SVG colors ride in `style=""`, not in `fill=`/`stroke=`.** SVG2 parses
  presentation attributes with the property's own grammar rather than as CSS
  declarations, so `var()` substitution in them is not dependable across
  browsers — a `fill="var(--gem-facet)"` that fails to substitute falls back
  to black. `style=""` is real CSS and is already this panel's idiom.

- **The transfer pages are a different surface and must stay one.**
  `transfer_upload.html` and `transfer_download.html` serve third parties
  under a strict per-request nonce CSP with static-response discipline
  (`src/transfer/routes.py`). They carry their own local `--t-*` token block,
  keep their light-first `prefers-color-scheme` behaviour, and have no
  toggle, no `localStorage`, and no shared panel partial — including the
  panel's would drag an unnonced inline script into a page whose whole point
  is that everything inline is nonced. Their security headers must stay
  byte-identical once each response's per-request nonce is canonicalized.

- The archived ops-health and usage-slicing sweep wrappers locate their
  shared `colorscan` module by walking ancestors, then use its `repo_root()`
  helper (#220). They must not assume a fixed `parents[N]` depth; archiving
  changes that depth and previously broke the import before any scan ran.

- **The sweep discovers its own templates now, and refuses to report a
  vacuous pass.** `checks/literal_sweep.py` in the light-mode change (under
  `openspec/changes/archive/`) is what proves "no color literal outside a
  token definition". It used to scan a *recorded* list —
  `colorscan.PANEL_TEMPLATES`, the templates that existed when the sweep was
  written — and to resolve the templates directory as `parents[4]` of
  `__file__`, which assumed the change still sat at
  `openspec/changes/<id>/checks/`. Archiving moved it one level deeper, so an
  unmodified run found no files and reported **zero declarations, zero
  literals, exit 0** — a clean bill of health for a directory it never read
  (#170). `colorscan` now finds the repo root by walking up from `__file__`
  to the nearest directory holding `pyproject.toml` / `Makefile` / `.git`
  (falling back to `git rev-parse --show-toplevel`), globs
  `src/control_panel/templates/*.html` instead of carrying a list, and makes
  `scan_all` raise `SystemExit` — non-zero, with the directory named — when
  it reads zero templates, is asked for a template that is not there, or
  finds zero in-scope declarations. So **a new page needs no registration**:
  add the template and it is swept. `PANEL_TEMPLATES` survives as a snapshot
  of the glob for `replay.py`, but the panel-vs-transfer distinction is a
  predicate (`is_panel_template`), not membership in a list that can go
  stale. Running `colorscan.py` itself prints the root, the directory, the
  file count and the per-template declaration counts — the cheapest way to
  confirm it is reading anything at all. Pages added since the light-mode
  change: `performance.html` (#160), `search_analytics.html` (#161),
  `health.html` (#163) — all token-only, and the glob confirms it.

  Two sibling wrappers in other archived changes
  (`2026-08-29-panel-ops-health/checks/literal_sweep.py`,
  `2026-08-29-panel-usage-slicing-quotas/checks/literal_sweep.py`) still
  derive `REPO` as their own `parents[4]` and so cannot even import
  `colorscan` from the archive; they are the same bug one level up and are
  not fixed here.


## The health page (#163)

- **Three sections, three sources, and only one of them is a table this
  application writes.** `/admin/health` shows the newest 50 rows of
  `indexer_runs` (the same `recent_indexer_runs` the performance page reads,
  asked for a longer window rather than given a second query with its own
  scoping rule), the in-process error ring buffer, and the age of the newest
  `backups_log` row. The dashboard's health strip reads the same three and
  links to the page; a failed pass links to `#run-<id>`, which is why every
  row on the page carries that id.

- **Backup recency is a database row because the container cannot see the
  backups directory — and must not.** Dumps are written host-side by
  `make db-backup` into `$(DATA_DIR)/backups`; mounting that path would put a
  host-specific volume into a public repo's compose file and hand the
  application write access to its own backups. So `docker/record-backup.sh`
  inserts `(filename, size_bytes)` through the same `docker exec … psql`
  channel `pg_dump` just used. `filename` is the **basename**: the directory
  differs between the repo default and the real host's `Makefile.local`, and a
  host path does not belong in a shared table.

- **Writer, reader and migration all resolve `public.backups_log`.** An
  unqualified reference goes through `search_path`, so a role or database
  pointing elsewhere would have the three addressing different tables — and the
  failure is silent in the worst direction: backups recorded into a table the
  panel never reads, so the page warns that none have been taken while one is
  taken daily. The writer's INSERT and the panel's SELECT name it explicitly.
  Migration 021 instead **pins the path** — `SET LOCAL search_path TO public` at
  the top of `upgrade()` and `downgrade()`, `RESET` at the end because alembic
  runs every pending revision in one transaction. Pinning and not
  `schema="public"` on each `op.*` call: a schema-qualified table does not match
  an ORM model that declares no schema, and `alembic check` would then report
  drift forever. Its catalog lookups still resolve the qualified name, and it
  asserts after creating (and before dropping) that the object really is
  `public.backups_log`, so a pin that failed to take effect fails closed.

- **The recording guard has three branches and they do not agree, deliberately.**
  `make deploy` runs `db-backup` *before* `db-migrate` — the backup is the only
  way back from a bad migration, so that ordering is not negotiable — which
  means the deploy that ships migration 021 dumps against a database with no
  `backups_log` in it. Table absent → loud warning, **exit 0** (a bookkeeping
  row must never block a disaster-recovery step). Table present, insert lands →
  success. Table present, insert fails → **non-zero**, because once the table
  exists the panel reports its newest row as the age of the last backup, and a
  dump that silently failed to record itself makes the page claim a staler
  safety net than the operator has. A probe that cannot answer is treated as
  the third case: the database answered a full `pg_dump` through that channel
  seconds earlier.

- **The age is the whole signal, and the page says so.** Nothing verifies that
  the file still exists, that it restores, or that its contents are the
  database — the panel cannot see the filesystem it is reporting on. Staleness
  warns at **> 8 days**, a day clear of a weekly cadence so a Sunday schedule
  does not page every Saturday.

- **Errors live in a `deque(maxlen=100)`, in memory, for the life of the
  process.** Container logs rotate with the container, so the errors from before
  the last deploy — usually the ones worth reading — are gone.
  `src/services/error_log.py` attaches at ERROR in the lifespan *before* the
  sandbox-mode branch returns, so a process that dies in a startup guard still
  has the record. Only four strings per entry are kept, never the `LogRecord`:
  a record holds `exc_info`, which holds a traceback, which holds every frame's
  locals, and a hundred of those is a bounded buffer of unbounded object graphs.
  `emit` cannot raise — it runs inside every `logger.error(...)` in the process,
  including the ones in exception handlers.

- **The root logger alone would miss every 500, so `uvicorn.error` is attached
  explicitly.** uvicorn applies its own `dictConfig` at startup and it stops the
  ascent before the root — 0.52 sets `propagate: false` on the `uvicorn` logger,
  the *parent* of `uvicorn.error`, which is precisely where an unhandled
  exception in the ASGI app is logged. (Which logger in that chain carries the
  flag has moved between releases, so the test asserts the behaviour — a
  root-only probe handler sees nothing — rather than the config key.) Attaching
  only to the root captures the errors the application chose to log and none of
  the ones it did not, which is the opposite of what a page headed "recent
  errors" is for. The **same handler instance** goes on both, and `emit` stamps
  each record so one that reaches the handler twice, on a release whose chain
  does reach the root, is stored once. Any other logger that breaks propagation
  has to be added to `CAPTURED_LOGGERS` or its errors will not appear.

- **The observation window is rendered next to the count, always.** "No errors"
  on its own reads as a claim about the server; what it means is "this process
  has not failed since it started", and a container restarted two minutes ago
  has an empty buffer for reasons unrelated to health. Same reason the copy
  points at `make logs` for anything older or fuller.

- **The dashboard strip has a failure boundary; the dashboard does not depend
  on it.** Its reads are the only ones on `/admin/` that touch `indexer_runs`
  and `backups_log`, so a fault confined to those two tables would take the
  whole dashboard down for every user while every other query on the page
  succeeds — on the page an operator opens *because* something is wrong. So
  `_health_strip_or_degraded` rolls the failed transaction back (without it the
  render's own queries raise `InFailedSQLTransaction` instead of the real
  error), records `panel_health_strip_failed` at ERROR so the ring buffer
  catches it, and renders a "health summary unavailable" strip. Saying so beats
  rendering "ok" from a query that never returned. It goes through
  `security_events.emit` rather than the bare logger because a caller can drive
  it on demand by reloading the dashboard — see
  [security event logging](security-event-logging.md); the panel's on-demand
  index and embed failures migrated with it for the same reason.

- **Reconfiguring logging must not take the buffer with it.** `configure_logging`
  removes and closes every root handler *except* the one `error_log` owns, in
  either call order, and `attach()` stays idempotent across it. The rule and
  the reason it is not `basicConfig(force=True)` live in
  [security event logging](security-event-logging.md); what matters here is
  that the health page keeps working because the code says so, not because an
  import happens to run first.

- **Errors and backup age are admin-only; the pass history is scoped.** The run
  list follows the performance page's rule (an admin sees every pass, a regular
  user only their own, and deliberately none of the ownerless single-user or
  global ones). The other two sections have **no owner to scope by** — the ring
  buffer holds whatever the process logged, other tenants' paths and identifiers
  included, and a backup covers the whole database — so they are gated on
  `is_admin` rather than filtered, and a non-admin gets a page of their own run
  history with a line saying why.

## The vault-root quarantine on the operator surfaces (#199)

When two active users' vault roots overlap — or one cannot be examined — the
affected accounts are refused by every MCP tool, every pass stage and the
transfer redemption gate. The checks and the snapshot are in
[vault-roots-and-tenancy.md](vault-roots-and-tenancy.md); what belongs here is
how the panel says so.

- **The panel reads the published snapshot and never recomputes it.**
  `_quarantine_view(is_admin)` is one attribute read and a mapping walk: no
  session, no statement, and — the part that matters — **no `open`, no `stat`
  and no `realpath`**. A page render must not touch a vault root, and two
  independent computations of "do these roots overlap" is how the panel and the
  enforcement come to disagree about a live tenant.
- **It re-reads no `users` row either.** Every name, path, relation and cause
  rendered is a fact the detection recorded at the moment it looked, and the
  surfaces label it **"as at last check"**. The operator's first move on
  reading "vault root overlaps bob" is to edit or delete one of the two
  accounts; a render-time resolution would show a changed path — or a blank
  where the deleted peer was — beside a condition that is still in force. **Do
  not "improve" these into a join.**
- **The tri-state comes through intact, and the empty state is not an
  all-clear.** Never published → `checked: False`, and the strip and page say
  the roots have not been checked in this process yet *and* that every
  multi-user tool call is refused while that holds. Published and empty →
  `accounts: []`, and nothing renders at all: an empty snapshot is the healthy
  state, and a green badge for it would be one more thing to read on a page
  that exists to show what is wrong. Published with reasons → one entry per
  named account.
- **The two reasons are worded apart, from the one shared wording.** An overlap
  names the peer account, the peer's root and the relation; an unexaminable
  root names the root and the cause and states that **no peer was observed**.
  Both come from `vault_overlap.operator_text`, which is also what the ERROR
  log line and the `indexer_runs.error` row carry — composing a second wording
  in a template is how the panel and the run row come to describe the same
  condition differently. Calling an unexaminable root an overlap sends an
  administrator hunting for a second account that does not exist.
- **Administrators only, and that asymmetry is the point.** The condition names
  another account and another account's vault path, which is exactly what the
  tool-facing refusal withholds because *its* reader is a tenant's agent.
  `_quarantine_view` returns `None` for everyone else, so the block is absent
  rather than empty — the same split the backup and error cells already take.
  The operator surfaces name everything; the agent-facing wording names no
  other user, no other path and no note path.
- **It is not a flash message.** There is nothing to dismiss and no
  acknowledgement: the condition clears on its own when a later snapshot stops
  naming the account. A dismissible banner for a state that is still in force
  is a banner an operator dismisses once and never sees again.
- **A degraded health strip carries no `quarantine` key** and renders nothing
  there; the condition is still on `/admin/health`, which reads the snapshot
  itself and shares none of the strip's three queries. This keeps
  `_health_strip_or_degraded`'s failure return byte-identical, which an
  existing test asserts exactly.
- **`vault_page` refuses a named user through the gate's own
  `_refuse_quarantined_root`** and renders the existing `vault_error` empty
  state — placed *after* the warm, so an account whose assignment was just
  cleared is told it has no vault rather than that it is quarantined for a root
  it no longer holds. The wording there is the agent-facing one that names no
  other account, which is right because this page is not admin-only.
- **The users list shows the quarantined state instead of the note count.** The
  page already refuses to render a number beside an *unassigned* account, for
  precisely this reason: a count reads as capacity the account has, when every
  tool call from it is refused before its body runs. A quarantined account is
  in the same position — assigned, indexed, and served by nothing — so it gets
  the same treatment with its own wording (`(quarantined — not served)`), its
  own reason from `_quarantine_display`, the "as at" timestamp, and the
  statement that **the index is retained**. The count is not deleted to make
  the display true.
- **The panel's on-demand reindex is a detection entry point (E4).**
  `_reindex_background` awaits `detect_root_overlaps("panel on-demand")` at its
  top, **before** `index_pass_lock` is taken, so Reindex Now, re-embed and
  reset embeddings cannot start a pass against an unchecked snapshot. It routes
  through the indexer's shared helper rather than calling `detect_and_publish`
  directly, so the "a detection failure must not abort the caller" handling is
  not re-implemented in a second place.

## The usage page's outcome column

- **A refused write must not look like a successful one.** A read-only
  credential calling `create_note` writes a `usage_logs` row shaped exactly
  like a successful write, so `/admin/usage` showed a read-only key apparently
  writing (#192). The request log now carries an **Outcome** column derived
  from the row's own markers.

- **The values are read as text and cast nowhere.** `recent_logs` selects
  `params->>'error'`, `params->>'error_type'` and `params->>'over_quota'`, all
  three as `text`. `params` is `JSONB` and `->>` always yields text; a
  `::boolean` on `over_quota` would 500 the whole page — for every user, until
  the offending row aged out of the window — the moment one row carried
  anything that is not `true`/`false`. `/admin/performance`'s unguarded casts
  are the standing example of that hazard and this query deliberately does not
  join it.

- **The mapping lives in the route, with a declared precedence** (`_usage_outcome`),
  because the three values can co-occur and a page that decided by whichever
  branch it happened to test first would rank them differently as the code
  moved:
  1. `tool_exception` → **failed**, showing the exception class. "It broke" and
     "it was refused" are different answers to "why did this call do nothing",
     and the colour tells them apart before the reason is read.
  2. any other `error` marker → **refused**, showing that marker
     (`permission_denied` is the one this change added).
  3. `over_quota == "true"` → **refused / over_quota**.
  4. any other non-empty `over_quota` value → **refused**, showing the value
     **raw**. `_tracked` writes that key only when it refuses and only as the
     JSON boolean `true`, so nothing the server produces reaches this branch —
     but a hand-edited or future-shaped row renders instead of taking the page
     down, and the operator is the one who can tell whether it is a bug.
  5. none of the above → no outcome; the row renders exactly as it always did.

- **Nothing is discarded between the query and the template.** All three
  selected values survive to the mapping, and the mapping shows what it does
  not recognise rather than dropping it — which is the difference between a row
  that looks ordinary and a row that says it is not.

## Flash messages

- **Panel flash messages ride the session, never the query string** (#138,
  A6 of #130). `src/control_panel/flash.py` holds the pair — `flash(request,
  message, kind)` before the redirect, `pop_flash` called from *one* place,
  `_panel_context`, so every panel render pops exactly once and a message is
  shown once and gone on reload. The old `?flash=` / `?error=` /
  `&flash_kind=err` was escaped by Jinja and so was never an XSS; the defect
  is that a crafted link chose what an authenticated admin read, on the page
  whose controls delete accounts, and survived every reload and re-share of
  the URL. Templates must render the context variable and never
  `request.query_params` — `tests/test_issue_138_session_flash.py` sweeps
  `src/control_panel`, `src/api` and `src/auth` for both halves. Untouched by
  this: the login and bootstrap pages, which pass `error` straight into the
  same response they render (no redirect, nothing in a URL), and every OAuth
  `error=` — those are protocol parameters on a redirect to the *client*, not
  panel flash. The pre-existing `flash_new_key` / `flash_key_error` /
  `flash_oauth_error` session entries keep their own dedicated render slots.

## Destructive actions take the index pass lock

- **Because the pre-warm holds `index_pass_lock`, the panel's destructive
  actions take it too.** `reset_embeddings` and `trigger_reembed` take
  `_pause_indexer()` — a **depth counter** whose first holder sets
  `indexer_paused` and whose last one clears it, because a bare
  `indexer_paused = False` in each handler's `finally` had the first of two
  overlapping actions unpause the indexer underneath the second, and the
  progress endpoint then reported "not paused" about a pause still in force
  (#130) — then `await session.close()` on the request's own session
  **before** waiting for the lock — a waiter that keeps its pooled connection
  deadlocks against a lock holder that needs one — and only then open a fresh
  session inside the lock (`_pass_lock_without_a_connection`). `trigger_reembed`
  also NULLs `notes_metadata.embedded_content_hash` in the same transaction as
  the `DELETE`: `embed_vault` selects on hash mismatch, so deleting vectors
  alone meant the reindex it spawns re-embedded nothing.

- **They also take the generation lock, and it is a different lock doing a
  different job** (#206). `acquire_generation_lock(fresh)` is the **first
  statement of the destructive transaction** in both `reset_embeddings` and
  `trigger_reembed` — before `SET LOCAL statement_timeout`, before the
  `DROP INDEX`, before the `DELETE`. `index_pass_lock` is **process-local**: it
  stops *this* container's pass and nobody else's. The reset workflow is
  deliberately a one-off `docker compose run` that reads the edited `.env` and
  works whether or not the service is up (#142), so the process it must exclude
  is usually a *different* one — and only a database-level lock can. Without
  it: the reset wipes the column and records the new fingerprint while the old
  container, mid-provider-call, comes back and stamps `embedded_content_hash`
  with old-model vectors under a fingerprint claiming the new model, silently
  and for ever. Both locks, in that order — advisory before any row or table
  lock, one direction everywhere, so the new lock cannot close a cycle with the
  row locks the pass already holds.

- **The wait is meant to be long, so no timeout is set over it.** Waiting here
  means an index pass in another container is in flight, and waiting for it to
  commit is the required behaviour rather than a stall. `SET LOCAL
  statement_timeout = '5min'` therefore comes *after* the acquisition, over the
  destructive DDL it was written for; putting the lock inside its scope would
  abort a legitimate wait and turn a correct interlock into an intermittent
  failure.

- **The fingerprint is recorded in that same locked transaction, and a failed
  record rolls the wipe back** (`_record_embedding_fingerprint`). This is the
  one write on these paths that is *not* instrumentation: it is the claim a
  later startup refuses on. Swallowing a failed record — the rule that rightly
  governs `_write_indexer_run` and the rotation cursor — would leave the stored
  value naming the **previous** configuration over rows about to be built under
  the new one, and every later startup silent about it. So the failure aborts
  the whole operation: the operator gets a flash error (a 500 with
  `{"status": "error"}` on the JSON branch), no reindex is spawned, and the
  vault keeps the vectors it had. Losing a reset is recoverable; keeping one
  under a lying fingerprint is not. `reset_embeddings` still invalidates the
  HNSW cache on that path — the DDL was transactional and the rollback undid
  it, so a re-probe costs one query and is never wrong.

- **HNSW creation on both reset paths is conditional on the configured
  dimension** and must stay so: pgvector refuses the index above 2000 dims, so
  an unconditional `CREATE INDEX` aborts the entire reset on such a deployment
  and leaves the operator with a wiped column and no index (#6).
  `trigger_reembed` creates no index at all, so the condition is vacuous there.

## Two coverage questions, and the dashboard answers both

- **The bar and the pending count are different questions and the bar was not
  redefined** (#201). `stats.embedding_pct` counts notes holding *at least one
  vector row* — "is this note represented at all". The pending count beside it
  counts notes whose vectors are **not current** — "is that representation the
  note as it stands now". They disagree during every embed backlog, and
  collapsing them into the stricter one would silently rewrite what every
  coverage figure an operator has ever read on this page meant. So the page
  shows both, and `embedding_pct` / `notes_with_embeddings` are untouched.

- **`_vectors_not_current()` is written once and called twice.** The predicate
  is `embedded_content_hash IS NULL OR embedded_content_hash IS DISTINCT FROM
  content_hash`. `IS DISTINCT FROM`, not `!=`: under `!=` a NULL
  `embedded_content_hash` yields NULL, a `WHERE` reads that as false, and every
  never-embedded note would count as *current* — the exact inversion of what
  the count is for. Its two callers are `dashboard()` and
  `/settings/reset-embeddings/progress`, and a second copy of the expression is
  precisely how the page and the poller come to disagree about what "pending"
  means.

- **The two callers differ in scope, deliberately, and only in scope.**
  `dashboard()` scopes both new counts by `_scope_user_id(user)` exactly as the
  coverage numbers directly above them are scoped. The progress endpoint is
  admin-only and **unscoped** — it is the poller behind a whole-database reset,
  so "how much is left" is a question about the whole table. Copying the
  poller's unscoped query onto the dashboard would show a regular user the
  entire database's backlog beside their own note count: another tenant's index
  state read as their own, on the one panel surface a non-admin can reach.

- **The progress endpoint's JSON is unchanged.** `pending` is now counted with
  the shared predicate and `embedded` derived as `total - pending`, rather than
  the reverse. The two queries are exact complements over this table
  (`content_hash` is `NOT NULL`, and `IS DISTINCT FROM` is total where `=` was
  three-valued), so every previously reported figure is the figure still
  reported under the same four keys.

- **Both counts render at zero.** An absent count is not evidence of absence:
  an operator cannot distinguish "no backlog" from "this build does not report
  one", which is the same reason the vector tools carry their stale count even
  when it is nought. Zero renders in the muted token and a non-zero truncation
  count in the warning one, so the row changes colour rather than changing
  shape — no new CSS values, `checks/token_coverage.py` stays green.

- **A pending count that does not shrink across passes is the operator-visible
  shape of two failures nothing else on this page surfaces**: a provider
  outage, which now marks the pass record but leaves coverage reading whatever
  it read yesterday, and a tenant whose embedding is repeatedly stopped at its
  per-pass budget, which is deliberately *not* written into the pass record's
  `error` because it is a decision rather than a fault.


## The last-admin guard

- **Every panel handler that can change `users.is_admin` / `users.is_active`
  takes `_lock_admin_guard(session)` before counting the remaining admins**
  (`src/control_panel/users.py` — `edit_user_submit` and `delete_user`, one
  shared `pg_advisory_xact_lock` key). The last-admin guard is a count
  followed by a write; without the lock two admins demoting each other
  concurrently both read "one other admin remains", both pass, and the panel
  is left with zero admins and no way back in through the UI. The lock is
  transaction-scoped, so **never commit between taking it and writing the
  flags** — that is what makes the check-then-act atomic. A new handler that
  flips either flag must take it too, and use the *same* constant: two keys
  do not exclude each other. **Immediately after the lock, both handlers
  re-read the acting admin's own `is_admin`/`is_active`
  (`_actor_still_privileged`) and refuse unless both are exactly True** —
  `require_admin_panel` authorised the request before the lock was requested,
  and the wait for that lock is precisely the window in which another admin's
  demotion of *this* actor commits; serializing the writes is no use if the
  loser of the race then performs the mutation anyway.

- **The guard's key now lives in `src/oauth/grants.py`, and its contention set
  is wider than the two admin handlers** (#197, #198). `ACCOUNT_GUARD_LOCK_KEY`
  and `lock_account_guard(session)` moved there because
  `src/control_panel/users.py` imports `src/control_panel/routes.py` and the
  reverse import is impossible, and `grants.py` is already this codebase's home
  for advisory-lock primitives (`USER_BOOTSTRAP_LOCK_KEY`,
  `lock_user_bootstrap`, `lock_grant`). `_lock_admin_guard` delegates to it
  with the **value unchanged** — it is a wire constant, and two builds holding
  different constants would not exclude each other for exactly the window of a
  rolling deploy. Three more callers now take the same key: the self-service
  password change, and **every session mint** through `start_session`. That is
  deliberate rather than incidental: a mint that does not serialize against the
  deactivating handlers can insert a live session row for an account an
  administrator has just disabled. Lock order stays acyclic — these handlers
  take the account guard and nothing else, the bootstrap takes
  `USER_BOOTSTRAP_LOCK_KEY`, the OAuth paths take bootstrap-then-grant, and no
  path takes two of them in opposing orders.

## The panel session is a row, not just a cookie (#198)

- **A signed cookie cannot be un-signed, which is why `logout` did not log
  anyone out.** `logout()` called `request.session.clear()` and nothing else;
  Starlette answers that with an expiring `Set-Cookie`, while the copy already
  taken stays a correctly-signed credential until its itsdangerous timestamp
  passes `session_max_age` — seven days. The verification on #198 replayed a
  pre-logout cookie against the container's installed Starlette and got the
  user back. This panel has **no CSP and will not get one** (see the top of
  this note), so "an XSS steals the cookie" is a live path rather than a
  theoretical one, and the panel that cookie reaches mints `readwrite` `omcp_`
  keys and approves OAuth grants. The only thing that can be revoked is a row,
  so `user_sessions` (migration 024) is the row and `src/auth/session.py` is
  its single implementation.

- **The table stores `sha256(sid)`, never `sid`.** The cookie carries a
  `secrets.token_urlsafe(32)` identifier under `sid`; `hash_session_id` is the
  one definition of the primary key derived from it. The identifier *is* a
  bearer credential for seven days, and a `pg_dump` is taken before every
  migration and kept thirty days — the invariant in
  [schema-and-migrations.md](schema-and-migrations.md) is that a dump holds
  hashes and no plaintext credential, and storing the identifier verbatim would
  make every retained dump a file full of live panel sessions. The digest is
  **unkeyed on purpose**: 256 bits of CSPRNG output has nothing to
  brute-force, and an HMAC under `SECRET_KEY` would make the table unreadable
  after a rotation an operator may need to perform.

- **`session_version` stays beside the registry rather than being replaced by
  it.** The registry is the per-session control; `users.session_version` is the
  account-wide one, is already load-bearing for a shipped requirement
  ("password reset invalidates consent session"), and is the one invalidator
  that still works if a registry write is lost. Both have to pass.

The lifecycle, in one table — one implementation per phase:

| Phase | Trigger | What happens | Where |
| --- | --- | --- | --- |
| **Mint** | login; bootstrap registration; the re-issue after a password change | **under the account guard**: re-read the user `FOR UPDATE` with `populate_existing=True`, require it to exist and be active, insert a row keyed on `sha256(sid)` with `expires_at = now + session_max_age` and `user_agent_hash`, **commit**, and only then write `sid` into the signed cookie | `start_session()`, three callers |
| **Validate** | every request that resolves a browser identity | the cookie must carry **both** `user_id` and `sid`; refused when the row is absent, revoked, expired, or `row.user_id` disagrees with the cookie's; then the existing user-exists / `is_active` / `session_version` checks. Every refusal clears the cookie **and records `panel_session_replay_refused`** with one of eight reasons | `get_active_session_user()`, reached from `require_user_panel`, `login_form`, `authorize_get`, `authorize_post` — the only four entry points |
| **Touch** | a `GET`/`HEAD` whose row is more than `SESSION_TOUCH_INTERVAL_SECONDS` stale | `last_seen_at = now()` on the **request's own** session, committed in the dependency before the handler runs; skipped on every other method and on any failure | `touch_session()` |
| **Revoke — one** | logout | `revoked_at` for that row, commit, cookie cleared | `logout` in `src/auth/routes.py` |
| **Revoke — all** | self-service password change; admin password reset; deactivation through the user edit form; soft delete | `revoked_at` for every unrevoked row of that user, **in the caller's transaction**, under the account guard | `change_password`; `reset_password`, `edit_user_submit`, `delete_user` |
| **Revoke — implicit** | permanent user delete | the database's `ON DELETE CASCADE` removes the rows; `User.sessions` declares `passive_deletes=True`, so that cascade is what fires rather than a per-row ORM delete | `delete_user` |
| **Purge** | every indexer tick, in **both** modes | `expires_at < cutoff AND (revoked_at IS NULL OR revoked_at < cutoff)`, `cutoff = now() - SESSION_PURGE_RETAIN_DAYS` | `cleanup_expired_tokens()` |

- **The mint commits and the revoke helpers do not; that asymmetry is the
  contract.** `get_session` neither commits nor rolls back, so an insert left
  to a caller's discretion is an insert that may never happen — and the cookie
  handed to the browser beside it would authenticate nothing, which is a hard
  logout loop on the very next request. Making `start_session` own its
  transaction makes "the row exists before the cookie leaves" a property of one
  function rather than of three call sites' discipline.
  `revoke_session` / `revoke_user_sessions` are the opposite by the same
  reasoning: every one of their callers holds the account guard, and the
  documented rule for that critical section is that nothing may commit between
  taking the lock and writing the flags it protects. A helper that committed
  would silently break the last-admin guard from inside. Both carry
  `AND revoked_at IS NULL`, so a second revocation does not rewrite a
  historical revocation time, and both return the row count so a caller can
  record it without a second query.

- **The mint runs *inside* the guard, not after it.** A mint placed after its
  caller's guard was released can be overtaken by an administrator's
  deactivation and will then insert a **live row for a just-disabled account**.
  Validation refuses that row while `is_active` is false, which hides it — and
  the day the account is reactivated it becomes a working credential nobody
  granted and nobody saw. `populate_existing=True` on the locked re-read is
  load-bearing for the reason [oauth-and-grants.md](oauth-and-grants.md)
  already records: a `SELECT … FOR UPDATE` whose row is in the session's
  identity map hands back the loaded object's pre-lock attribute values, and
  every caller arrives with that row already loaded. A refusal is not an error
  a caller recovers from — the user is simply not signed in — so
  `start_session` rolls back to release the guard and returns `None`. It also
  `clear()`s the cookie before writing the new session into it, which is
  session-fixation hygiene and means **a caller must flash after the mint,
  never before**, or the message is thrown away with the old cookie.

- **A mint is bound to the credential generation that authorized it, not just
  to `is_active`.** `start_session` takes a required `expected_session_version`
  and refuses when the locked re-read disagrees. Checking the active flag alone
  left the hole the guard was supposed to close: an administrator's reset
  writes a new hash, **bumps `session_version`** and revokes every row it can
  see, and it can commit in the window between a caller verifying a credential
  and the mint taking the lock. The account is active throughout, so a mint
  that only asked that question would insert a live row *after* the reset's
  sweep and copy the new version into the cookie — handing the **superseded**
  password a fully valid session and defeating both invalidators at once. The
  three call sites each pass the generation they hold: `login_submit` the
  version `verify_password` ran against, `register_submit` the row it just
  created, `change_password` the bumped version it just committed. The same
  window is why the post-change re-issue, which runs in a *second* transaction,
  needs it too.

- **`panel_login_succeeded` is emitted after the mint, not after the
  `last_login_at` commit.** That commit was never what made somebody signed in
  — the session row is — so a mint refused by the race above would otherwise
  have left a record asserting a sign-in that did not happen. A refused mint
  records `panel_login_failed` with reason `session_mint_refused` instead: a
  correct credential that did not sign in must not be silently absent from the
  log.

- **The touch never opens a second `AsyncSession`, and only ever runs on a safe
  method.** A request holding two connection-pool leases halves a pool that
  tops out at `pool_size + max_overflow` = fifteen; the sixteenth caller
  anywhere in the process — an MCP tool call, `/token`, the indexer — waits
  `pool_timeout` and then 500s, so a telemetry field must not be able to take
  the server down. `GET`/`HEAD` only, and committed **inside the dependency
  before the handler body runs**, closes a second failure: the `UPDATE` takes a
  row lock on the actor's session row, and a mutating panel handler then waits
  on the account-guard advisory lock, which an administrator may be holding
  while revoking that same actor's sessions — A waits on the advisory lock, B
  waits on A's row lock. Committing before any handler starts releases the row
  lock before any advisory lock can be requested, and on a safe request there
  is no partial handler work a commit could publish. Any failure rolls back,
  logs a WARNING carrying the exception's class name, and serves the page:
  `last_seen_at` is telemetry throttled to once a minute and nothing authorizes
  on it.

  Failure reporting captures the actor's `user_id` and route as primitives
  before database work. A failed commit followed by a failed rollback can
  expire the ORM row; reading its attributes to log that failure would attempt
  another database refresh and fail the request. Both failure records use the
  captured values and carry no session identifier, including its stored hash.

- **`user_agent_hash` is forensic and is never an authorization input.** It is
  useful when reconstructing an incident. As a *binding* it is bad: whoever
  stole the cookie also has the header, so it stops nobody, and enforcing it
  signs real users out on every browser auto-update — training them to
  re-authenticate after an unexplained logout, which is the habit phishing
  depends on. The same argument rules out IP binding.

- **Every refusal records its reason, all eight of them.** Clearing the cookie
  signs a browser out mid-session, so an operator asked "why was this user
  logged out?" must be able to answer it from the log for every branch, not
  five of them. `REPLAY_REFUSAL_REASONS` is the closed vocabulary —
  `no_session_id`, `unknown_session`, `revoked_session`, `expired_session`,
  `user_mismatch`, `user_missing`, `user_inactive`, `version_mismatch` — and
  the last three were the ones that used to clear silently. `version_mismatch`
  in particular is the account-wide invalidator working as designed after a
  password reset, and it was the refusal with no record at all. The one case
  that records nothing is a cookie with no `user_id`: that is an anonymous
  request, not a refusal. A test closes the vocabulary in both directions and
  asserts `cookie.clear()` and `_replay_refused(` occur the same number of
  times in the validator.

- **`login_form` must resolve through `get_active_session_user`, never read
  `request.session["user_id"]` raw.** That raw read was a fifth validation
  entry point, and with a revoked cookie it is an infinite loop: the login page
  saw a user id and redirected to `/admin/`, `require_user_panel` refused the
  session and redirected back, and nobody holding a dead cookie could reach the
  login page to fix it. "Four entry points, one implementation" is the property
  worth re-checking after any change here; a fifth reader of the cookie is the
  shape of this defect returning. (`src/csrf.py`'s nonce read is not an
  identity decision and is not one of them.)

- **A logout that cannot revoke still signs the browser out, and records a
  class name only.** If the `UPDATE` or its commit raises, `logout` attempts a
  rollback — **itself guarded**, because a failing rollback escaping would turn
  the sign-out into a 500 — records an ERROR, and still clears the cookie and
  redirects. Failing closed here would leave the user signed in *and* the
  cookie alive, which is strictly worse than the state we are leaving: clearing
  it removes this browser's copy, the common case of somebody walking away from
  a shared machine, and the replay window then survives only for a copy already
  taken, which is what the record is for. The record carries the exception's
  **class name only** — never `str(exc)`, never `exc_info` — because SQLAlchemy
  renders the failing statement *and its bound parameters* into the message,
  the engine does not set `hide_parameters`, and one of those parameters on
  this path is the stored session hash, i.e. the name of a specific live
  session. Every event this surface emits is declared in the catalogue in
  [security-event-logging.md](security-event-logging.md); where a session must
  be named it is by `token_tag`, never by the identifier and never by its
  stored SHA-256.

- **Pre-registry cookies are refused, not grandfathered, and the first deploy
  therefore signs everyone out once.** A correctly-signed cookie carrying
  `user_id` and no `sid` is refused with `reason="no_session_id"`.
  Grandfathering would keep #198's replay window open for a further seven days
  after the fix shipped; the cost is one forced login per live session at the
  deploy, and production has two users.

- **The purge takes the *later* of `expires_at` and `revoked_at`, unlike the
  OAuth half of the same function.** The OAuth argument — a token can only be
  revoked while it exists, so `R <= expires_at` — does not transfer: an
  administrative reset revokes every unrevoked row of a user *including
  already-expired ones*, and such a row is immediately past its expiry, so an
  expiry-only rule would delete the record of a revocation minutes after an
  operator performed it. That is the #64 blank space in a new table. Both
  windows are `ge=1` at settings construction, so a zero retention (which would
  delete a revocation the moment it was made) or a zero touch interval (which
  would turn a throttled hint into a write on every request) stops the
  container instead of degrading it silently.

- **Accepted, and stated rather than hidden:** revocation takes effect at the
  *next* request and one already in flight completes (the posture OAuth
  revocation already documents); `last_seen_at` may be up to a minute stale and
  is never recorded for a session that only ever POSTs; and a stolen cookie
  still works until logout, expiry or an account event — this change makes
  those three effective, it does not detect theft.

## The account page and the self-service password change (#197)

- **A user can rotate their own password; the admin reset stays the recovery
  path.** Before this, every route that wrote `password_hash` outside bootstrap
  was on the admin router, so a non-admin who suspected their password was
  compromised had to ask an administrator — who then knew the replacement.
  `GET /admin/account` and `POST /admin/account/password` sit on the panel
  router behind `require_user_panel`, **not** `require_admin_panel`, and take
  the router-wide `verify_csrf`.

- **Both methods 404 in single-user mode.** There is no `users` row and no
  local password there — the panel's identity is the sentinel and the
  credential is Traefik's OAuth chain — so the page's only content would be a
  form that cannot exist, and the sidebar entry is already gated on
  `multi_user_mode`. One rule for both methods (`_require_account_route`) is
  also one thing to test. A 404 rather than a 403: a route that can do nothing
  here should not advertise that it exists elsewhere.

- **The handler verifies against a row it re-read under the guard, never
  against the `User` a dependency loaded.** Between the dependency's read and
  the write, an administrator's reset or deactivation can commit — and the
  self-change would then overwrite the administrator's new hash with one
  authorised by a stale verification, restoring access that had just been
  removed. So: `lock_account_guard(session)`, a `SELECT … FOR UPDATE` with
  **`execution_options(populate_existing=True)`** (without which SQLAlchemy
  hands back the loaded object's pre-lock attribute values and the re-read
  proves nothing), a re-check that the row exists and `is_active` is exactly
  true, and only then `verify_password` against the **freshly read** hash.

- **Success is one transaction and then a second, guarded one.** The new hash,
  `session_version += 1` and `revoke_user_sessions(session, user.id)` —
  **every** row of that user, this browser's included — commit together, which
  releases the lock. Only then does `start_session` run, re-taking the guard
  and re-checking the account, because the guard is released between the two
  and an administrator can deactivate in exactly that gap. The user-visible
  effect is the one asked for: other devices signed out, this one still signed
  in under a new identifier. **The password change is the durable half.** A
  re-issue that refuses or raises signs this browser out and is recorded; it
  never rolls the change back.

- **Two independent throttles, not one composite key.**
  `@limiter.limit("5/minute", key_func=session_user_key)` **and**
  `@limiter.limit("5/minute", key_func=get_remote_address)`, stacked. Neither
  subsumes the other: an account-keyed limit bounds guessing against one
  account however many addresses an attacker rotates through, an address-keyed
  limit bounds one address walking many accounts, and a single `(ip, user)` key
  does neither — it hands out a fresh allowance per address. Successes count
  against both, so the allowance cannot be drained by guessing.
  `session_user_key` must stay **synchronous** (slowapi computes the key before
  the handler and cannot await, and reading the body there would consume the
  stream the handler needs) and must degrade to `ANONYMOUS_SESSION_KEY` where
  no `SessionMiddleware` is mounted, or a throttled route 500s for every
  caller. The cookie's `user_id` is a bucket name, never an authorization
  decision — the validator refuses the request a moment later if the row behind
  it is dead. Both buckets are per-process, in slowapi's in-memory storage, and
  reset on restart; one uvicorn worker makes that per-server today.

- **The rate-limited request is the one deliberate exception to the
  flash-and-303 rule.** Every refusal that reaches the handler flashes on the
  session and redirects 303 to a bare `/admin/account`, never a query parameter
  (#138). A request either limit rejects never reaches the handler at all:
  slowapi's `RateLimitExceeded` is answered by the application-wide
  `_rate_limit_exceeded_handler` with its own JSON 429, and **that response
  stands**. Wrapping it would mean either duplicating the limiter's decision
  inside the handler — where it is no longer a limit but a second, divergent
  counter — or replacing a process-wide error handler for one route. The cost
  is that the sixth attempt in a minute renders as JSON rather than as the
  account page with a flash.

- **One constant message for both credential refusals.** A wrong current
  password and a new password that is already the current one answer with the
  same `CREDENTIAL_REFUSAL` text, which names both possibilities. A message
  that said *which* check failed would say something about the stored password;
  one naming only the wrong-password case would tell an honest reuser something
  untrue, since the reuse branch is reached *after* the current password
  verified. The `reason` that separates them lives in the event record only.

- **One password policy, one constant, four setters.** `MIN_PASSWORD_LENGTH =
  12` and `validate_new_password(new, confirm=None)` live in
  `src/auth/passwords.py`; the self-service handler, `register_submit`, the
  administrator `reset_password` and the administrator `create_user` all route
  through them — the last three were at 8, and `create_user` handed form input
  straight to `hash_password`. Length is measured in **characters**, there are
  no composition rules (they push people to `Password1!`), there is no maximum
  and no forced rotation: nothing re-checks policy at login, so existing
  accounts keep working and are never re-checked against the new minimum. The
  validator also rejects an embedded **NUL**, which is not tidiness:
  `hash_password` *raises* on one, preserving passlib's semantics, so a NUL in
  a password field was a latent 500 on four handlers and is now a form error.
  **The 72-byte truncation and the NUL rejection themselves are untouched** —
  every stored hash depends on them; read the module docstring before
  "fixing" either.

- **The four forms render `MIN_PASSWORD_LENGTH`; none of them writes a
  number.** `register.html`, `account.html`, `users.html` and `user_edit.html`
  take `min_password_length` from their handler's context and put it in both
  `minlength` and the hint. That is not tidiness either: the server moved to
  twelve while three of those templates went on promising eight, so the browser
  accepted precisely what the handler would then refuse. A literal that happens
  to equal today's constant is still a literal, and the test asserts the
  absence of any numeric `minlength` on these forms **and** that each handler
  actually passes the key — a template reading it off a context that has none
  renders `minlength=""`, which is silently no minimum at all.
