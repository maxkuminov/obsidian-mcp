## Context

The MCP server is markdown-only: the indexer globs `*.md` and skips dot-dirs; all 17 tools return a plain `str`; there is no binary handling and no MCP *resources* registered. The vault holds ~12,800 non-markdown files. The user-facing ones (PDFs — median 0.29 MB / p90 1.9 MB, images, skill HTML/JS) live in normal folders (`Reference Docs/`, `Projects/`, `Skills/`, `Attachments/`); the heavy `.py`/`.pyc`/`.so`/`.ajson` noise lives entirely in dot-dirs (`.venv-larisa`, `.smart-env`, `.cursor`) the indexer already skips.

Two driving use cases: (1) grab an existing PDF/HTML so a client-side skill can consume it; (2) save a generated file (e.g. an output PDF) back into the vault.

Existing building blocks to reuse: `vault.validate_path()` (path-traversal guard), `vault.write_file()` (atomic temp-file + `os.replace`, with EXDEV fallback), and the indexer's dot-dir rule (`any(part.startswith(".") for part in rel.parts)`).

## Goals / Non-Goals

**Goals:**
- Read, write, and browse arbitrary files in the vault from any MCP client, with no dependency on the client sandbox reaching an external HTTP endpoint.
- Keep the new surface as distinct peer tools, not fused into existing note tools.
- Stay safe: no path traversal, no dot-dir exposure, no accidental clobbering, bounded response sizes.

**Non-Goals:**
- No server-side PDF/text extraction. `read_file` is pure byte transport; a client skill decodes and parses.
- No embedding, semantic search, or keyword indexing of non-markdown files.
- No new HTTP download/upload endpoints; no database schema, ORM, or indexer changes.

## Decisions

### Inline MCP transport over out-of-band HTTP URLs
Bytes travel inside the tool result (text / base64 / image block). **Why:** universal — works regardless of whether the client sandbox can `curl`. A signed-URL approach would be more efficient for large files but breaks the moment the client can't reach the host. Trade-off accepted: base64 inflates ~33% and passes through model context, so transport is practical only for small/medium files (hence the read cap).

### Three distinct peer tools, not generalized note tools
`read_file` / `write_file` / `list_files` are new; `read_note` / `create_note` / `list_notes` stay markdown-only and unchanged. **Why:** matches the project's established "distinct peer tools over fused primary tools" preference and keeps each tool's return shape coherent. Alternative (overloading `read_note` to branch on extension) was rejected as the fused-tool anti-pattern.

### `read_file` return shape: rich image blocks, base64 otherwise
`auto` → text for text-like MIME types, an inline MCP image content block for images (so scans/illustrations render in-client), base64 string for everything else. **Why:** images are the one type where rendering is high-value and cheap (the client downsamples; vision tokenization compresses), whereas PDFs/other binaries only need to reach a skill as bytes. PDF-as-MCP-document-block was rejected — cross-client support is uncertain. This makes `read_file` the first tool returning MCP content objects rather than `str`; FastMCP supports returning an image/content object or list.

### MIME detection: stdlib + magic-byte sniff
Use `mimetypes.guess_type` for the text/binary/image decision, confirmed by a short magic-byte sniff for the common image signatures (PNG, JPEG, GIF, WebP). **Why:** no new dependency; `mimetypes` alone is extension-trusting, so the sniff guards against mislabeled images.

### Size caps: read 10 MB, write 25 MB, configurable
`MAX_FILE_READ_BYTES` (10 MB, matches existing `MAX_NOTE_BYTES`) and `MAX_FILE_WRITE_BYTES` (25 MB) in `config.py`. Read is checked against on-disk size before reading; write against decoded byte length. **Why:** read cap covers all but ~12 vault files and guards against context blowups; write is intentional so it gets a higher ceiling.

### `write_file`: no-clobber + auto-create parents, atomic
Default `overwrite=False` errors if the target exists; missing parent folders are created; the write routes through the existing atomic `vault.write_file()`. **Why:** safest default against data loss while keeping "save to a new `Outputs/` folder" frictionless.

### Shared safety helper for dot-dirs
A small helper rejects any path whose resolved components include a dot-dir, applied in all three tools (and as a filter in `list_files`), layered on top of `validate_path()`. **Why:** one consistent visibility rule matching the indexer; keeps `.obsidian` config/secrets, `.git`, `.trash`, and `.smart-env` out of reach.

## Risks / Trade-offs

- **Base64 context cost** → A multi-MB PDF inlined as base64 can exceed the context window. Mitigation: read cap + tool docstring instructs checking size via `list_files` first; document that base64 reads are token-heavy.
- **Model misreads base64 PDF as "readable"** → The model cannot interpret PDF bytes. Mitigation: docstring states `read_file` returns opaque bytes for non-text/non-image types, intended for a skill to decode; no server-side extraction implied.
- **`list_files` on a large folder** → Could return a huge list. Mitigation: `limit` (default 200) with explicit truncation indicator; non-recursive default.
- **MIME misdetection** → An image with a wrong extension or a text file sniffed as binary. Mitigation: magic-byte sniff for images; `encoding="text"`/`"base64"` overrides always available.
- **New return type for `read_file`** → First non-`str` tool; must confirm the MCP/FastMCP wrapper and usage-logging path handle image/content returns. Mitigation: covered by tasks (verify content-block return + logging).

## Migration Plan

Additive only — no schema, indexer, or data migration. Deploy via the normal `make deploy`. New env vars have defaults, so existing `.env` files work unchanged. Rollback = revert the code; no state to undo.

## Open Questions

None blocking. Possible follow-ups (out of scope here): filename-in-search indexing for discovery, a feature flag to disable file tools, and optional server-side text extraction — all deferred pending real usage.
