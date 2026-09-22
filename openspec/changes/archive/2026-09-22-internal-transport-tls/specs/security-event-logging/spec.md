## ADDED Requirements

### Requirement: A plaintext internal hop SHALL be recorded once per process start as a catalogued event
The server SHALL declare an `internal_transport_plaintext` event in the security event catalogue at WARNING level with exactly the fields `reason` and `outcome`, and SHALL emit it at most once per hop per process start. `reason` SHALL be `database` or `embedding`; `outcome` SHALL be the database mode that admitted the plaintext session (`prefer` or `disable`) or `override` for the embedding plaintext override. The record SHALL NOT carry a host, port, URL, DSN, username, file path or any other value outside its declared fields, and its message SHALL be a constant.

Startup records normally stay on the bare logger because the catalogue's suppressor exists to bound flood channels. This one is catalogued anyway: it is a standing security fact that operators query by name, it is emitted at most twice per start, and a catalogued event is the only log shape with a stable name and a policed field set.

#### Scenario: Catalogue entry
- **WHEN** the event catalogue and the documented event table are compared
- **THEN** both SHALL contain `internal_transport_plaintext` with the fields `reason` and `outcome` and no others

#### Scenario: Database hop
- **WHEN** the server starts under `DATABASE_SSL_MODE=prefer` and the session is unencrypted
- **THEN** exactly one record SHALL be emitted with `reason` `database` and `outcome` `prefer`

#### Scenario: No endpoint identity in the record
- **WHEN** the embedding hop's record is emitted for `OLLAMA_URL=http://ollama:11434`
- **THEN** the serialised record SHALL NOT contain `ollama`, `11434` or `http://`
