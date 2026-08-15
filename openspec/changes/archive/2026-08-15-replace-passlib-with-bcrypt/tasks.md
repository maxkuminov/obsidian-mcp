## 1. Implementation

- [x] 1.1 `src/auth/passwords.py`: reimplement on `bcrypt` (`hashpw(gensalt())`, `checkpw`), UTF-8 encode + truncate at 72 bytes in both functions, module docstring explaining the truncation and legacy compatibility.
- [x] 1.2 `requirements.txt`: remove `passlib[bcrypt]` and `bcrypt<4.1` (+ comment); add `bcrypt==5.0.0` with a one-line comment.

## 2. Tests

- [x] 2.1 `tests/test_passwords.py`: round trip; wrong password; legacy `$2b$12$` fixture (generate one now under the current passlib+bcrypt 4.0.1 venv and paste the literal with its plaintext); >72-byte password and its 72-byte-prefix twin both verify; non-ASCII password; `hash_password` output starts with `$2b$12$`.
- [x] 2.2 One route-level test (register + login via the app's routes with the real hasher, no monkeypatch of `hash_password`); keep the existing monkeypatched test.

- [x] 2.3 Edge cases: NUL-containing password (`hash_password` raises, `verify_password` False, and False against a legitimate hash); malformed stored hash returns False and emits the warning (asserted via `caplog`); `None` stored hash returns False. `tests/test_auth_routes_real_hasher.py` gets an autouse fixture resetting slowapi's limiter storage around each test so rate-limit state cannot leak between tests.

## 3. Verification & ship

- [x] 3.1 `openspec validate replace-passlib-with-bcrypt --strict`; full suite green; `pip-audit -r requirements.txt` clean.
- [x] 3.2 `openspec-verifier`; adversarial Codex (auth). Iterate to no BLOCKER/MAJOR.
- [ ] 3.3 Deploy with the next `make deploy` after merge; post-deploy the operator logs in to the panel and changes a password and back (needs real credentials — deliberately not automated).
- [ ] 3.4 Archive, PR, merge, push.
