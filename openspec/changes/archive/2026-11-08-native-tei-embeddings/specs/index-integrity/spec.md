## MODIFIED Requirements

### Requirement: A many-chunk note completes, and certifies only on full coverage

The Ollama embedding batch SHALL have no aggregate deadline. Its liveness bound is the per-request timeout: 30 s per provider request, where each request carries at most `OLLAMA_EMBED_BATCH_SIZE` inputs, a fixed size independent of the note. The TEI embedding batch SHALL likewise have no aggregate deadline: its bound is per slice, at most `TEI_EMBED_BATCH_SIZE` inputs, and covers that slice's retries. So a note cannot be structurally unable to finish while every request is individually healthy. A note SHALL be certified only when every one of its **requested** chunks has a vector, whether produced by the provider or reused under the chunk-reuse requirement. No partial coverage of the requested chunks may ever be stamped complete.

**"Requested chunks" is the bounded chunk list**, not every chunk the note's text could yield. The chunker caps a note at `MAX_CHUNKS_PER_NOTE` in document order, and a capped note is certified on full coverage of that capped list. Certification is what makes the cap safe. A capped note left uncertified would be re-selected by the backlog on every pass for ever, and would re-perform every provider call it already made: exactly the never-finishing note that removing the aggregate deadline exists to prevent. The degradation is declared on the row and in every result that names the note; it is not expressed by withholding the stamp.

**No aggregate deadline SHALL be reintroduced in place of the cap, and the batch size SHALL NOT be made proportional to the note.** The cap bounds a *count*, deterministically, and says so on the row. A time budget fires on a note whose chunks are all individually healthy, certifies nothing, and repeats every tick. A proportional batch size would reintroduce the same boundary one size class up.

#### Scenario: A giant note eventually embeds and stops being retried

- **WHEN** a note produces more chunks than the former fixed 300 s deadline allowed at normal provider latency, and fewer than the chunk cap
- **THEN** the embed pass SHALL process all of its chunks, certify it, and not select it again while its content is unchanged

#### Scenario: A hung provider still fails fast

- **WHEN** the Ollama embedding provider stops responding mid-batch (the OpenAI provider keeps its own pre-existing contract: per-request HTTP timeout with bounded retries; the TEI provider's bound is its per-slice deadline, which the same scenario applies to at that deadline)
- **THEN** the in-flight request SHALL time out at the per-request timeout and the note SHALL remain uncertified

#### Scenario: Partial coverage is never certified

- **WHEN** the provider returns fewer vectors than the chunks it was sent for a note
- **THEN** the note SHALL NOT be certified and its previous vectors SHALL remain in place

#### Scenario: A capped note is certified on full coverage of its capped list

- **WHEN** a note yields more chunks than the cap and every one of the first N chunks has a vector
- **THEN** the note SHALL be certified
- **AND** it SHALL NOT be selected by the backlog again while its content is unchanged

#### Scenario: A capped note with one chunk missing is not certified

- **WHEN** a note is capped at N chunks and only N-1 of them have a vector
- **THEN** the note SHALL NOT be certified and its previous vectors SHALL remain in place
