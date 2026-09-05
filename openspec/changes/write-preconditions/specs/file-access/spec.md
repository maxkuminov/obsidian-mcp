## ADDED Requirements

### Requirement: read_file exposes a file's content hash without wrapping its content

`read_file` SHALL make a file's `content_hash` — the digest defined by the `vault-write` capability's "The write-precondition digest is defined in exactly one place" requirement — available through server-controlled response regions only, and SHALL NOT introduce any header, trailer, or delimiter around the bytes a text read returns.

Two surfaces, and no third:

- a **base64** result SHALL carry `content_hash` in the labelled header it already emits ahead of its opaque body;
- `read_file(path, hash_only=true)` SHALL return a metadata-only result — the path, the size in bytes, the MIME type and the `content_hash` — and **no file content at all**.

Argument precedence SHALL be fixed and documented, because two validations now compete: `encoding` is validated **first**, so an invalid `encoding` is refused whatever `hash_only` says; then `hash_only` against `offset` and `limit` — supplying either alongside `hash_only=true` SHALL be refused rather than silently ignored; then the existing `offset`/`limit` range checks. With `hash_only=true` a *valid* `encoding` SHALL have no effect, because the digest is over raw bytes in every case, and the docstring SHALL say so rather than leaving it to be inferred.

A text result SHALL remain exactly what it is today: the file's decoded text with nothing added. An envelope around note-controlled bytes is the forgery class the structured-read work removed, and it SHALL NOT be reintroduced here under any flag. `read_file` is also the byte-exact route for a note's frontmatter block, which `read_note` returns only LF-normalized.

#### Scenario: Base64 header carries the hash

- **WHEN** `read_file` returns a base64 result
- **THEN** the header SHALL include the file's `content_hash` alongside the existing encoding, MIME, byte-count and path lines

#### Scenario: hash_only returns metadata and no content

- **WHEN** `read_file(path, hash_only=true)` is invoked on a text file
- **THEN** the result SHALL carry the path, size, MIME type and `content_hash`, and SHALL NOT carry any of the file's bytes

#### Scenario: A text read is unchanged

- **WHEN** `read_file(path, encoding="text")` is invoked without `hash_only`
- **THEN** the result SHALL be exactly the file's decoded text as today, with no header, no trailer and no hash

#### Scenario: hash_only with a window is refused

- **WHEN** `read_file(path, hash_only=true, offset=100)` is invoked
- **THEN** the tool SHALL return an error stating that the two cannot be combined, rather than returning a hash while ignoring the window

#### Scenario: An invalid encoding is refused before hash_only is considered

- **WHEN** `read_file(path, encoding="utf-7", hash_only=true)` is invoked
- **THEN** the tool SHALL return the existing invalid-encoding error, not a hash

### Requirement: write_file honours an optional expected_hash and reports what it wrote

`write_file` SHALL accept an optional `expected_hash` that binds an `overwrite=true` call to the incumbent file's `content_hash`, and SHALL report the resulting file's `content_hash` in its success message.

Behaviour follows the `vault-write` capability's precondition requirements: omitted, the call is the unconditional replace it is today, reading nothing; supplied with `overwrite=true` on a file that exists, the tool reads the incumbent through the descriptor it already validated, refuses on a mismatch with `stale_precondition` naming the current hash and writing nothing, and — on a match — also passes those bytes as the pre-publication comparison so both windows close together; supplied with `overwrite=false`, or with `overwrite=true` on a path that does not exist, the call is a `no_incumbent` refusal and **nothing is created**. An incumbent too large to read within `MAX_FILE_READ_BYTES` SHALL be a refusal naming the cap, never an unguarded write.

For the guarded call to reach the pre-publication comparison at all, the shared raw-byte publish helpers SHALL accept the same optional expectation the text helpers already accept, defaulting to none.

#### Scenario: A guarded overwrite of a changed file refuses

- **WHEN** `write_file(path, content, overwrite=true, expected_hash=…)` is invoked and the file's bytes have changed since the hash was obtained
- **THEN** the tool SHALL refuse with `stale_precondition` naming the file's current `content_hash`, and the file SHALL be unchanged

#### Scenario: A guarded overwrite of an unchanged file succeeds and reports its hash

- **WHEN** the same call is made with a matching hash
- **THEN** the write SHALL succeed and the result SHALL report the `content_hash` of the bytes just written

#### Scenario: An unguarded overwrite is unchanged

- **WHEN** `write_file(path, content, overwrite=true)` is invoked with no `expected_hash` and the deployment does not require one
- **THEN** the tool SHALL replace the file unconditionally, exactly as today, and SHALL NOT read the incumbent

#### Scenario: A precondition on a no-clobber write is refused

- **WHEN** `write_file(path, content, expected_hash=…)` is invoked with `overwrite=false`
- **THEN** the tool SHALL return a `no_incumbent` refusal and SHALL NOT create the file

#### Scenario: A guarded overwrite of an absent path creates nothing

- **WHEN** `write_file(path, content, overwrite=true, expected_hash=…)` names a path with no file at it
- **THEN** the tool SHALL return a `no_incumbent` refusal and SHALL NOT create the file, because the caller asserted it was replacing something

#### Scenario: The raw-byte helper carries an expectation only when asked

- **WHEN** an existing caller publishes raw bytes through the shared helper without an expectation
- **THEN** the publication SHALL behave exactly as it does today
