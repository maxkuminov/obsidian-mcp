## 1. Config

- [x] 1.1 Add `max_file_read_bytes: int = Field(10 * 1024 * 1024, ge=1)` and `max_file_write_bytes: int = Field(25 * 1024 * 1024, ge=1)` to `Settings` in `src/config.py` (env: `MAX_FILE_READ_BYTES`, `MAX_FILE_WRITE_BYTES`).
- [x] 1.2 Document both vars in `.env.example` / `.env` docs and in `CLAUDE.md`'s key-decisions section.

## 2. Vault service helpers

- [x] 2.1 Add a dot-dir guard helper in `src/services/vault.py` (e.g. `is_hidden_path(rel) -> bool` / `validate_visible_path()`) that rejects any path whose vault-relative components include a part starting with `.`, layered on top of `validate_path`. Reuse the indexer's existing rule.
- [x] 2.2 Add `read_bytes(path) -> bytes` (or extend read) that validates path + dot-dir, stat-checks size against the cap before reading, and returns raw bytes.
- [x] 2.3 Add `write_bytes(path, data, overwrite)` that validates path + dot-dir, enforces no-clobber unless `overwrite`, creates missing parent dirs, and routes through the existing atomic temp-file + `os.replace` write.
- [x] 2.4 Add a `list_dir(folder, pattern, recursive, limit)` helper returning entries (relative path, is_dir, size, mtime), excluding dot-dirs, applying the glob, capping at `limit`, and reporting truncation.
- [x] 2.5 Add MIME helpers: classify a path as text-like / image / other via `mimetypes` plus a magic-byte sniff for PNG/JPEG/GIF/WebP.

## 3. MCP tools

- [x] 3.1 Implement `read_file_impl(path, encoding="auto")` in `src/mcp_server/tools.py`: resolve `auto` (text-like → text, image → MCP image content block, else → base64 string), honor forced `text`/`base64`, return clear errors for missing file, over-cap size, and non-UTF-8 under `encoding="text"`. Return an MCP image/content object for images.
- [x] 3.2 Implement `write_file_impl(path, content, encoding="base64", overwrite=False)`: base64-decode or treat as text, enforce `MAX_FILE_WRITE_BYTES` on decoded length, no-clobber default, auto-create parents, atomic write; clear errors for invalid base64 and existing-file-without-overwrite.
- [x] 3.3 Implement `list_files_impl(folder=".", pattern="*", recursive=False, limit=200)`: list immediate children (files + subdirs) by default, include size + mtime, distinguish dirs, filter by glob, cap + truncation note, reject dot-dir `folder`.
- [x] 3.4 Add `MAX_FILE_READ_BYTES`/`MAX_FILE_WRITE_BYTES` references via `settings`; keep `MAX_NOTE_BYTES` untouched.

## 4. Registration & guide

- [x] 4.1 Register `read_file`, `write_file`, `list_files` as `@mcp.tool()` peers in `src/mcp_server/server.py` with thorough docstrings (incl. base64 token-cost note for `read_file`, "opaque bytes for a skill to decode" framing, neutral cross-references).
- [x] 4.2 Wire the three new `_impl` functions into the imports in `server.py`.
- [x] 4.3 Update `get_vault_guide_impl` text to mention `read_file` / `write_file` / `list_files` with neutral framing and note they work on non-markdown files.
- [x] 4.4 Confirm usage logging records the three new tool names (verify the logging path handles non-`str` / content-block returns from `read_file`).

## 5. Tests

- [x] 5.1 `read_file`: text-like → text, image → image content block (assert MIME), pdf/binary → base64; forced `base64` on text; missing file error; over-`MAX_FILE_READ_BYTES` error reports size + path; non-UTF-8 under `text` errors.
- [x] 5.2 `write_file`: new binary via base64 round-trips; text mode; parent-dir creation; no-clobber error leaves existing file unchanged; `overwrite=True` replaces atomically; invalid base64 errors and writes nothing; over-`MAX_FILE_WRITE_BYTES` errors and writes nothing.
- [x] 5.3 `list_files`: non-recursive lists files+subdirs with size/mtime; `pattern="*.pdf"` filters; `recursive=True` descends; `limit` caps + truncation indicated.
- [x] 5.4 Safety: path traversal (`../`) rejected for all three; dot-dir path rejected for read/write; `list_files` hides dot-dirs and rejects a dot-dir `folder`.
- [x] 5.5 `get_vault_guide` response mentions the three new tools.

## 6. Verify

- [x] 6.1 Run the test suite; run `openspec validate add-nonmarkdown-file-access --strict`.
- [x] 6.2 Manual smoke against a running server: `list_files` a folder, `read_file` a PNG (renders) and a PDF (base64), `write_file` a small PDF then re-read it; confirm caps and dot-dir blocks behave.
