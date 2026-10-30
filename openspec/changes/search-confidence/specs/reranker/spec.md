## MODIFIED Requirements

### Requirement: The rerank argument SHALL be in both tools' schemas on every deployment, and rerank=false SHALL be byte-identical to the tools without it
`semantic_search` and `hybrid_search` SHALL each accept `rerank: bool = false`, whatever the configuration. The argument SHALL NOT be listed in `required`. With `rerank=false` or the argument omitted, each tool's statements SHALL be byte-identical to what they were before this change, and its output and telemetry SHALL be byte-identical to the search-confidence contract's `rerank=false` page: the page before this change plus the `confidence:` header token and, when `low`, the conditional hint line (the `search-quality` and `hybrid-search` capabilities define both), and the telemetry before this change plus the `rerank` argument and the confidence keys. With `rerank=true`, the response SHALL always be rendered in the `rerank=true` encoding, whether reranked or not. The tools' annotations SHALL be unchanged. Each description SHALL state what `rerank=true` does, that it can add up to the configured timeout, that its rows use the quoted-object encoding, and what the `reranked` header token means. No description SHALL call reranking better, best, recommended or default. Each SHALL stay at most 4,000 characters on the wire.

#### Scenario: Byte-identity
- **WHEN** `semantic_search` renders the same service rows with `rerank` omitted, before #367 and after search-confidence
- **THEN** the later output SHALL equal the earlier one with exactly ` — confidence: ok` inserted before the header's closing `:` (or ` — confidence: low` and the hint line, when `low`), and no other byte changed

#### Scenario: Neutral description
- **WHEN** the `semantic_search` description is read from the registry
- **THEN** it SHALL mention `rerank` and `reranked: false`, and SHALL NOT contain "better", "best" or "recommended"
