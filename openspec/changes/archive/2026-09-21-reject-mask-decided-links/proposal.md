## Why

Issue #218 still reproduces after the previous issue sweep: masking inline code
inside a link target can invent a different target and persist a false graph
edge. Agents consume backlinks and neighborhoods as facts. The rewrite path
already rejects these targets; extraction must apply the same rule.

## What Changes

Skip candidates whose deciding target/href span differs between original and
code-masked text, before counting them toward the extraction cap. Preserve
valid links with code only in labels, aliases or anchors. Advance extraction
version 2 to 3 so unchanged notes repair their stored graph; reuse the existing
embedding cleaner so the bump alone causes no provider work.

## Capabilities

### Modified Capabilities
- `wikilink-graph`: reject targets invented by masking and repair existing rows.

## Impact

`src/services/links.py`, the extraction version in `indexer.py`, the cleaner
registry in `embeddings.py`, regression tests and indexing architecture notes.
No migration or new setting. Existing owner-scoped rewrite refusal applies
until re-derivation completes. Existing issue #218 tracks this work.
