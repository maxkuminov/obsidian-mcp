## 1. Implementation

- [x] 1.1 `src/auth/passwords.py`: reimplement on `bcrypt` (`hashpw(gensalt())`, `checkpw`), UTF-8 encode + truncate at 72 bytes in both functions, module docstring explaining the truncation and legacy compatibility.
- [x] 1.2 `requirements.txt`: remove `passlib[bcrypt]` and `bcrypt<4.1` (+ comment); add `bcrypt==5.0.0` with a one-line comment.

## 2. Tests

- [x] 2.1 `tests/test_passwords.py`: round trip; wrong password; legacy `$2b$12$` fixture (generate one now under the current passlib+bcrypt 4.0.1 venv and paste the literal with its plaintext); >72-byte password and its 72-byte-prefix twin both verify; non-ASCII password; `hash_password` output starts with `$2b$12$`.
- [x] 2.2 One route-level test (register + login via the app's routes with the real hasher, no monkeypatch of `hash_password`); keep the existing monkeypatched test.

## 3. Verification & ship

- [ ] 3.1 `openspec validate replace-passlib-with-bcrypt --strict`; full suite green; `pip-audit -r requirements.txt` clean.
- [ ] 3.2 `openspec-verifier`; adversarial Codex (auth). Iterate to no BLOCKER/MAJOR.
- [ ] 3.3 `make deploy`; post-deploy: log in to the panel with the test user (or `POST /admin/auth/login`), change a password and back; record what was exercised.
- [ ] 3.4 Archive, PR, merge, push.
