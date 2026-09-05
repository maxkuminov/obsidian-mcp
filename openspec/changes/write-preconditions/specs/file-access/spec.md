## ADDED Requirements

### Requirement: read_file exposes a file's content hash without wrapping its content

`read_file` SHALL make a file's `content_hash` — the digest defined by the `vault-write` capability's "The write-precondition digest is defined in exactly one place" requirement — available through server-controlled response regions only, and SHALL NOT introduce any header, trailer, or delimiter around the bytes a text read returns.

Two surfaces, and no third:

- a **base64** result SHALL carry `content_hash` in the labelled header it already emits ahead of its opaque body;
- `read_file(path, hash_only=true)` SHALL return a metadata-only result — the path, the size in bytes, the MIME type and the `content_hash` — and **no file content at all**.

`hash_only` SHALL ignore `encoding`, because the digest is over raw bytes in every case, and SHALL be refused in combination with a non-default `offset` or `limit` rather than silently having no effect on them.

A text result SHALL remain exactly what it is today: the file's decoded text with nothing added. An envelope around note-controlled bytes is the forgery class the structured-read work removed, and it SHALL NOT be reintroduced here under any flag.

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

### Requirement: write_file honours an optional expected_hash and reports what it wrote

`write_file` SHALL accept an optional `expected_hash` that binds an `overwrite=true` call to the incumbent file's `content_hash`, and SHALL report the resulting file's `content_hash` in its success message.

Behaviour follows the `vault-write` capability's precondition requirements: omitted, the call is the unconditional replace it is today, reading nothing; supplied with `overwrite=true`, the tool reads the incumbent through the descriptor it already validated, refuses on a mismatch naming the current hash without writing, and — on a match — also passes those bytes as the pre-publication comparison so that the two windows are closed together; supplied with `overwrite=false`, the call is refused, because a no-clobber creation has no incumbent bytes to bind. An incumbent too large to read within `MAX_FILE_READ_BYTES` SHALL be a refusal naming the cap, never an unguarded write.

#### Scenario: A guarded overwrite of a changed file refuses

- **WHEN** `write_file(path, content, overwrite=true, expected_hash=…)` is invoked and the file's bytes have changed since the hash was obtained
- **THEN** the tool SHALL refuse naming the file's current `content_hash`, and the file SHALL be unchanged

#### Scenario: A guarded overwrite of an unchanged file succeeds and reports its hash

- **WHEN** the same call is made with a matching hash
- **THEN** the write SHALL succeed and the result SHALL report the `content_hash` of the bytes just written

#### Scenario: An unguarded overwrite is unchanged

- **WHEN** `write_file(path, content, overwrite=true)` is invoked with no `expected_hash`
- **THEN** the tool SHALL replace the file unconditionally, exactly as today, and SHALL NOT read the incumbent

#### Scenario: A precondition on a no-clobber write is refused

- **WHEN** `write_file(path, content, expected_hash=…)` is invoked with `overwrite=false`
- **THEN** the tool SHALL return an error explaining that a no-clobber write has nothing to bind, and SHALL NOT create the file
