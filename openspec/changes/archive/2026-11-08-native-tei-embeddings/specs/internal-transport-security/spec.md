## MODIFIED Requirements

### Requirement: The active embedding endpoint MUST satisfy a scheme policy at startup
The server MUST validate the URL of the active embedding provider — `OLLAMA_URL` when `EMBEDDING_PROVIDER` is `ollama`, `OPENAI_BASE_URL` when it is `openai`, `TEI_URL` when it is `tei` — at settings construction, and MUST refuse to start when the raw value has leading or trailing whitespace or contains a control character, when the HTTP client's own URL parser rejects it, when the scheme is neither `http` nor `https`, the URL has no host, the URL carries userinfo, or the port is invalid. The host, scheme and port SHALL be those produced by the HTTP client's URL parser, and the validated string SHALL be used unchanged by every client and by the startup report. An `https` URL SHALL be accepted. An `http` URL SHALL be accepted when its host is literally loopback (`localhost`, an IPv4 address in 127.0.0.0/8, or `::1`, bracketed or not), and otherwise SHALL be accepted only when `EMBEDDING_ALLOW_PLAINTEXT` is true. `EMBEDDING_ALLOW_PLAINTEXT` SHALL default to false. The host SHALL be the one the HTTP client will dial, with no DNS resolution. The inactive provider's URL SHALL NOT be validated. Under `MCP_SANDBOX_MODE` every rule that depends on the endpoint URL SHALL be skipped, while the validity of a configured `EMBEDDING_CA_FILE` SHALL still be enforced.

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

#### Scenario: Whitespace and control characters are refused
- **WHEN** the active URL is ` https://embeddings.internal.example/v1` (leading space), `http://127.0.0.1:11434\n` (trailing newline), or contains a tab or carriage return anywhere
- **THEN** startup SHALL fail, with or without the override

#### Scenario: The client's parser decides
- **WHEN** a URL is interpreted differently by the standard library's URL splitter and by the HTTP client's parser
- **THEN** the policy's verdict SHALL follow the HTTP client's parser

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

#### Scenario: Sandbox does not excuse a broken CA file
- **WHEN** `MCP_SANDBOX_MODE=true` and `EMBEDDING_CA_FILE` names a file that is not a PEM certificate
- **THEN** settings construction SHALL fail

#### Scenario: TEI_URL is the active URL under tei
- **WHEN** `EMBEDDING_PROVIDER=tei`, `TEI_URL` is `http://tei:80` and `EMBEDDING_ALLOW_PLAINTEXT` is unset
- **THEN** startup SHALL fail with a message naming `TEI_URL` and `EMBEDDING_ALLOW_PLAINTEXT`

#### Scenario: Inactive TEI_URL is not validated
- **WHEN** `EMBEDDING_PROVIDER=ollama` with an admitted `OLLAMA_URL` and `TEI_URL` is `http://tei:80` without the override
- **THEN** startup SHALL proceed

### Requirement: Every embedding HTTP client SHALL be built by one factory that pins trust, ignores the environment and refuses redirects
Every HTTP client the server opens to the configured embedding endpoint — the Ollama provider, the OpenAI-compatible provider, the TEI provider including its `GET /info` request, and the control panel's Ollama and TEI reachability checks — SHALL be constructed by one factory that sets `follow_redirects` to false and `trust_env` to false explicitly, so that no proxy variable, CA-location variable or netrc file affects the hop. The trust anchor SHALL be the TLS context parsed from `EMBEDDING_CA_FILE` at settings construction when that setting is set, and otherwise a context built from the certifi bundle; the operating-system store and the `SSL_CERT_FILE` / `SSL_CERT_DIR` variables SHALL NOT be consulted. `EMBEDDING_CA_FILE` SHALL be parsed at settings construction, and startup SHALL be refused when it does not exist, is not a readable regular file, cannot be loaded as a certificate, or is set while the active embedding URL uses `http`; the parsed context SHALL be reused and the file SHALL NOT be re-read.

