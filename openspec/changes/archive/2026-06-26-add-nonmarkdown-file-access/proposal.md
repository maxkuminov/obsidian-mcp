## Why

The vault holds ~12,800 non-markdown files (PDFs, scans, illustrations, skill HTML/JS, data files), but the MCP server is markdown-only end to end — the indexer globs `*.md`, every tool returns a plain string, and there is no binary handling. Clients cannot grab an existing PDF/HTML to feed a skill, nor save a generated file (e.g. an output PDF) back into the vault. These files cannot be embedded or semantically indexed, but raw read/write/browse access is both feasible and valuable.

## What Changes

- Add three new **peer** MCP tools (`17 → 20`), exposed alongside the existing note tools:
  - **`read_file(path, encoding="auto")`** — returns any file's contents inline. `auto` resolves text-like types to text, images to an inline MCP image block (renders in-client), and all other binaries to a base64 string. `text`/`base64` force the form. Read cap (default 10 MB).
  - **`write_file(path, content, encoding="base64", overwrite=False)`** — lands a file in the vault. No-clobber by default; auto-creates parent folders; atomic write. Write cap (default 25 MB).
  - **`list_files(folder=".", pattern="*", recursive=False, limit=200)`** — `ls`-style browser returning subdirectories and files with size + mtime, glob-filterable, result-capped.
- All three tools **block dot-dirs** (`.obsidian`, `.git`, `.trash`, `.smart-env`, …), consistent with the indexer's existing visibility rule, and reuse the vault path-traversal guard.
- Two new configurable settings: `MAX_FILE_READ_BYTES` (default 10 MB) and `MAX_FILE_WRITE_BYTES` (default 25 MB).
- `get_vault_guide` mentions the new file tools so clients discover the capability.

Non-goals (explicitly out of scope): no server-side PDF/text extraction, no embedding or semantic/keyword indexing of non-markdown files, no new HTTP download/upload endpoints, no database schema or indexer changes.

## Capabilities

### New Capabilities
- `file-access`: Raw read, write, and browse access to arbitrary (non-markdown and markdown) files in the vault via MCP tools, including binary transport (base64 / inline image blocks), size caps, dot-dir exclusion, and path-traversal safety.

### Modified Capabilities
- `vault-guide`: The onboarding guide returned by `get_vault_guide` SHALL reference the new file-access tools so agents discover that raw file read/write/browse is available.

## Impact

- **Code**: new tool implementations in `src/mcp_server/tools.py` and registration in `src/mcp_server/server.py`; helper(s) in `src/services/vault.py` for dot-dir checks and binary read (reusing existing `validate_path` and atomic `write_file`); MIME detection via stdlib `mimetypes` + magic-byte sniff for images; `get_vault_guide` text update.
- **Config**: `MAX_FILE_READ_BYTES`, `MAX_FILE_WRITE_BYTES` in `src/config.py` (pydantic-settings) and `.env` docs.
- **Return types**: `read_file` introduces MCP content objects (image blocks) — the first tool not returning a plain `str`.
- **No impact** to: database schema/ORM, the indexer, embeddings, full-text search, migrations.
