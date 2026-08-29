## Why

~80% of vault accesses now come through MCP (agents), not the Obsidian app, but the README's title, tagline, and lead still frame the project as an Obsidian accessory ("turns your Obsidian vault into…"). The body already describes a shared agent-memory layer; the header sells a narrower product than the one that exists, and positions it against the wrong competitors (other Obsidian MCP servers instead of hosted agent-memory systems like mem0/Zep/Letta).

## What Changes

- Rewrite the README lead paragraph and tagline to lead with **markdown-native agent memory**: a memory system for AI agents that happens to be a folder of markdown you can open in Obsidian. Obsidian becomes the human window — audit UI, escape hatch, proof of no lock-in — not the product definition.
- Reorder / reframe "Why this exists" so the agent-memory framing comes first and the exocortex/shared-vault narrative supports it.
- Add a "A session away from the keyboard" companion to the existing "A session at the keyboard" section: the podcast-walk story (transcript → conversation → health decisions, date corrections, and a philosophical note land in the vault; Obsidian never opened).
- Add a "vs. hosted memory systems" comparison (mem0/Zep/Letta-class: opaque stores, per-agent silos, export problems) alongside the existing "vs. other Obsidian MCP servers" and "vs. an agent with raw file access" comparisons.
- **Not changing**: the repo name, registry listings, or package identity — "Obsidian MCP Server" stays for search discoverability. No code changes.

## Capabilities

### New Capabilities

- `readme-positioning`: what the README's framing must communicate — agent-memory-first lead, honest scope (markdown-native, not universal), Obsidian-as-window, comparison coverage, and name stability.

### Modified Capabilities

(none — documentation-only change; no runtime behavior changes)

## Impact

- `README.md` only. No source, schema, deployment, or tool changes.
- Registry descriptions (punkpeye PR #6462 etc.) are out of scope here but should eventually echo the new tagline.
- Coordination issue: #158.
