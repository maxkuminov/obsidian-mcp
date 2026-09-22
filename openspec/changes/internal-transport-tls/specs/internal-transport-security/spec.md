## ADDED Requirements

### Requirement: Database TLS SHALL be configured by one mode setting whose default preserves current behaviour
The server SHALL read the database transport policy from `DATABASE_SSL_MODE`, accepting exactly `disable`, `prefer`, `require`, `verify-ca` and `verify-full` (case-insensitive, surrounding whitespace ignored), with a default of `prefer`, and SHALL refuse to start on any other value. The mode `allow` SHALL NOT be accepted.

`prefer` is the default because it is the behaviour every existing deployment already has; a stricter default would take down a deployment whose database does not offer TLS. The downgrade `prefer` permits is made visible by the startup assertion, not by the default.

#### Scenario: Default mode
- **WHEN** `DATABASE_SSL_MODE` is unset
- **THEN** the effective mode SHALL be `prefer`
- **AND** a database that does not offer TLS SHALL still be reachable

#### Scenario: Spelling is normalised
- **WHEN** `DATABASE_SSL_MODE` is ` Verify-Full `
- **THEN** the effective mode SHALL be `verify-full`

#### Scenario: Unknown or weaker mode is refused
- **WHEN** `DATABASE_SSL_MODE` is `allow`, `true`, `on` or `verify_full`
- **THEN** settings construction SHALL fail with a message listing the accepted modes

### Requirement: Strict database modes SHALL use an explicit TLS context with no plaintext fallback
For `require`, `verify-ca` and `verify-full` the server SHALL pass the database driver an explicit TLS client context with a minimum protocol version of TLS 1.2, and SHALL NOT pass a mode string that lets the driver consult environment variables or home-directory certificate files. `require` SHALL encrypt without verifying the server; `verify-ca` SHALL require a certificate chain to the configured CA without checking the hostname; `verify-full` SHALL additionally require the certificate to match the host named in `DATABASE_URL`. Under any of the three, a server that does not offer TLS SHALL cause the connection to fail rather than fall back to plaintext. `disable` SHALL connect without TLS and `prefer` SHALL keep the driver's advisory behaviour.

#### Scenario: verify-full checks the hostname
- **WHEN** the connect arguments are built for `verify-full` with a readable CA file
- **THEN** the TLS context SHALL have hostname checking enabled and SHALL require a verified certificate
- **AND** its minimum protocol version SHALL be TLS 1.2

#### Scenario: verify-ca checks the chain but not the hostname
- **WHEN** the connect arguments are built for `verify-ca`
- **THEN** the TLS context SHALL require a verified certificate and SHALL NOT check the hostname

#### Scenario: require encrypts without verification
- **WHEN** the connect arguments are built for `require`
- **THEN** the TLS context SHALL NOT verify the certificate or the hostname
- **AND** it SHALL still be passed as a context, not as a mode string

#### Scenario: No silent fallback under a strict mode
- **WHEN** the mode is `require` and the database server does not offer TLS
- **THEN** the connection attempt SHALL fail
- **AND** no plaintext session SHALL be opened

#### Scenario: Stray driver trust files are ignored
- **WHEN** the mode is `require` and a `root.crt` exists in the process's home PostgreSQL directory
- **THEN** the connection SHALL remain unverified, exactly as `require` is documented, and SHALL NOT silently start verifying

#### Scenario: Client certificate
- **WHEN** both `DATABASE_SSL_CERT_FILE` and `DATABASE_SSL_KEY_FILE` are set under a strict mode
- **THEN** the TLS context SHALL present that certificate and key to the server

### Requirement: Every production database connection creator SHALL use the same transport arguments
The application engine and the migration engine SHALL both obtain their TLS connect arguments from one shared helper, so that the application, the MCP stdio entry point, the maintenance scripts and every alembic command connect under the same policy. Test-only engines against the throwaway integration database are exempt.

#### Scenario: Application engine
- **WHEN** the application engine is created
- **THEN** its connect arguments SHALL contain the helper's TLS arguments alongside the existing server settings

#### Scenario: Migration engine
- **WHEN** `alembic upgrade head` or `alembic check` runs with `DATABASE_SSL_MODE=verify-full`
- **THEN** the migration connection SHALL use the same verifying TLS context as the application

### Requirement: Conflicting or incomplete database TLS configuration MUST be refused at startup
The server MUST refuse to start, naming the offending input and the setting to use instead, when any of the following holds: the database URL's query string carries a TLS key (`ssl`, `sslmode`, `sslrootcert`, `sslcert`, `sslkey`, `sslcrl`, `sslpassword`, `sslnegotiation` or `direct_tls`, case-insensitively); an environment variable whose name begins with `PGSSL` is set; `verify-ca` or `verify-full` is selected without `DATABASE_SSL_CA_FILE`; `DATABASE_SSL_CA_FILE` is set with `disable`, `prefer` or `require`; exactly one of `DATABASE_SSL_CERT_FILE` and `DATABASE_SSL_KEY_FILE` is set; a client certificate is set with `disable` or `prefer`; or a named file does not exist, is not a regular file, is not readable, or cannot be loaded as the certificate material it names.

