## ADDED Requirements

### Requirement: Agent-memory-first lead
The README SHALL open (H1 tagline + first paragraph) by presenting the project as a memory system for AI agents whose storage is plain markdown in an Obsidian vault, with Obsidian framed as the human window (reading, auditing, editing agents' memory) rather than as the product being extended.

#### Scenario: A visitor reads only the first paragraph
- **WHEN** a reader sees only the tagline and lead paragraph
- **THEN** they can state that the product is agent memory stored as readable markdown, and that Obsidian is how the human inspects it

#### Scenario: README-only diff
- **WHEN** the implementation commit for the rewrite is diffed
- **THEN** the only file changed is `README.md` (OpenSpec change artifacts exempt), and the H1 text "Obsidian MCP Server" is byte-identical to before

### Requirement: Capability-bounded scope framing
The README SHALL state the capability boundary of the memory claim: markdown-native, wikilink-structured storage with indexing, full-text/semantic search, and graph operations, exposed to connected MCP clients, where agents direct their own reads and writes. It SHALL NOT claim universality, automatic memory extraction/consolidation/relevance/decay, or drop-in replacement of hosted memory products, explicitly or by implication.

#### Scenario: Claim audit
- **WHEN** the README's positioning statements are reviewed
- **THEN** each memory claim is scoped to what the server implements (MCP-accessible storage, search, graph; agent-directed persistence) and no statement implies automatic extraction, consolidation, decay, or functional parity with hosted memory systems

### Requirement: Away-from-keyboard narrative
The README SHALL include a section, sibling to "A session at the keyboard", narrating a real session conducted entirely through an agent, containing all of these beats: a podcast listened to on a walk, its transcript run through Claude, a conversation about it, and — written to the vault by the agent — health-related decisions, date corrections, and a durable note clarifying how the author decides which experts to trust; the section SHALL state that Obsidian was never opened.

#### Scenario: Story beats present
- **WHEN** a reader finishes the section
- **THEN** every beat above is present: transcript ingestion, agent conversation, health decisions, date corrections, the trust-in-experts note as a named durable artifact, and the explicit absence of Obsidian in the loop

### Requirement: Hosted memory systems comparison
The README SHALL include a comparison against the hosted/API-accessed agent-memory architecture (the mem0/Zep/Letta class), covering at minimum readability, human+multi-agent sharedness, portability, and self-description, and SHALL concede at least one genuine advantage of hosted systems. Comparative claims SHALL be made about the architectural pattern (memory behind a service API), not as categorical assertions about named products; any claim attributed to a specific named product SHALL either be omitted or carry a dated source.

#### Scenario: Balanced comparison
- **WHEN** the comparison section is read
- **THEN** it names the competing class, differentiates on the four axes at the architecture level, includes at least one honest trade-off in the hosted systems' favor, and contains no unsourced categorical claim about a specific named product

### Requirement: Anchor stability
Every heading anchor generated from the pre-change README SHALL remain resolvable in the post-change README — via unchanged heading text or an explicit HTML compatibility anchor (`<a id="…">`) where a heading is renamed or renumbered — and every TOC entry in the post-change README SHALL resolve to an existing heading.

#### Scenario: Pre-change anchor set survives
- **WHEN** the full set of heading anchors extracted from the pre-change README is resolved against the post-change README
- **THEN** every anchor resolves (directly or via a compatibility anchor)

#### Scenario: Post-edit TOC check
- **WHEN** the edited README's TOC entries are resolved against its headings
- **THEN** every TOC link targets an existing anchor

### Requirement: Content preservation outside edit regions
Outside the named edit regions — the tagline/lead, the "Why this exists" ordering, the two new sections, the TOC, and image URL rewrites — every pre-existing README section, heading, image reference, badge, and link SHALL remain present, and protected technical facts SHALL be unchanged: the tool count (authoritative value: the number of tools registered in `src/mcp_server/server.py`, currently 25, in all its occurrences), the stack sentence, configuration names, and security statements. The reframing latitude SHALL NOT extend to technical facts.

#### Scenario: Section survey
- **WHEN** the pre-change README's headings, image references, and badges are checked against the post-change README
- **THEN** all are present except where a named edit region explicitly reorders them

#### Scenario: Fact preservation
- **WHEN** the edited README is diffed against the previous version
- **THEN** every occurrence of the tool count equals the count registered in `src/mcp_server/server.py`, and no stack claim, configuration name, or security statement is altered

### Requirement: Images render off-GitHub
Every image in the README SHALL be referenced by an absolute `https://raw.githubusercontent.com/…` URL pinned to the `main` branch so it renders on sites that re-render the README (MCP registries, mirrors), not only on github.com.

#### Scenario: Registry rendering
- **WHEN** the README's image srcs are extracted
- **THEN** each is an absolute raw.githubusercontent.com URL that returns HTTP 200