#### Scenario: CA pinning
- **WHEN** `EMBEDDING_CA_FILE` is set and a provider posts to an `https` endpoint
- **THEN** the client SHALL verify the server against that CA only, not against the certifi bundle or the system store

#### Scenario: Default trust
- **WHEN** `EMBEDDING_CA_FILE` is unset
- **THEN** the client SHALL verify `https` endpoints against the certifi bundle

#### Scenario: Ambient proxy is ignored
- **WHEN** the active URL is `http://127.0.0.1:11434`, the override is unset, and `HTTP_PROXY` names a remote proxy with `NO_PROXY` unset
- **THEN** the embedding request SHALL be sent directly to the loopback endpoint and not to the proxy

#### Scenario: Ambient CA variables are ignored
- **WHEN** `SSL_CERT_FILE` or `SSL_CERT_DIR` is set in the environment
- **THEN** the embedding client's trust anchor SHALL be unchanged by it

#### Scenario: Unparseable CA refuses startup for any provider
- **WHEN** `EMBEDDING_CA_FILE` is a readable regular file that is not a PEM certificate and the active provider is `ollama` with an `https` URL, `openai` with an `https` base URL, or `tei` with an `https` URL
- **THEN** settings construction SHALL fail naming `EMBEDDING_CA_FILE`, before any request is served

#### Scenario: A redirect is not followed
- **WHEN** the embedding endpoint answers with a 3xx redirect to another URL
- **THEN** the client SHALL NOT issue a request to the redirect target

#### Scenario: CA file with a plaintext URL
- **WHEN** `EMBEDDING_CA_FILE` is set and the active embedding URL is `http://`
- **THEN** startup SHALL fail

#### Scenario: No bypassing client
- **WHEN** the source of the embedding providers and the control panel routes is inspected
- **THEN** no HTTP client directed at `OLLAMA_URL`, `OPENAI_BASE_URL` or `TEI_URL` SHALL be constructed except through the factory

### Requirement: The server SHALL report each outbound hop's transport once at startup
Outside sandbox mode, the server SHALL log one line per hop at startup: the database line required above, and an embedding line naming the active provider, the scheme, the host, the port, the trust source (`certifi`, `ca-file`, or `n/a` for `http`) and whether the plaintext override is in effect. The embedding line SHALL NOT contain the URL's path, query or userinfo. When the active embedding URL is `http` to a non-loopback host, the server SHALL also emit one `internal_transport_plaintext` event with `reason` `embedding` and `outcome` `override`; a loopback `http` endpoint SHALL NOT produce the event. When `RERANK_URL` is set, the server SHALL also log one rerank line naming the scheme, the host, the port, the trust source (`certifi`, `ca-file`, or `n/a` for `http`) and whether `RERANK_ALLOW_PLAINTEXT` is in effect, without the URL's path, query or userinfo; when `RERANK_URL` is `http` to a non-loopback host, the server SHALL emit one `internal_transport_plaintext` event with `reason` `rerank` and `outcome` `override`. When `RERANK_URL` is empty no rerank line SHALL be logged.

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

#### Scenario: Plaintext rerank hop admitted by its override
- **WHEN** the server starts with `RERANK_URL=http://gpu-host:8081/v1/rerank` and `RERANK_ALLOW_PLAINTEXT=true`
- **THEN** the rerank transport line SHALL report scheme `http`, host `gpu-host`, port `8081`, trust `n/a` and the override in effect, and SHALL NOT contain `/v1/rerank`
- **AND** one `internal_transport_plaintext` event SHALL be emitted with `reason` `rerank` and `outcome` `override`

#### Scenario: TEI embedding hop
- **WHEN** the server starts with `EMBEDDING_PROVIDER=tei`, `TEI_URL=http://tei:80` and `EMBEDDING_ALLOW_PLAINTEXT=true`
- **THEN** the embedding transport line SHALL name the provider `tei`, scheme `http`, host `tei`, port `80`, trust `n/a` and the override in effect
- **AND** one `internal_transport_plaintext` event SHALL be emitted with `reason` `embedding` and `outcome` `override`
