## MODIFIED Requirements

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
