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

- **A new page has to be added to the sweep's template list, and the sweep
  has to be pointed at the templates.** `checks/literal_sweep.py` in the
  light-mode change (now under `openspec/changes/archive/`) is what proves
  "no color literal outside a token definition", but it scans a *recorded*
  list — `colorscan.PANEL_TEMPLATES` — which is the set of templates that
  existed when the sweep was written, and it resolves the templates directory
  from `__file__` assuming the change still sits at
  `openspec/changes/<id>/checks/`. Archiving moved it one level deeper, so an
  unmodified run now finds no files and reports **zero declarations, zero
  literals, exit 0** — a clean bill of health for a directory it never read.
  Run it from a wrapper that repoints `colorscan.TEMPLATE_DIR` at
  `src/control_panel/templates` and appends the new page (and
  `performance.html`, added by #160, which is not in the recorded list
  either), and assert the scan actually saw a nonzero number of declarations
  before believing the result. Pages added since: `performance.html` (#160),
  `search_analytics.html` (#161), `health.html` (#163) — all token-only,
  verified that way.


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

- **Errors live in a `deque(maxlen=100)` on the root logger and nowhere else.**
  Container logs rotate with the container, so the errors from before the last
  deploy — usually the ones worth reading — are gone. `src/services/error_log.py`
  attaches at ERROR in the lifespan *before* the sandbox-mode branch returns, so
  a process that dies in a startup guard still has the record. Only four
  strings per entry are kept, never the `LogRecord`: a record holds `exc_info`,
  which holds a traceback, which holds every frame's locals, and a hundred of
  those is a bounded buffer of unbounded object graphs. `emit` cannot raise —
  it runs inside every `logger.error(...)` in the process, including the ones
  in exception handlers.

- **The observation window is rendered next to the count, always.** "No errors"
  on its own reads as a claim about the server; what it means is "this process
  has not failed since it started", and a container restarted two minutes ago
  has an empty buffer for reasons unrelated to health. Same reason the copy
  points at `make logs` for anything older or fuller.

- **Errors and backup age are admin-only; the pass history is scoped.** The run
  list follows the performance page's rule (an admin sees every pass, a regular
  user only their own, and deliberately none of the ownerless single-user or
  global ones). The other two sections have **no owner to scope by** — the ring
  buffer holds whatever the process logged, other tenants' paths and identifiers
  included, and a backup covers the whole database — so they are gated on
  `is_admin` rather than filtered, and a non-admin gets a page of their own run
  history with a line saying why.

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
