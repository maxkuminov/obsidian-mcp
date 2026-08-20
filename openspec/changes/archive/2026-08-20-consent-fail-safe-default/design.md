## Context

The consent screen has two jobs that #62 accidentally fused: **telling the
user what was asked for** and **choosing what gets submitted by default**. On
an HTML form those are the same attribute — `checked` — which is why a
disclosure fix turned into an authorization change.

Splitting them is the whole design: the preselect becomes a constant, the
disclosure becomes prose.

## Decisions

### The preselect is a constant, not a policy

Considered and rejected: keep the requested-scope preselect but restrict it to
clients whose write capability was established through an authenticated
administrative process (the alternative #63 names). It is defensible, but it
needs a notion of "administratively vetted client" that does not exist in the
data model — `oauth_clients` has no provenance column, and every client on the
live deployment arrived through unauthenticated DCR. Adding that distinction
is a larger change with its own failure mode (a mis-set flag re-opens the
hole silently), and #63's author chose the fail-safe option. So: `checked` is
hardcoded on "Read only" in the template, not passed in from the route.

Hardcoding it in the *template* rather than passing a `preselect_write=False`
is deliberate — a context variable is a thing a future edit can start
computing again. There is nothing to recompute.

### `requested_write` is ungated by the registered scope

Before, `requested_write = "readwrite" in scope_parts and client_can_write` —
the `and` existed because the value drove the radio and a radio that is not
rendered cannot be checked. Now the value is display-only, and the case the
`and` was suppressing is exactly the one worth showing: a client asking for
more than it may hold. So `requested_write` is what the client asked for, and
`write_unavailable = requested_write and not client_can_write` carries the
mismatch.

The value is still derived from `scope` **after** `_validate_scope`, so it can
only ever be one of the three known scope tokens; the template renders fixed
prose from a boolean and never echoes the raw parameter.

### The preselect is only fail-safe with `autocomplete="off"`

A markup default is not the last word on what a radio is checked. Firefox
restores a control's *dynamic* checked state across page loads — session
history, reload, back/forward — in preference to the markup, unless the
control opts out. So a user who once selected "Read + Write" would find that
radio re-checked the next time the same `/authorize` URL was visited (a client
is free to reuse its `state` and PKCE challenge), and an unchanged **Approve**
would post `readwrite` — after the earlier grant had been revoked or
downgraded, which is precisely the state in which nobody intends to re-grant
it. That is the #63 one-click write grant re-entering through the browser
instead of through the query param, so hardcoding `checked` closes only half
the hole on its own.

`autocomplete="off"` therefore goes on **both** the `<form>` and each
`name="scope"` radio. The per-control attribute is the one that actually
governs restoration; the form-level one is the blanket that covers any control
added later without someone remembering this note. It is asserted on both, for
the write-capable client (two radios) and the read-only client (one).

### `:has()` rules stand alone

CSS drops an entire rule when any selector in its selector list fails to
parse, so grouping `.scope-option:has(…)` with a selector that must keep
working would take that one down on a browser without `:has()`. The two
highlight rules are therefore separate blocks whose only loss on such a
browser is the highlight itself — the native radio dot (`accent-color`)
remains the baseline signal. `tests/test_authorize_get_scope_preselect.py`
asserts the isolation, not just the presence of the rule.

### Consent tests go through a real request

`scope: str = Query("read")` cannot be exercised by calling `authorize_get`
directly: the default Python sees is a `Query` object, not the string FastAPI
resolves it to, which is why the previous test passed `scope="read"`
explicitly and never reached the default (#65 gap 3). The rewritten module
mounts the router on a bare `FastAPI` app and drives it with `TestClient`,
patching only `async_session` and `settings.multi_user_mode`, so omitting the
parameter takes the real default path.

Every behavioural test in the module was checked by stashing `src/` and
re-running: 7 of 8 fail against the pre-change tree. The eighth
(`test_write_capable_client_is_not_told_write_is_unavailable`) is the negative
complement of the disclosure test — it pins that the "not available" notice
keys off the *registered* scope — and passes either way by construction.

## Risks

- **This is a deliberate UX regression for the write case.** A user whose
  connector legitimately wants write must now click the second radio every
  time. That cost is the point; the mitigation is that the screen says, in
  prose, both what was requested and that Read only is preselected.
- A client that treats a narrower grant as fatal will now hit that path more
  often. It already could — `_clamp_scope` has always been free to narrow —
  and the disclosure makes the cause visible to the user instead of silent.