TLS for the database has one source. The driver stack would otherwise merge a URL parameter, an environment variable and the explicit connect arguments by rules an operator cannot see — and in at least one ordering, the URL's value is silently discarded.

#### Scenario: TLS parameter in the URL
- **WHEN** `DATABASE_URL` ends in `?ssl=require` or `?sslmode=disable`
- **THEN** startup SHALL fail with a message naming `DATABASE_SSL_MODE`

#### Scenario: A non-TLS URL parameter is untouched
- **WHEN** `DATABASE_URL` carries only a non-TLS query parameter such as `prepared_statement_cache_size`
- **THEN** startup SHALL NOT be refused on that account

#### Scenario: Driver environment variable
- **WHEN** `PGSSLMODE` or `PGSSLROOTCERT` is set in the process environment
- **THEN** startup SHALL fail with a message naming the variable and `DATABASE_SSL_MODE`

#### Scenario: CA file with a non-verifying mode
- **WHEN** `DATABASE_SSL_CA_FILE` is set and the mode is `require`
- **THEN** startup SHALL fail with a message suggesting `verify-ca` or `verify-full`

#### Scenario: Verifying mode without a CA
- **WHEN** the mode is `verify-full` and `DATABASE_SSL_CA_FILE` is unset
- **THEN** startup SHALL fail

#### Scenario: Unusable CA file
- **WHEN** `DATABASE_SSL_CA_FILE` names a missing path, a directory, an unreadable file, or a file that is not a PEM certificate
- **THEN** startup SHALL fail before the first request is served

#### Scenario: Half a client pair
- **WHEN** `DATABASE_SSL_CERT_FILE` is set and `DATABASE_SSL_KEY_FILE` is not
- **THEN** startup SHALL fail

### Requirement: The server SHALL assert the database session's transport at startup
Before any other database startup check, and outside sandbox mode, the server SHALL read `pg_stat_ssl` for its own backend. Under `require`, `verify-ca` or `verify-full` it SHALL exit with a critical log record when the session is not encrypted, when no row is returned, or when the connection could not be established. Under `prefer` or `disable` it SHALL continue and SHALL emit one `internal_transport_plaintext` security event with `reason` `database` and `outcome` set to the mode when the session is not encrypted. In every case it SHALL log the effective database transport once, naming the mode, whether the session is encrypted, the TLS version when encrypted, and whether the server was verified — the last derived from the mode, never inferred from the session.

#### Scenario: Strict mode with a plaintext session
- **WHEN** the mode is `require` and `pg_stat_ssl` reports the backend unencrypted (for example because the URL names a Unix socket)
- **THEN** the process SHALL log at critical level and exit with a non-zero status before serving

#### Scenario: Strict mode and the server refuses TLS
- **WHEN** the mode is `verify-full` and the first connection fails because the server does not offer TLS or the certificate does not verify
- **THEN** the process SHALL log at critical level naming the mode and the error class, and exit with a non-zero status

#### Scenario: Lax mode with a plaintext session
- **WHEN** the mode is `prefer` and the session is unencrypted
- **THEN** the process SHALL continue to start
- **AND** exactly one `internal_transport_plaintext` event SHALL be emitted with `reason` `database` and `outcome` `prefer`

#### Scenario: Encrypted session
- **WHEN** the session is encrypted
- **THEN** no `internal_transport_plaintext` event SHALL be emitted for the database hop
- **AND** the startup line SHALL report the TLS version and, for `prefer` or `require`, that the server was not verified

#### Scenario: Missing row fails closed
- **WHEN** the `pg_stat_ssl` query returns no row
- **THEN** the session SHALL be treated as unencrypted

#### Scenario: Sandbox mode
- **WHEN** `MCP_SANDBOX_MODE` is true
- **THEN** no transport probe SHALL run

### Requirement: The active embedding endpoint MUST satisfy a scheme policy at startup
The server MUST validate the URL of the active embedding provider — `OLLAMA_URL` when `EMBEDDING_PROVIDER` is `ollama`, `OPENAI_BASE_URL` when it is `openai` — at settings construction, and MUST refuse to start when the scheme is neither `http` nor `https`, the URL has no host, the URL carries userinfo, or the port is invalid. An `https` URL SHALL be accepted. An `http` URL SHALL be accepted when its host is literally loopback (`localhost`, an IPv4 address in 127.0.0.0/8, or `::1`, bracketed or not), and otherwise SHALL be accepted only when `EMBEDDING_ALLOW_PLAINTEXT` is true. `EMBEDDING_ALLOW_PLAINTEXT` SHALL default to false. The host SHALL be the one the HTTP client will dial, with no DNS resolution. The inactive provider's URL SHALL NOT be validated. The policy SHALL be skipped under `MCP_SANDBOX_MODE`.

#### Scenario: HTTPS endpoint
- **WHEN** the active URL is `https://embeddings.internal.example/v1`
- **THEN** startup SHALL proceed

