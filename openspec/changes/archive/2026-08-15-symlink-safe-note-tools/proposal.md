## Why

`validate_path` resolves the requested path and every mutating tool then acts
on the *resolved* path. An in-vault symlink `alias.md → important.md` therefore
makes `delete_note("alias.md")`, `edit_note("alias.md", …)`,
`set_frontmatter`, `move_note`, and `write_file` act on `important.md` while
reporting success for `alias.md` — a destructive write on a path nobody named
(found while hardening `delete_file` in #50; issue #54). Escaping symlinks are
already rejected by the containment check; this is about aliases inside the
vault. Symlinked *directories* inside a vault are a common Obsidian setup
(shared attachment folders) and must keep working.

## What Changes

- **Rule:** mutating tools act on the **final component as named** and **refuse
  when it is a symlink**, with an error naming the link target so the agent can
  operate on the real note. Symlinked ancestor directories remain allowed when
  they resolve inside the vault. Reads (`read_note`, `read_file`, `list_*`,
  graph tools) keep following links (harmless, and what users expect from an
  alias).
- Applies to `create_note`, `edit_note` (all modes), `set_frontmatter`,
  `move_note` (source and destination), `delete_note`, `write_file`; `delete_file`
  already refuses (uses the anchored helper).
- Implementation (design D2): one helper in `src/services/vault.py` —
  `validate_mutable_path(rel, user_id) -> Path`. It normalises `rel`
  (`PurePosixPath`; rejects absolute, NUL, trailing slash, and `..` — an
  in-vault `..` gets its own message naming the normalised path), **resolves
  the parent directory** and requires it to stay inside the resolved vault root
  (this is where in-vault symlinked ancestors are permitted and escaping ones
  become the traversal error), applies the hidden-path check to the resolved
  relative path, `os.lstat`s the **leaf** — `S_ISLNK` → refuse, naming the
  canonical vault-relative target — and returns **`resolved_parent / name`**:
  the real directory entry the indexer sees, so `_atomic_write` gets a real
  directory for its temp file and `move_note`'s DB paths line up with
  `notes_metadata`. Resolving once also means an allowed symlinked ancestor is
  never re-traversed during the mutation — which requires the tools to act on
  the returned `Path`, so the read-modify-write pairs use `read_bytes_at` /
  `write_file_at` / `write_bytes_at` rather than re-passing the caller's string.
- Docs: `CLAUDE.md` write-tools section, tool docstrings one sentence.

## Capabilities

### New Capabilities

_None._

### Modified Capabilities

- `vault-write`: new requirement — mutating note tools act on the named path and refuse a symlinked final component; existing scenarios unchanged.
- `file-access`: `write_file` joins the same rule (delete_file already has it).

## Impact

- `src/services/vault.py` (`validate_mutable_path`), `src/mcp_server/tools.py` (six call sites), `src/mcp_server/server.py` docstrings, `CLAUDE.md`, tests (per tool: alias final component → refused, target byte-identical; symlinked ancestor dir resolving inside the vault → still writable; escaping symlink → still rejected; dangling symlink at a create/write destination → refused with a clear message, no write).
- Adversarial-Codex trigger: write tools.
