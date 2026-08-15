## Why

Panel/OAuth user passwords are hashed through `passlib` 1.7.4 (unmaintained
since 2020) with a `bcrypt<4.1` pin that exists only because passlib's backend
probe breaks on newer bcrypt. That pin has now expired as a workaround: with
`bcrypt` 5.x installed, `CryptContext` raises `ValueError: password cannot be
longer than 72 bytes` during backend initialisation, so **every login and
every password change fails** — and the test suite does not notice, because
the only relevant test monkeypatches `hash_password`. The next unrelated
dependency refresh that lets bcrypt float would take down authentication.
`passlib` is used in exactly one 11-line module; replacing it with direct
`bcrypt` calls removes a dead dependency and the load-bearing pin.

## What Changes

- `src/auth/passwords.py` reimplements `hash_password` / `verify_password`
  on `bcrypt` directly (`bcrypt.hashpw` with `bcrypt.gensalt()` at the
  default cost 12; `bcrypt.checkpw`). Existing hashes are plain modular-crypt
  `$2b$12$…`, produced by passlib's bcrypt backend, and verify unchanged — no
  re-hash, no migration, no user-visible change.
- **72-byte handling made explicit:** passlib silently truncated passwords to
  72 bytes; bcrypt 5 raises. To keep every existing hash verifiable, both
  functions truncate the UTF-8 encoding at 72 bytes before calling bcrypt
  (documented in the module). Password *policy* is unchanged (min 8, no max).
- `requirements.txt`: drop `passlib[bcrypt]` and the `bcrypt<4.1` pin (and
  its comment); pin `bcrypt==5.0.0`.
- Tests: real round-trip (`hash` → `verify` true, wrong password false),
  a passlib-era `$2b$12$` fixture hash verifies, a >72-byte password hashes
  and verifies (and a variant differing only after byte 72 also verifies —
  the documented truncation semantics), non-ASCII passwords, and a
  `secrets.compare_digest`-style timing property is not required (bcrypt's
  `checkpw` is constant-time on the hash). Remove the monkeypatch-only
  coverage gap by adding one login-route test that goes through the real
  hasher.

## Capabilities

### New Capabilities

- `panel-password-hashing`: the password hashing contract for panel/OAuth
  users (algorithm, compatibility with existing hashes, 72-byte semantics).

### Modified Capabilities

_None._

## Impact

- `src/auth/passwords.py`, `requirements.txt`, tests. Callers
  (`src/auth/routes.py`, `src/control_panel/users.py`) unchanged.
- Auth code → adversarial-Codex pass per `CLAUDE.md`.
- Deploy: no migration; existing hashes verify. Post-deploy: log in to the
  panel once (or exercise `POST /admin/auth/login` with the test user) and
  change a password back and forth.
