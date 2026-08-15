## ADDED Requirements

### Requirement: Password hashing uses bcrypt directly and preserves existing hashes

`hash_password` SHALL produce a modular-crypt bcrypt hash (`$2b$`, cost 12) using the `bcrypt` library directly, and `verify_password` SHALL verify with `bcrypt.checkpw`. Hashes produced by the previous passlib-based implementation SHALL verify unchanged. Both functions SHALL operate on the UTF-8 encoding of the password truncated to 72 bytes, preserving the previous implementation's semantics so that no stored hash becomes unverifiable; this truncation SHALL be documented in the module. `passlib` SHALL NOT be a dependency.

#### Scenario: Round trip

- **WHEN** a password is hashed and then verified
- **THEN** verification SHALL succeed for the same password and fail for a different one

#### Scenario: Legacy hash verifies

- **WHEN** a `$2b$12$` hash produced by the passlib-era implementation is verified with its original password
- **THEN** verification SHALL succeed

#### Scenario: Long passwords

- **WHEN** a password longer than 72 UTF-8 bytes is hashed
- **THEN** hashing SHALL succeed, verification with the same password SHALL succeed, and verification with a password identical in its first 72 bytes SHALL also succeed (documented truncation semantics)

#### Scenario: Suite exercises the real hasher

- **WHEN** the login route test suite runs
- **THEN** at least one test SHALL register/log in through the real `hash_password`/`verify_password` (not a monkeypatch), so a broken hasher fails the suite
