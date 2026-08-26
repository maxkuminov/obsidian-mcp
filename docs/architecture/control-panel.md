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
