# Obsidian MCP — Architectural Improvements Roadmap

This is the post-architecture-review roadmap. The following items have shipped
as formal OpenSpec changes:

- `openspec/changes/note-filters/` — tag and frontmatter filter parameters on the four query/listing tools, plus a shared filter helper. ✅ shipped.
- `openspec/changes/wikilink-graph-navigation/` — wikilink graph extraction + backlinks/links/neighborhood/find_related/find_orphans tools. ✅ shipped.
- `openspec/changes/vault-write-completion/` — atomic writes, expanded `edit_note` modes (`dry_run`, `replace_all`, `section`), plus `move_note`, `delete_note`, and `set_frontmatter` tools. ✅ shipped (covers items #5 and #6 below, minus the explicitly skipped sub-items called out inline).
- HNSW index on `note_embeddings.embedding` (item #8 below, HNSW portion). ✅ shipped (migration 008, `vector_cosine_ops`, `m=16, ef_construction=64`; `semantic_search` sets `hnsw.ef_search=80` per query and dedupes per note). Chunk size dropped 1500 → 512 with overlap 50 → 0 in the same release.

Everything below is the longer tail. Each item includes intent, why it
matters, and concrete implementation notes so it's straightforward to spec
out when its turn comes.

> **Note on hybrid search.** A hybrid (RRF-fused) `search` tool was
> considered and rejected during design review. `keyword_search` and
> `semantic_search` cover distinct, well-understood use cases and the
> agent picks well when docstrings are sharp. Folding them into one
> tool would add fusion noise without solving a clear problem.
> Revisit only if usage data shows agents consistently struggling to
> choose, or asking for both back-to-back.

---

## 3. File-watching + parallel embeddings

**Intent.** Replace the 5-minute periodic full scan with `watchfiles`-driven
event indexing, and parallelize embedding requests against Ollama.

**Why.** Today edits show up in search after up to 5 minutes. With
file-watching, propagation drops to seconds. The full-scan loop also walks all
~2,577 files every 300 s for nothing when nothing changed. Parallelizing
embeddings cuts force-reembed time roughly 3-5×.

**Implementation notes.**
- Add `watchfiles` to `requirements.txt` (already commented in).
- New service `src/services/watcher.py`:
  - `awatch(settings.vault_path, recursive=True)` async generator.
  - Debounce changes per-path (200 ms) and batch them per second.
  - Skip dot-dirs (mirror `index_vault`'s filter).
- Refactor `index_vault` so the per-file work is callable as
  `index_paths(paths: list[str])`. The watcher calls this with the debounced
  batch; the periodic loop calls it with the full file list.
- Keep the periodic loop as a safety net at a longer interval (e.g. 30 min)
  in case the watcher misses an event (NFS, container restart races).
- For embeddings: replace the `for chunk in chunks: await get_embedding(chunk)`
  loop with a `Semaphore(4)` + `asyncio.gather`. Keep the per-call timeout.
  Don't go higher than ~4 in parallel — Ollama on a single GPU saturates fast.

**Risk.** `watchfiles` on Docker bind mounts varies by host kernel. Verify
events fire reliably for the host vault path bind-mounted at `/obsidian`. If not,
fall back to the periodic-scan path.

**Effort.** ~½ day.

---

## 4. Heading-aware chunking

**Intent.** Replace character-window chunking with markdown-structure-aware
chunking and use a real tokenizer for size accounting.

**Why.** Current `chunk_text` slices on `chunk_size * 4` chars. Markdown has
natural semantic boundaries (`#`, `##`, paragraphs, lists, code blocks).
Heading-bound chunks embed dramatically better — each chunk is a coherent
unit. The "1 token ≈ 4 chars" approximation also drifts noticeably for
non-English content or note headers/lists.

**Implementation notes.**
- New module `src/services/chunking.py`:
  - Parse the note into a tree of sections by ATX headings.
  - Emit one chunk per section. If a section exceeds `max_tokens`, split it
    on paragraph breaks (then sentences if still too long).
  - If a section is much smaller than `min_tokens`, merge with the next
    sibling under the same parent heading.
  - Preserve a small overlap (≈ 1 sentence or ~50 tokens) between adjacent
    chunks for context continuity.
- Use the bge-m3 tokenizer for accurate counts. Either:
  - Add `tokenizers` (the HF Rust binding) and load `BAAI/bge-m3` once at
    startup, or
  - Approximate via Ollama's `/api/embed` with a length probe — slower, ok
    for batch sizing only.
- Backwards compatibility: keep `embedded_content_hash` so existing
  embeddings remain valid until a Force Re-embed All. Plan to run
  Force Re-embed All as part of the deploy.
- Update the panel "Settings" page with the new chunking strategy (info-only).

**Risk.** Initial reindex over the whole vault is slow; combine with item 3's
parallel embeddings to keep it under an hour.

**Effort.** ½ day for chunking + a long-running reembed afterward.

---

## 5. Vault revision safety (auto-commit + dry-run) — partially shipped

**Status.** The `dry_run=True` mode on `edit_note` and the atomic
tmp-file-then-`os.replace` write path shipped via `vault-write-completion`.
The git auto-commit and `note_revisions` table options are explicitly NOT
shipped — daily backups on the file server cover the rollback story for the
single-user-vault case, and per-tool-call git noise was not worth it.

**Intent (original).** Make agent writes recoverable. Either git-commit the
vault on every write tool call, or save before/after deltas in a new
`note_revisions` table. Add a `dry_run=True` mode to `edit_note` that
returns a unified diff without writing.

**Why.** Today, an LLM with a `readwrite` key can silently destroy a note
via full-file replace and there is no rollback path inside the system.
Even find-and-replace is destructive if the agent uses the wrong context
window. We should not give agents broad write power without an audit
trail.

**Implementation notes.**

_Option A — Git auto-commit (cheapest, recommended):_
- Confirm the host vault directory is (or becomes) a git repo. If not, `git init`
  and add a `.gitignore` for `.obsidian/workspace*`.
- After every `create_note` / `edit_note` / `delete_note` (when added) tool
  call succeeds, run `git add -A && git commit -m "auto: <tool> <path> via
  <key_prefix>"` from the indexer's process. Commits are cheap and
  immediately give `git log`-based rollback.
- Auth: GitConfig `user.name = "obsidian-mcp"`, `user.email = "mcp@local"`.
- Squash daily via a cron (`git rebase --autosquash` is overkill — just leave
  the history; it's cheap).

_Option B — `note_revisions` table (more flexible, more code):_
- Schema: `(id, note_path, before_content, after_content, key_id, tool,
  created_at)`.
- Write a row inside the same logical operation as the file write.
- Add a UI panel "Revisions" listing recent edits with diff view.
- Add a `revert_note(path, revision_id)` write tool.

_Either way:_
- Add `dry_run: bool = False` to `edit_note`. When true, compute the would-be
  new content, run `difflib.unified_diff(before, after)`, and return the diff
  to the agent without writing.
- Document this in the tool's docstring as the recommended preflight for
  full-replace edits.

**Effort.** ½ day for Option A + dry-run. Option B is ~1 day plus UI work.

---

## 6. Vault organization tools (`move_note`, `delete_note`, `set_frontmatter`) — shipped

**Status.** Shipped via `vault-write-completion` with atomic writes and
`rewrite_links=False`-by-default semantics. The `bulk_edit` sub-item is NOT
shipped — it was deferred until measured-needed. Cross-tool transactions /
two-phase-commit and auto-cleanup of dangling backlinks were also explicitly
deferred (use `find_orphans` / `get_backlinks` instead).

**Intent (original).** Round out the write surface with the organize-the-vault
operations.

**Why.** Today an agent can read, create, and edit, but cannot move or
remove notes or safely modify frontmatter. That blocks "rename and update
links", "soft-delete obsolete cards", "tag this batch as archived", etc.
Doing these by full-content rewrite is fragile.

**Implementation notes.**

`move_note(from_path, to_path)`:
- Validate both paths via `validate_path` (no traversal).
- If destination exists, fail.
- Create destination directory if missing.
- Use `shutil.move` (atomic on same filesystem).
- After file move: in the same transaction, update
  `notes_metadata.file_path` for the moved note and (only after the
  wikilink graph from item 2 lands) rewrite all `note_links` rows whose
  resolved target was this note. Optionally: rewrite incoming wikilinks
  in source notes to use the new path (gated behind a `rewrite_links=False`
  default — destructive, opt-in).

`delete_note(path)`:
- Soft-delete by default: move to `.trash/<timestamp>-<basename>` inside the
  vault.
- Add a `permanent=False` parameter for true delete (still keeps the
  auto-commit history if §5 is in place).
- DB rows cascade (notes_metadata FK CASCADEs already cover embeddings; add
  explicit cleanup for `note_links` if needed).

`set_frontmatter(path, updates: dict, remove: list[str] = [])`:
- Read note, parse frontmatter via existing `parse_frontmatter`.
- Apply updates; remove keys in `remove`.
- Re-serialize with `yaml.safe_dump(default_flow_style=False, sort_keys=False)`.
- Reassemble: `---\n<yaml>---\n<body>`.
- Write back.
- This is the right place for tag mutation: `set_frontmatter(path,
  updates={"tags": ["x", "y"]})`.

`bulk_edit(operations: list[dict])`:
- Each item is `{path, find, content}` or `{path, frontmatter_updates}`.
- Run sequentially in one tool call to avoid round-trip overhead. Don't
  parallelize writes (file-system race). Roll up errors into the response.

**Effort.** 1 day for the four tools.

---

## 7. MCP resources + structured tool returns

**Intent.** Expose vault content as MCP resources, and return structured
content from search tools instead of pre-formatted markdown strings.

**Why.** MCP resources are URI-addressable content the client can list,
discover, and prefetch without spending tool calls. Structured returns
(real JSON in the tool result instead of formatted markdown) let clients
render results their own way and make them more reliable to parse.

**Implementation notes.**

Resources:
- Implement `@mcp.list_resources()` returning a paginated list:
  - `obsidian://recent?limit=20`
  - `obsidian://tags`
  - `obsidian://note/{path}`
- Implement `@mcp.read_resource(uri)`:
  - For `obsidian://note/{path}`, return the note content with mime-type
    `text/markdown`.
  - For `obsidian://tags`, return JSON of `{tag: count}`.

Structured tool returns:
- The MCP Python SDK supports returning `list[Content]` blocks. Today we
  return strings.
- Refactor `search` (post-item-1) and `read_note` to return both:
  - A `TextContent` block formatted for human display.
  - An `EmbeddedResource` or structured JSON content for programmatic use.
- Document the structure in the tool docstrings so agents know what they
  get back.

**Effort.** ~1 day for resources + ½ day for structured returns.

---

## 8. Standardize ORM-vs-text in search.py

**Intent.** Finish the ORM migration. (HNSW index portion of this item is
✅ done — see top of file.)

**Why.** `src/services/search.py` still uses raw `text()` SQL while
`embeddings.py` is on ORM cosine distance. Standardize so filter helpers
(item 1) compose cleanly across both.

**Implementation notes.**
- Convert `src/services/search.py::full_text_search` to a SQLAlchemy
  `select` with `func.ts_rank_cd(...)` so it composes with the filter
  helper from item 1. (Item 1's tasks already cover this — listed here for
  completeness because both touch search.py.)

**Effort.** ½ day.

---

## 9. Deeper health endpoint + dashboard observability

**Intent.** Replace the `{"status": "ok"}` health endpoint with a probe
that actually checks subsystems, and surface embedding-queue depth on the
dashboard.

**Why.** Today the `/health` endpoint passes even when DB is down or
Ollama is unreachable, because it doesn't touch them. The indexer can
silently fall behind on embeddings and the only way to notice is by SSHing
in.

**Implementation notes.**
- Split into:
  - `/health` — cheap liveness probe (returns 200 if process is up).
    Used by Docker healthcheck.
  - `/ready` — readiness probe that:
    - Runs `SELECT 1` with a 1-second timeout.
    - Calls `GET <ollama_url>/api/tags` with a 1-second timeout.
    - Reads a module-level `last_index_completed_at` updated by the
      indexer; flags stale (> 2× `index_interval_seconds`).
    - Returns 200 + JSON details when all green; 503 + JSON details
      otherwise.
- Dashboard widget: count of
  `notes_metadata WHERE embedded_content_hash IS NULL OR embedded_content_hash != content_hash`
  → "embedding queue depth: N". Already partially shown via
  `embedding_pct`; promote it.
- Optional: add `/metrics` (prometheus_client) exposing
  `obsidian_mcp_index_queue_depth`, `obsidian_mcp_embed_queue_depth`,
  `obsidian_mcp_tool_calls_total{tool=...}`, `obsidian_mcp_tool_duration_seconds{tool=...}`.
  Skip unless we actually wire up scraping.

**Effort.** ½ day without metrics, +½ day with.

---

## 10. Cross-encoder reranking (optional, post-hybrid-search)

**Intent.** After hybrid search lands and we have data on its quality, add
a small cross-encoder rerank pass for the top-50 → top-K.

**Why.** RRF on tsvector + bge-m3 is already strong. Reranking adds
another quality bump on hard queries — the kind where the agent is
asking a complex conceptual question and the top-5 are all close.

**Implementation notes.**
- Defer until usage data confirms hybrid search quality is the bottleneck.
- Two options:
  - Use Ollama with a small instruct model (e.g. qwen2.5:1.5b) and a tight
    rerank prompt.
  - Run a lightweight cross-encoder via `sentence-transformers`
    (e.g. `BAAI/bge-reranker-v2-m3`) in-process. Adds a hefty Python
    dependency and ~500 MB of RAM.
- Either way, expose as `search(..., rerank=False)` (off by default until
  it's clearly worth the latency).

**Effort.** 1 day if we go with Ollama; 2+ days for an in-process model.

---

## 11. Misc hardening

Small items that don't need their own roadmap section but are worth doing:

- **API key not in URL.** `POST /admin/keys/create` redirects to
  `/admin/keys?new_key=<raw>`, putting the secret in browser history and
  Traefik access logs. Switch to a one-shot session/flash cookie or render
  the key directly in the response template instead of the URL.
- **`last_used_at` write per request.** `APIKeyMiddleware` issues an
  `UPDATE + COMMIT` on every authenticated MCP request. Move to an
  in-memory `dict[key_id, timestamp]` flushed every 30 s by a background
  task. Personal-use scale doesn't need it; gets bad if usage grows.
- **Usage logs need a success/error column.** Today `_tracked` doesn't
  catch exceptions, so failed tool calls aren't logged. Add a `status`
  column (`"ok" | "error"`) and an optional `error` text. Useful for
  debugging.
- **Embeddings table cleanup on note delete.** ON DELETE CASCADE is set on
  `note_embeddings.note_id`, but verify it actually fires through SQLAlchemy
  by using `passive_deletes=True` on the relationship. Spot check after a
  vault rename.
- **`reembed_confirm` idempotency.** If two reembed clicks land within
  60 s, two background tasks run. Add a module-level `asyncio.Lock` or a
  DB sentinel.
- **Pin requirements via lockfile in container.** For reproducible builds,
  generate a real lockfile from inside the container venv
  (`docker run --rm <image> pip freeze > requirements-lock.txt`) and have the
  Dockerfile install from it instead of the loose `requirements.txt`. (A prior
  `requirements-lock.txt` was a stray host `pip freeze` and has been removed.)

**Effort.** ½ day across the lot.

---

## Suggested order

1. **Tag + frontmatter filters** (separate proposal). Unblocks structured queries. ✅ shipped.
2. **Wikilink graph + tools** (separate proposal). Biggest "this is Obsidian, not just markdown" win. ✅ shipped.
3. **Vault write completion.** Atomic writes + `dry_run` / `replace_all` / `section` on `edit_note` + `move_note` / `delete_note` / `set_frontmatter`. ✅ shipped.
4. **File-watching + parallel embeddings.** Quality-of-life. Quick.
5. **Heading-aware chunking.** Triggers a Force Re-embed All; combine
   with #4 so it doesn't take all day.
6. **MCP resources + structured returns.** Polish; nice for client UX.
7. **HNSW index + ORM standardization.** Future-proofs scale.
8. **Health/observability.** Operational hygiene.
9. **Reranking** (only if measured quality is the bottleneck).
10. **Misc hardening.** As-and-when.

The git-auto-commit / `note_revisions` sub-items of (5) and the `bulk_edit`
sub-item of (6) were considered and explicitly NOT shipped; see those
sections for the reasoning.
