## Context

`src/auth/passwords.py` wraps `passlib.context.CryptContext(schemes=["bcrypt"])`.
`hash_password` is called at `src/auth/routes.py:260` (registration) and
`src/control_panel/users.py:193,400` (create user, set password);
`verify_password` at `src/auth/routes.py:138` (login). Stored hashes are
`$2b$12$` (verified against a live-format sample under bcrypt 4.0.1).
`bcrypt` 5.0.0 raises on >72-byte input where 4.x truncated silently; passlib
1.7.4's backend probe trips that on init. `tests/test_security_review_followups.py:97`
monkeypatches `hash_password`, so the suite passes with a broken hasher.

## Goals / Non-Goals

**Goals:** remove passlib; keep every existing hash valid; make the 72-byte
rule explicit and tested; make the suite fail if the hasher breaks.

**Non-Goals:** changing cost factor, argon2, password policy changes,
re-hash-on-login.

## Decisions

1. **Direct `bcrypt`, cost 12 (library default), `$2b$`.** Same output family
   as passlib produced; `checkpw` handles `$2a$`/`$2b$`/`$2y$` prefixes.
2. **Truncate at 72 bytes in both hash and verify**, on the UTF-8 encoding
   (`plain.encode("utf-8")[:72]`), because that is what passlib did for the
   hashes already in the database; a stored hash of a 100-byte password was
   computed over its first 72 bytes and must keep verifying. Documented in a
   module docstring with the consequence (two passwords identical in their
   first 72 bytes are equivalent). *Alternative:* enforce a max length in the
   policy and reject >72 — would lock out any existing user with such a
   password; rejected.
3. **Pin `bcrypt==5.0.0`** explicitly (no floating).
4. **One route-level test uses the real hasher** (register/login round trip
   through the app) so the monkeypatch-only gap is closed.
5. **Preserve passlib's two edge-case behaviours explicitly:** reject
   NUL-containing passwords (`hash_password` raises, `verify_password` returns
   False before bcrypt is called — raw `bcrypt` 5.x accepts them, and the old C
   bcrypt truncated at the first NUL, so silently accepting them would cut a
   password's entropy short), and fail closed on a malformed stored hash with a
   single identifier-free `logger.warning` rather than a 500 on the login route.

## Risks / Trade-offs

- [Existing hash not verifiable] → fixture test with a passlib-generated
  `$2b$12$` hash; post-deploy manual login.
- [Behavioural change for >72-byte passwords] → identical to before by
  construction (truncation preserved).

## Migration Plan

Deploy; log in once; change a password and back. Rollback: previous image.
