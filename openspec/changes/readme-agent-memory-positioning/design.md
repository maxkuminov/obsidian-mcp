## Context

The README is the product's entire storefront: repo visitors, MCP registry reviewers, and HN/Reddit readers all decide from it what this is. Its body already contains the agent-memory argument ("Why this exists" §1–2, "Agent memory that you can actually read"), but the header, tagline, and section ordering frame the project as an Obsidian add-on. Observed usage has inverted: ~80% of vault access is agents over MCP; Obsidian is the occasional human window.

Constraints:
- Public repo; README is the published face. No host-specific details may leak in.
- Registry listings and the repo name are established; renaming would forfeit "Obsidian MCP server" search traffic and invalidate open registry submissions (punkpeye PR #6462).
- The existing README voice is first-person, concrete, story-driven. The rewrite must read as the same author, not a marketing pass.

## Goals / Non-Goals

**Goals:**
- Lead with "memory system for AI agents, stored as markdown you can open in Obsidian."
- Reposition Obsidian as the human window (audit, edit, no lock-in) rather than the product.
- Add the away-from-keyboard story and a hosted-memory-systems comparison.
- Preserve voice, factual accuracy (25/27-tool counts, stack claims), and all existing anchors that external links may target.

**Non-Goals:**
- No rename of repo, package, container, or registry entries.
- No claim of "universal" agent memory — the honest scope is markdown-native, wikilink-structured corpora.
- No code, screenshot, or tooling changes (the dashboard screenshot stays as-is; light mode ships separately under `panel-light-mode`).

## Decisions

1. **Keep the name, flip the tagline.** "Obsidian MCP Server" remains the H1; the tagline beneath it carries the repositioning. Rationale: discoverability is an asset, and the name is accurate — the inversion happens in the first sentence, which is what people actually read. Alternative (rename to something like "exocortex"/"agent-memory-server") rejected: loses search category, breaks in-flight registry submissions.
2. **Rewrite the lead as memory-first.** Pattern: "A memory system for your AI agents — stored as plain markdown in your Obsidian vault, so you can read every note your agents write." The existing self-describing/indexed clauses survive, subordinated. Alternative (keep current lead, add a second paragraph) rejected: the first sentence is the positioning.
3. **Reorder "Why this exists" to memory → shared exocortex → follows you.** Current §2 ("Agent memory you can actually read") becomes §1; current §1 (shared memory layer / exocortex) becomes §2 with its content intact; §3 unchanged. Headings within keep their text where possible so deep links keep working; the TOC order changes.
4. **"A session away from the keyboard" as a sibling section** after "A session at the keyboard", telling the podcast-walk story in the same transcript-flavored or narrative style: health podcast → transcript through Claude → conversation → decisions, date corrections, notes, and a philosophical note about trusting medical expertise written to the vault — Obsidian never opened. Genericize the health details to what the story needs; it is Max's real story and stays first-person.
5. **"vs. hosted memory systems" comparison** placed with the existing comparison sections, contrasting on: readability (files vs. opaque store), sharedness (one substrate for the human and every agent vs. per-agent silos), portability (folder of markdown vs. export APIs), and self-description (vault guide vs. schema-less blobs). Name the class (mem0/Zep/Letta-style hosted memory), compare honestly — acknowledge what hosted systems do better (zero ops, managed relevance/decay) so the section reads as analysis, not ad copy.

## Risks / Trade-offs

- [Tagline drift from registry blurbs] → registries still say "Obsidian vault access"; acceptable short-term, tracked as follow-up in #158.
- [Rewrite flattens the author's voice] → implementer edits the existing text rather than regenerating it; reviewer gate checks voice explicitly.
- [Broken anchors from reordering] → keep heading slugs stable where feasible; verify TOC links after edit.
- [Overclaiming vs. mem0/Zep-class systems] → comparison must concede hosted strengths; Codex review told to attack unsupportable claims.

## Open Questions

(none blocking — palette of exact wording is the implementer's latitude within the spec requirements)
