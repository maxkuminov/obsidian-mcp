## ADDED Requirements

### Requirement: Agent-memory-first lead
The README SHALL open (H1 tagline + first paragraph) by presenting the project as a memory system for AI agents whose storage is plain markdown in an Obsidian vault, with Obsidian framed as the human window (reading, auditing, editing agents' memory) rather than as the product being extended.

#### Scenario: A visitor reads only the first paragraph
- **WHEN** a reader sees only the tagline and lead paragraph
- **THEN** they can state that the product is agent memory stored as readable markdown, and that Obsidian is how the human inspects it

#### Scenario: Name stability
- **WHEN** the repositioned README is published
- **THEN** the repository name, H1 title "Obsidian MCP Server", container name, and registry identity are unchanged

### Requirement: Honest scope framing
The README SHALL scope its memory claim to markdown-native, wikilink-structured corpora and SHALL NOT claim to be a universal or drop-in memory backend for arbitrary data.

#### Scenario: Claim audit
- **WHEN** the README's positioning statements are reviewed
- **THEN** no statement claims universality, and the markdown/wikilink assumption is stated or evident wherever the memory claim is made

### Requirement: Away-from-keyboard narrative
The README SHALL include a section, sibling to "A session at the keyboard", narrating a real session conducted entirely through an agent (podcast-walk story) in which decisions, corrections, and new notes land in the vault without Obsidian being opened.

#### Scenario: Story demonstrates the inversion
- **WHEN** a reader finishes the section
- **THEN** the section has shown the agent as the primary writer of vault memory and named at least one durable artifact left in the vault (e.g., a note clarifying how the author decides which experts to trust)

### Requirement: Hosted memory systems comparison
The README SHALL include a comparison against hosted/opaque agent-memory systems (mem0/Zep/Letta-class) covering at minimum readability, human+multi-agent sharedness, portability, and self-description, and SHALL concede at least one genuine advantage of hosted systems.

#### Scenario: Balanced comparison
- **WHEN** the comparison section is read
- **THEN** it names the competing class, differentiates on the four axes, and includes at least one honest trade-off in the hosted systems' favor

### Requirement: Existing content integrity
The README SHALL retain its existing factual claims (tool counts, stack, security notes), first-person voice, and working intra-document TOC links after the repositioning edit.

#### Scenario: Post-edit link check
- **WHEN** the edited README's TOC entries are resolved against its headings
- **THEN** every TOC link targets an existing heading anchor

#### Scenario: Fact preservation
- **WHEN** the edited README is diffed against the previous version
- **THEN** no tool count, configuration name, or security statement has been altered except where the edit intentionally reframes positioning