#### Scenario: Loopback plaintext forms
- **WHEN** the active URL is `http://localhost:11434`, `http://127.0.0.1:11434`, `http://127.8.9.10`, `http://[::1]:11434` or `http://LOCALHOST.`
- **THEN** startup SHALL proceed without the override

#### Scenario: Non-loopback plaintext without the override
- **WHEN** the active URL is `http://ollama:11434` and `EMBEDDING_ALLOW_PLAINTEXT` is unset
- **THEN** startup SHALL fail with a message naming `EMBEDDING_ALLOW_PLAINTEXT` and the `https` alternative

#### Scenario: Look-alike hosts are not loopback
- **WHEN** the active URL is `http://127.0.0.1.evil.example`, `http://localhost.evil.example`, or `http://0.0.0.0:11434`, and the override is unset
- **THEN** startup SHALL fail

#### Scenario: Userinfo is refused
- **WHEN** the active URL is `http://127.0.0.1@evil.example:11434` or `https://user:secret@gateway.example/v1`
- **THEN** startup SHALL fail
- **AND** the message SHALL NOT contain the userinfo

#### Scenario: Other schemes are refused
- **WHEN** the active URL is `ftp://ollama:11434`, `ollama:11434` or `file:///tmp/x`
- **THEN** startup SHALL fail even with the override set

#### Scenario: Override admits plaintext
- **WHEN** the active URL is `http://ollama:11434` and `EMBEDDING_ALLOW_PLAINTEXT=true`
- **THEN** startup SHALL proceed

#### Scenario: Inactive provider is not validated
- **WHEN** `EMBEDDING_PROVIDER=openai` with an `https` base URL and `OLLAMA_URL` is `http://ollama:11434` without the override
- **THEN** startup SHALL proceed

#### Scenario: Sandbox exemption
- **WHEN** `MCP_SANDBOX_MODE=true` and the active URL is the default `http://ollama:11434` without the override
- **THEN** settings construction SHALL succeed

### Requirement: Every embedding HTTP client SHALL be built by one factory that pins trust and refuses redirects
Every HTTP client the server opens to the configured embedding endpoint — the Ollama provider, the OpenAI-compatible provider, and the control panel's Ollama reachability check — SHALL be constructed by one factory that sets `follow_redirects` to false explicitly and, when `EMBEDDING_CA_FILE` is set, verifies the server against that CA file only. `EMBEDDING_CA_FILE` SHALL be refused at startup when it does not exist, is not a readable regular file, cannot be loaded as a certificate, or is set while the active embedding URL uses `http`.

#### Scenario: CA pinning
- **WHEN** `EMBEDDING_CA_FILE` is set and a provider posts to an `https` endpoint
- **THEN** the client SHALL verify the server against that CA and not against the system trust store

#### Scenario: Default trust
- **WHEN** `EMBEDDING_CA_FILE` is unset
- **THEN** the client SHALL verify `https` endpoints against the system trust store

#### Scenario: A redirect is not followed
- **WHEN** the embedding endpoint answers with a 3xx redirect to another URL
- **THEN** the client SHALL NOT issue a request to the redirect target

#### Scenario: CA file with a plaintext URL
- **WHEN** `EMBEDDING_CA_FILE` is set and the active embedding URL is `http://`
- **THEN** startup SHALL fail

#### Scenario: No bypassing client
- **WHEN** the source of the embedding providers and the control panel routes is inspected
- **THEN** no HTTP client directed at `OLLAMA_URL` or `OPENAI_BASE_URL` SHALL be constructed except through the factory

### Requirement: The server SHALL report each outbound hop's transport once at startup
Outside sandbox mode, the server SHALL log one line per hop at startup: the database line required above, and an embedding line naming the active provider, the scheme, the host, the port, the trust source (`system`, `ca-file`, or `n/a` for `http`) and whether the plaintext override is in effect. The embedding line SHALL NOT contain the URL's path, query or userinfo. When the active embedding URL is `http` to a non-loopback host, the server SHALL also emit one `internal_transport_plaintext` event with `reason` `embedding` and `outcome` `override`; a loopback `http` endpoint SHALL NOT produce the event.

#### Scenario: Plaintext embedding hop admitted by the override
- **WHEN** the server starts with `OLLAMA_URL=http://ollama:11434` and `EMBEDDING_ALLOW_PLAINTEXT=true`
- **THEN** the embedding transport line SHALL report scheme `http`, host `ollama`, port `11434`, trust `n/a` and the override in effect
- **AND** one `internal_transport_plaintext` event SHALL be emitted with `reason` `embedding` and `outcome` `override`

#### Scenario: Loopback plaintext
- **WHEN** the active embedding URL is `http://127.0.0.1:11434`
- **THEN** the transport line SHALL be logged and no `internal_transport_plaintext` event SHALL be emitted

#### Scenario: TLS embedding hop
- **WHEN** the active embedding URL is `https` with `EMBEDDING_CA_FILE` set
- **THEN** the transport line SHALL report trust `ca-file` and no event SHALL be emitted
