## 1. Rewrite the storefront

- [ ] 1.1 Rewrite the tagline + lead paragraph: memory-system-for-agents first, markdown/Obsidian as the human window; keep H1 "Obsidian MCP Server" and badges unchanged
- [ ] 1.2 Reorder "Why this exists": agent-memory-readable (§2 today) first, shared-exocortex second, "the vault follows you" third; keep heading text stable where possible; update TOC order

## 2. New sections

- [ ] 2.1 Write "A session away from the keyboard" after "A session at the keyboard": podcast-walk story, first-person, agent as primary writer, names the trust-in-experts note as the durable artifact
- [ ] 2.2 Write "vs. hosted memory systems" comparison (mem0/Zep/Letta-class) on readability / sharedness / portability / self-description, with at least one honest concession to hosted systems; add TOC entry near the other comparisons

## 3. Integrity pass

- [ ] 3.1 Verify every TOC link resolves to a heading anchor in the edited file
- [ ] 3.2 Diff against previous README: tool counts, config names, and security statements unchanged except intentional reframing; voice reads as the same author

## 4. Codex-review additions

- [ ] 4.1 Snapshot all pre-change heading anchors; after the edit, verify each resolves (add `<a id="old-slug">` compatibility anchors where renumbering changed a slug)
- [ ] 4.2 Rewrite all six image references to absolute raw.githubusercontent.com URLs; verify each returns 200
- [ ] 4.3 Survey check: every pre-change section, heading, image, and badge still present outside named edit regions; every occurrence of the tool count matches the registered count in src/mcp_server/server.py (25)
- [ ] 4.4 Comparison-claims audit: no unsourced categorical claim about a named product; capability boundary stated (MCP clients, agent-directed persistence; no auto-extraction/consolidation/decay implied)
