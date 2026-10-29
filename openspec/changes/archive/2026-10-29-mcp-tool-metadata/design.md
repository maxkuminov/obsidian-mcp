## Context

Issue #343. The published tool surface carries no `title`, no `ToolAnnotations` and no server `instructions`, and three descriptions exceed the 4,096-character cutoff Claude Code applies to upfront tool descriptions. Everything below was verified on 5a391ca (`main`, migration 030 present).

### What the code does today (verified)

| Fact | Where |
| --- | --- |
| 25 `@mcp.tool()` registrations, every one bare — no `title=`, no `annotations=`, no `description=` | `src/mcp_server/server.py:113` … `:1252` (`grep -c "@mcp.tool" src/mcp_server/server.py` → 25; `grep -n "title=\|annotations" src/mcp_server/server.py` → none on a registration) |
| `FastMCP("obsidian-vault", stateless_http=True, streamable_http_path="/", max_request_body_size=…, transport_security=…)` — no `instructions=` | `src/mcp_server/server.py:92-110` |
| Post-registration pass over `server._tool_manager.list_tools()` already exists (`_forbid_unknown_arguments`, #295) | `src/mcp_server/server.py:1289-1324` |
| `_tracked(tool_name, param_keys, transforms, refusal_result, write_class=False, arg_char_caps, resource_class)` is the gate order for every tool; `write_class` marks the eight tools that pass the write bucket | `src/mcp_server/tools.py:1454-1462`, docstring at `:1479-1486` ("Eight tools carry it — the five note tools, `write_file`, `delete_file` and `import_from_url`") |
| The write bucket is charged in `_bucket_admission(write_class)` | `src/mcp_server/tools.py:1302-1331` |
| `write_class=True` on exactly: `create_note` (`:3235`), `edit_note` (`:3975`), `move_note` (`:4756`), `delete_note` (`:5657`), `set_frontmatter` (`:5854`), `write_file` (`:6211`), `import_from_url` (`:6858`), `delete_file` (`:6979`) | `src/mcp_server/tools.py` |
| `concurrency.TOOL_CLASSES` maps the same eight names to the `"write"` class, and the remaining seventeen to `embedding` / `vector` / `scan` / `light` | `src/services/concurrency.py:40-51` |
| Read-only enforcement: `_require_write()` refuses with `permission_denied` unless `current_permission.get() == "readwrite"`; called inside the body of all eight write tools and inside `_mint_preflight(need_write=True)` for `request_upload` / `import_from_url` | `src/mcp_server/tools.py:2821-2866`; callers at `:3257, :4087, :4798, :5679, :5897, :6233, :6484, :6998` |
| `current_permission` is a `ContextVar` set from `api_key.permission` or the OAuth token's scope | `src/mcp_server/auth.py:42`, `:955`, `:1178` |
| `check_upload` reads a `transfer_tokens` row and re-checks liveness; it writes nothing | `src/mcp_server/tools.py:6629-6700` |
| `request_upload` / `request_download` insert a `transfer_tokens` row (mint a capability); `request_upload` takes `overwrite` and is preflighted with `need_write=True`, `request_download` is not | `src/mcp_server/tools.py:6544-6551`, `:6764` |
| `import_from_url(url, path, overwrite=False)` fetches a public https URL and is no-clobber unless `overwrite=True` | `src/mcp_server/server.py:1214-1218` |
| `write_file(path, content, encoding, overwrite=False, expected_hash)` — `overwrite=True` replaces an existing file | `src/mcp_server/server.py:1005-1012` |
| `create_note(path, content, expected_hash=None)` — refuses an existing path; `expected_hash` is always refused as `no_incumbent` | `src/mcp_server/server.py:349-351`; `docs/architecture/vault-tools.md:42` |
| `src/mcp_stdio.py` imports the same `mcp` instance | `src/mcp_stdio.py:18` |

### What the pinned SDK offers (verified against `.venv`, `mcp` 1.30.0; `requirements.txt:21` pins `mcp[cli]==1.30.0`)

- `FastMCP.tool(self, name=None, title=None, description=None, annotations: ToolAnnotations | None = None, icons=None, meta=None, structured_output=None)`.
- `FastMCP.__init__(self, name=None, instructions=None, website_url=None, icons=None, …)`; the value is readable as `mcp.instructions`.
- `mcp.types.ToolAnnotations` fields: `title`, `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint`, **all defaulting to `None`** — so "set explicitly" means a `bool` on every one; `None` is the spec default, which is the worst case.
- `Tool.from_function`: `func_doc = description or fn.__doc__ or ""`. **The wire description is the raw docstring**, leading indentation and all; `inspect.cleandoc` is never applied. Every length in this change is therefore measured on `tool.description` as the registry holds it, which is what a client receives.
- `FastMCP.list_tools()` builds `types.Tool(name, title, description, inputSchema, outputSchema, annotations, icons, _meta)` from the registry — `title` and `annotations` go to the wire unchanged.

### Description lengths on 5a391ca

Measured with an AST walk over `src/mcp_server/server.py` (`ast.get_docstring(node, clean=False)` for the raw/wire length, `inspect.cleandoc` for the clean one):

| tool | line | raw (wire) | cleandoc |
| --- | ---: | ---: | ---: |
| keyword_search | 114 | 846 | 797 |
| read_note | 142 | **5,400** | 5,091 |
| list_notes | 235 | 669 | 624 |
| get_tags | 259 | 130 | 117 |
| get_recent | 269 | 520 | 479 |
| semantic_search | 294 | 2,409 | 2,272 |
| create_note | 349 | 2,453 | 2,312 |
| edit_note | 398 | **10,458** | 9,857 |
| get_vault_guide | 589 | 538 | 505 |
| get_backlinks | 604 | 372 | 347 |
| get_links | 618 | 999 | 934 |
| get_neighborhood | 645 | 607 | 566 |
| find_related | 663 | 1,696 | 1,595 |
| find_orphans | 699 | 314 | 289 |
| move_note | 712 | **5,316** | 5,019 |
| delete_note | 811 | 2,880 | 2,711 |
| set_frontmatter | 871 | 3,764 | 3,551 |
| read_file | 946 | 2,598 | 2,445 |
| write_file | 1005 | 3,136 | 2,959 |
| list_files | 1069 | 1,125 | 1,052 |
| request_upload | 1103 | 1,928 | 1,811 |
| check_upload | 1148 | 1,597 | 1,500 |
| request_download | 1183 | 1,307 | 1,226 |
| import_from_url | 1214 | 1,869 | 1,756 |
| delete_file | 1253 | 1,617 | 1,520 |
| **total (25)** | | **54,548** | **51,335** |

The issue's figures (51,335 / 9,857 / 5,091 / 5,019 / 3,551) are the `cleandoc` column. The wire column is the one the cap applies to; it is larger by the indentation, and on it **three** tools exceed 4,000 (`edit_note`, `read_note`, `move_note`), not four. `set_frontmatter` (3,764) is under the cap but is restructured with the other three so that all four `expected_hash`-bearing note tools lead the same way.

## Goals / Non-Goals

**Goals**
- Every tool tells an annotation-aware client what it is: read-only or not, destructive or not, idempotent or not, open-world or not — with the spec's worst-case defaults replaced by explicit booleans.
- A client that loads only names and instructions at session start has enough to begin correctly.
- No description is cut off by a 4,096-character client limit, and the part a model reads first is the contract.
- Nothing about any tool's name, arguments, schema or behaviour changes. Every sentence an existing spec pins in these docstrings survives.

**Non-Goals**
- Changing enforcement. Annotations never gate anything server-side.
- Changing `tools.py` docstrings, the vault guide, or any tool's result text.
- Adding a setting.

## Decisions

### D1. The annotation table

Four shapes cover all 25 tools; each is one shared `ToolAnnotations` constant in `server.py`:

| constant | readOnlyHint | destructiveHint | idempotentHint | openWorldHint |
| --- | --- | --- | --- | --- |
| `_READ_ONLY` | true | false | true | false |
| `_ADDITIVE` | false | false | false | false |
| `_DESTRUCTIVE` | false | true | false | false |
| `_DESTRUCTIVE_OPEN_WORLD` | false | true | false | true |

| tool | shape | title | why |
| --- | --- | --- | --- |
| `keyword_search` | `_READ_ONLY` | Keyword search | Reads the index. |
| `semantic_search` | `_READ_ONLY` | Semantic search | Reads the index. The embedding call is server infrastructure, not the tool's domain (L5). |
| `read_note` | `_READ_ONLY` | Read note | Reads a file. |
| `list_notes` | `_READ_ONLY` | List notes | Reads the index. |
| `get_tags` | `_READ_ONLY` | List tags | Reads the index. |
| `get_recent` | `_READ_ONLY` | Recent notes | Reads the index. |
| `get_vault_guide` | `_READ_ONLY` | Vault guide | Reads the primer and the vault's `CLAUDE.md`. |
| `get_backlinks` | `_READ_ONLY` | Backlinks | Reads `note_links`. |
| `get_links` | `_READ_ONLY` | Outgoing links | Reads `note_links`. |
| `get_neighborhood` | `_READ_ONLY` | Link neighborhood | BFS over `note_links`. |
| `find_related` | `_READ_ONLY` | Related notes | Vector query. |
| `find_orphans` | `_READ_ONLY` | Orphan notes | Reads `note_links`. |
| `read_file` | `_READ_ONLY` | Read file | Reads a file. |
| `list_files` | `_READ_ONLY` | List files | Lists a directory. |
| `check_upload` | `_READ_ONLY` | Check upload | Reads a `transfer_tokens` row; writes nothing (`tools.py:6629-6700`). |
| `create_note` | `_ADDITIVE` | Create note | Writes vault bytes (write bucket) but refuses an existing path, so it only ever adds. |
| `request_upload` | `_DESTRUCTIVE` | Request upload link | Mints a capability (a DB row) — not read-only; with `overwrite=True` the capability overwrites an existing file when redeemed. **Owner decision (Codex spec round 1, 2026-10-28): destructive**, because the tool call is what authorises the overwrite, and the person redeeming the link is not the one the hint protects (L4). |
| `request_download` | `_ADDITIVE` | Request download link | Mints a capability — not read-only; nothing is modified. |
| `edit_note` | `_DESTRUCTIVE` | Edit note | Replaces note content; `dry_run` is a per-call mode, not a per-tool fact (L3). |
| `move_note` | `_DESTRUCTIVE` | Move note | Renames; with `rewrite_links=True` rewrites other notes' bodies. |
| `delete_note` | `_DESTRUCTIVE` | Delete note | Removes a note. `.trash/` is an undo path, not a reason to call the delete non-destructive. |
| `set_frontmatter` | `_DESTRUCTIVE` | Set frontmatter | Rewrites the frontmatter block; YAML comments are lost. |
| `write_file` | `_DESTRUCTIVE` | Write file | `overwrite=True` replaces an existing file. |
| `delete_file` | `_DESTRUCTIVE` | Delete file | Removes a file; same `.trash/` rule as `delete_note`. |
| `import_from_url` | `_DESTRUCTIVE_OPEN_WORLD` | Import from URL | `overwrite=True` replaces an existing file, and the tool fetches the public internet. |

**Why `idempotentHint` is `true` for every read and `false` for everything else.** The hint means "calling repeatedly with the same arguments has no additional effect on the environment". Every read tool satisfies that trivially. Among the rest, `delete_*` and `set_frontmatter` arguably satisfy it too, but a wrong `true` lets a client re-run a write without asking while a wrong `false` costs one confirmation; the hint is set in the safe direction, uniformly (L6).

**Why `openWorldHint` is `true` only for `import_from_url`.** It is the only tool whose arguments name something outside the vault that the server then goes and interacts with. `request_download` publishes a link a person uses, but the tool's interaction is with the vault and the token table.

**The precise relationship to the write bucket.** Let `W` be the set of **production** tools registered with `_tracked(write_class=True)` — the write-bucket set, eight tools, identical to the `"write"` class of `concurrency.TOOL_CLASSES`. Then:

- `{t : destructiveHint}` = `(W − {create_note}) ∪ {request_upload}` (eight tools);
- `{t : not readOnlyHint}` = `W ∪ {request_upload, request_download}` (ten tools);
- `{t : openWorldHint}` = `{import_from_url}`;
- `{t : idempotentHint}` = `{t : readOnlyHint}` (fifteen tools).

The test asserts all four equalities against `W` read from the code, not from a list in the test (D2).

**`title` vs `annotations.title`.** The spec's display precedence is `title`, then `annotations.title`, then `name`. The top-level `title` is set on every registration; `annotations.title` is left `None` so the four shape constants can be shared. The test accepts `annotations.title is None or annotations.title == title`.

### D2. Where the metadata lives, and how the test reads the write set

Each registration carries its own `title=` and `annotations=` as decorator kwargs — the public `FastMCP.tool` API, beside the docstring it describes, so a reader of `server.py` sees name, title, hints and contract in one place. A post-registration pass in the style of `_forbid_unknown_arguments` was considered and rejected: that pass exists because the SDK offers no public way to set `extra="forbid"`, whereas here the public API is exactly the right shape, and a mapping dict beside the registrations would be a second place for the same fact. The guarantee that a tool added later is covered comes from the test, not from a pass: a registration without `title` or with any hint `None` fails `tests/test_issue_343_tool_metadata.py`.

So the test can reconcile the destructive set against the write-bucket set without a hand-kept list, `_tracked` stamps the wrapper it returns with `__write_class__ = write_class` beside the `__tracked_tool__ = tool_name` stamp it already carried for `test_issue_66`, and `tools.py` exposes `production_write_bucket_tools() -> frozenset[str]`, which enumerates **only** the stamped callables that are module attributes of `src.mcp_server.tools` with `__module__ == "src.mcp_server.tools"` (the `functools.wraps` copy of the implementation's own module) and returns the names of those with `write_class=True`. A module-global set populated by every decorator call was the first draft and was rejected in Codex spec review (round 1, MAJOR): `tests/test_concurrency_provenance.py` (`write_probe`, `blocking_write_probe`) and `tests/test_mcp_rate_limits.py` (`rate_probe_write`) decorate probes with `_tracked(write_class=True)` at module scope, so under a full-suite run such a set depends on collection order and contains non-production names. The wrapper-attribute form is immune: a probe defined in a test module carries the test module's `__module__` and is not an attribute of `tools`, and a probe decorated under a production name (`_tracked("edit_note", [], write_class=False)`) is likewise never enumerated. The metadata test reads `production_write_bucket_tools()`, asserts it equals `{t for t, c in concurrency.TOOL_CLASSES.items() if c == "write"}` and is a subset of the registered names, and — in the same module — decorates both kinds of probe itself and asserts the derived set is unchanged, so the three classifications that all mean "changes vault bytes" — the bucket, the concurrency class and `destructiveHint` (less `request_upload`, plus `create_note`) — cannot drift apart silently and cannot be moved by a test.

### D3. The instructions constant

`SERVER_INSTRUCTIONS` is a module-level `str` in `src/mcp_server/server.py`, passed as `FastMCP("obsidian-vault", instructions=SERVER_INSTRUCTIONS, …)`. Its text, measured at 512 characters for the first paragraph (at the limit) and 1,428 in total (the two paragraphs joined by one blank line); the `expected_hash` sentence was qualified after the Codex adversarial round (`WRITE_PRECONDITION_REQUIRED=true` refuses an omitted hash with `precondition_required`, so "optional" is a default, not a law):

> Agent memory in a markdown Obsidian vault. Search with `keyword_search` or `semantic_search`; read with `read_note`; write with `create_note` or `edit_note` (`dry_run=true` first). `get_vault_guide` describes this vault's conventions (one call per session). `create_note` never overwrites. Editing tools take `expected_hash` (a read's `content_hash`) and refuse, writing nothing, if it changed; optional by default (an unguarded stale edit overwrites) unless the deployment requires it (`precondition_required`).
>
> Round trips: only `content` from a complete whole-note `read_note` (`offset=0`, `truncated=false`) may go back as a full replacement; a section's `content` goes back only with the same `section=` selector; finish a truncated read before writing, or the rest of the note is lost. Server refusals end with one machine-readable line, `MCP-REFUSAL {"code":…}`; act on the code, not the prose (protocol-layer argument errors do not carry it). Non-markdown files use `read_file`, `write_file`, `list_files` and `delete_file`. `request_upload` and `request_download` mint short-lived links for a person; `import_from_url` fetches a public https URL into the vault. The link graph is `get_backlinks`, `get_links`, `get_neighborhood`, `find_related` and `find_orphans`. Deletes are soft (`.trash/`) unless `permanent=true`. Calls are rate-limited per credential; a rate refusal says when to retry and spends no daily quota.

Four constraints shaped it:

- **Truth (#131).** Every sentence is checkable against `CLAUDE.md` or a docstring: "server refusals end with one `MCP-REFUSAL` line" is the `_tracked` contract and body-refusal rule (#263) and is qualified for the protocol-layer argument errors, which do not carry it (#295, L1 there); "spends no daily quota" is the rate-bucket rule; "`create_note` never overwrites" is the no-clobber invariant.
- **The round trip is stated with its conditions, and `expected_hash` is stated as optional** (Codex spec round 1, BLOCKER). The first draft said "pass its `content` back to `edit_note` unchanged" and "writes refuse to clobber"; followed literally after `read_note("n.md", limit=100)` with `truncated=true`, that replaces the whole note with the fragment, and with `WRITE_PRECONDITION_REQUIRED=false` (the default) nothing refuses it. The text now carries the three conditions the vault-write spec puts on both docstrings (whole-note: `offset=0` and `truncated=false`; a section's `content` only with the same `section=`; finish a truncated read first) and says in so many words that an unguarded stale edit overwrites.
- **Neutral framing (vault-guide spec, "Other tools reference the guide neutrally").** The guide is *described*, and the once-per-session advice is stated as sufficiency ("one call per session is enough"), not as an order. The MODIFIED requirement in this change extends that rule from peer-tool docstrings to the instructions, and the test checks the text contains neither "MUST call" nor "IMPORTANT".
- **The first 512 characters are the contract** because that is the figure the issue sets for the lead, and the lead must survive a client that shows only an excerpt. The test asserts the lead (first 512 characters) contains, case-insensitively: `memory`, `markdown`, `` `keyword_search` ``, `` `semantic_search` ``, `` `read_note` ``, `` `create_note` ``, `` `edit_note` ``, `dry_run`, `` `get_vault_guide` ``, `` `expected_hash` ``, `optional` and `overwrite`; that the whole text contains `truncated=false`, `offset=0` and `same \`section=\``; that it does **not** contain "back to `edit_note` unchanged"; and that `len(SERVER_INSTRUCTIONS) <= 2000`.

The implementer may re-word for accuracy if review finds a sentence that is not true; the length limits and the token list are the contract, not the exact prose.

### D4. The docstring restructure: what stays, what moves, where

Rules, applied to `edit_note`, `read_note`, `move_note` and `set_frontmatter`:

1. **The lead** — the first 1,500 characters of the wire description — carries the contract: the one-line purpose; the write-permission sentence (write tools); a compact mode/argument summary that names **every declared argument**; the `expected_hash` precondition and the `MCP-REFUSAL` line with its codes (write tools); the named refusals that write nothing; what success reports. For `read_note`: the field list (`content`, `heading`, `content_hash`, `error` at minimum) and the round-trip rule.
2. **The body** (up to the 4,000 cap) keeps every sentence D5 lists and the `Args:` block, which stays last in Google style — the lead's compact argument summary is in addition to it, not instead of it, so the `Args:` block itself need not fit in 1,500 characters. **Implementation deviation, `edit_note` only:** even after every D4 move the description measured 4,334 on the wire with its `Args:` block, so that block was dropped; the lead's "Arguments:" paragraph names all ten and each is explained in the mode list, which is what the vault-write "describe their use case and parameters" scenario asks for. `read_note`, `move_note` and `set_frontmatter` keep theirs.
3. **Nothing in the lead or body changes an argument name, a default, or a described behaviour.** Text is deleted or moved, never re-stated with different meaning. Where the moved text is already in the destination document (most cases), the docstring sentence is simply dropped and the destination is checked for completeness; where the destination lacks it, the sentence is added there in the same commit.

Paragraph by paragraph (line numbers on 5a391ca, `src/mcp_server/server.py`):

**`edit_note` (lines 410–552; 10,458 → ≤ 4,000)**

| paragraph | disposition |
| --- | --- |
| Opening sentence + write permission (410–411) | stays, lead |
| "See `get_vault_guide` …" (413–414) | stays, one sentence, neutral framing |
| Four modes (416–440) | stays, compressed to one sentence per mode in the lead; the selector forms (`Parent/Child`, `#N`, "bare #N never shadowed", Setext not matched) stay in the body in short form; the parity sentence with its qualifier stays verbatim-equivalent (D5) |
| "Frontmatter and the round trip" (442–451) | the thematic-break / mapping-shaped-fence examples and "the repair path needs no flag" **move** to `docs/architecture/vault-tools.md` § "Frontmatter is preserved unless the caller says otherwise" (already there in substance, line 50 ff.; verify and add any missing sentence). The docstring keeps: full replace preserves a valid block, `replace_frontmatter=True` overwrites it, a note with no valid block is replaced wholesale, use `set_frontmatter` or `find=` for the block, never write back the JSON view |
| "The round-trip guarantee covers a complete, unwindowed whole-note read only" (453–457) | stays (vault-write spec line 260–266 requires it in both layers), compressed to one sentence |
| "Section mode: what `content` replaces" (459–482) | stays in substance (D5 pins five statements); compressed |
| "Section mode resolves headings over the frontmatter-stripped body … fence grammar" (484–489) | fence-grammar detail (three spaces of indentation, closer length, unclosed column-zero fence) **moves** to `vault-tools.md` § "The fence grammar every consumer shares (#150)" (line 544 ff., already there). Keeps: ordinals agree with `read_note`; a heading inside a fenced block is not a heading |
| "Two shapes refuse a section write outright" (491–503) | stays; the list-item explanation ("this server does not parse container blocks") **moves** to `vault-tools.md` § "Resolved residual: the masker's narrower-than-CommonMark fence grammar (#150)" (line 1180). Keeps the shape ("indented by one to three spaces that nothing below it closes"), the write-nothing guarantee and the remedy |
| "Both refusals are asymmetric with reads" (505–507) | stays, one sentence (pinned by `test_the_edit_docs_keep_the_read_asymmetry_explicit`) |
| Flags (509–518) | stays, compressed into the lead's argument summary and the `Args:` block |
| "Writes are atomic … staged … same-directory rename … crash" (520–531) | **moves** to `vault-tools.md` line 48 (already there) and README "Read and write"; the docstring keeps "writes are atomic" and the in-call `File changed while editing` guard sentence with "that is not a guard on your read" |
| "`expected_hash` — binding a write" first paragraph (533–546) | stays, lead: omit → unchanged; pass the `content_hash` verbatim; refused writing nothing if the file changed; whole-file hash; the two windows in one sentence; the `MCP-REFUSAL` line and six codes |
| second `expected_hash` paragraph (548–560: "applies in all four modes … `#N` ordinals are positional … narrowest mode most conflict-prone … reports the `content_hash` of the bytes this call wrote") | the positional-ordinal reasoning **moves** to `vault-tools.md` § "Per tool: what it accepts, what it binds, what it reports" (line 315) and README "Guarding edits against stale reads" (line 315); the docstring keeps: applies in all modes including `dry_run`, checked before mode dispatch; a `section=` write binds the whole file; success reports the hash of the bytes this call wrote, `dry_run` reports none (vault-write spec line 2009 requires the "what this call wrote, not what is on disk" distinction) |
| `Args:` (562–578) | **dropped** (see rule 2's deviation note): the lead's "Arguments:" paragraph and the mode list carry every argument |

**`edit_note` — implementation deviations from the rows above** (openspec-verifier round 1; the cap is the cap, and after the Codex adversarial round's qualifications the description stood at 4,001). Restored, compressed: the `set_frontmatter` / `find=` / never-write-back-the-JSON-view sentence; "`#N` ordinals agree with `read_note`; a heading inside a fenced code block is not a heading"; and the remedy for the indented-fence refusal ("close the fence or unindent it to column zero, then reissue"). **Recorded as not in the description** — the content lives in `docs/architecture/vault-tools.md`: (b) the atomic-write / `File changed while editing` / two-windows narrative (the `concurrent_write` code stays in the `MCP-REFUSAL` list, which is the agent-actionable part); (c) "checked before mode dispatch, the size cap, the diff and every no-op branch" beyond "in every mode, `dry_run` included"; (f) "a bare `#N` is never shadowed by a heading whose text is `#N`" and "Setext headings are not matched" (the `read_note` description still carries the bare-`#N` rule in its `section` argument, and the two tools share one selector grammar). The Codex adversarial round added, in all three write descriptions and the instructions, the protocol-validation exception to the `MCP-REFUSAL` statement and the `WRITE_PRECONDITION_REQUIRED` qualification of "optional", and restored `move_note`'s bare-href / angle-bracket markdown-link restriction from 5a391ca.

**`read_note` (lines 148–230; 5,400 → ≤ 4,000)**

| paragraph | disposition |
| --- | --- |
| Opening + "structured result, read the fields" (148–153) | stays, lead |
| `content` / `heading` / metadata bullets (155–165) | stay, lead (D5: `content` is the body `edit_note(section=…)` accepts; the `heading` field is not part of `content`) |
| `frontmatter_yaml` bullet (166–177) | stays: LF-normalized with the residual declared (D5), authoritative copy, `frontmatter` JSON view lossy, use `set_frontmatter` or `edit_note(find=…)` (D5) — the enumeration of what has no faithful JSON form (dates, non-string keys, recursive aliases, surrogate escapes) **moves** to `vault-tools.md` § "The representability boundary: scrub once, at the parse (#149)" (line 1058, already there); the docstring says "`metadata_omissions` says which and why" |
| truncation bullet (178–182) | stays |
| `metadata_omissions` bullet (183–184) | stays |
| `content_hash` bullet (185–200) | stays in short form: whole-file hash in every mode; hand back verbatim, never compute it from `content`; the token the write tools accept. The "three things about it" reasoning (why a section write guarded with it is refused on any change, "that is the trade", the `read_file` byte-exact aside) **moves** to `vault-tools.md` § "Write preconditions: `expected_hash` and `content_hash` (#205)" (line 264, already there) and README "Guarding edits against stale reads" (already there) |
| `error` bullet (201–203) | stays, lead |
| "Budgets are per field" (205–209) | **moves** to `vault-tools.md` § "Three kinds of size cap" (line 452) and README "Response size limits" (line 1279); the docstring keeps "read one section with `section=` rather than paging a large note" |
| "Round trips" (211–218) | stays, compressed: whole-note `content` is exact input for full replace only when `offset=0` and `truncated` is false; a section `content` likewise; LF residual declared (D5) |
| `Args:` (220–229) | stays, including the selector forms for `section` |

**`move_note` (lines 718–797; 5,316 → ≤ 4,000)**

| paragraph | disposition |
| --- | --- |
| Opening + write permission (718–719) | stays, lead |
| "Updates `notes_metadata.file_path` and `note_links.target_path` …" (721–723) | **moves** to `vault-tools.md` line 44 (already there); the docstring says "the index follows the move; backlinks keep working without rewriting source notes" |
| `rewrite_links=True` link forms (725–733) | the enumeration (`[[Old]]` → `[[New]]`, alias, anchor, embed, path-style, markdown href relative to the linking note) **moves** to README "Read and write" (line 265, already there) and `vault-tools.md` line 44 (already there). The docstring keeps: every source that links here is rewritten in place, **the moved note's own body included** (D5), wikilinks, embeds and markdown links; aliases and anchors survive |
| Preflight bounds (735–740: 10 MiB per source, 256 MiB aggregate) | the numbers **move** to `vault-tools.md` § "Write tools" (add a sentence beside line 44 if absent; the vault-write spec "move_note bounds the rewrites it plans for any one source" at line 1701 is the source of truth). The docstring keeps: "refused before anything moves, naming the source, if a rewrite would push a note past the size limit or the aggregate planning bound" |
| Fence-opener refusal (742–750) | stays (D5 pins: "every source", "own body", "before the rename", `rewrite_links=True`/`False`, the indent shape, "nothing below it closes"); the list-item explanation moves with `edit_note`'s |
| "A rewrite can still fail after the move has committed" (752–759) | stays (vault-tools.md line 44: "The docstring must say so"), compressed to: partial success names the sources still pointing at the old path, the move is not rolled back, fix them with `edit_note` |
| Atomic + symlink (761–764) | stays, one sentence |
| `expected_hash` paragraph (766–777) | stays, lead: binds `from_path`'s bytes only (D5), the `MCP-REFUSAL` codes, **and the residual — "compared at preflight, before the rename; the rename pins inode identity but does not compare the bytes again, so an in-place edit after that comparison is moved"**. The first draft moved that sentence to `vault-tools.md` only; Codex spec round 1 (MAJOR) pointed out it limits what the precondition guarantees at call time, so it stays agent-visible in the first 1,500 characters beside the precondition and the test pins both halves (`pins inode identity`, `not compare` / `without comparing`). `vault-tools.md` § "Residuals, declared" (line 370) keeps the maintainer copy |
| Hash-reporting matrix (779–788) | stays compressed: success reports the hash of the bytes published at `to_path`; a failed rewrite stays a partial success and never claims nothing was written; if another writer changed `to_path`, no hash is reported and the result says to re-read. The full matrix is in `vault-tools.md` § "`move_note`'s post-rename partial-success contract" (line 335, already there) |
| `Args:` (790–797) | stays |

**`set_frontmatter` (lines 877–934; 3,764, under the cap)**

Restructure only: the `expected_hash` paragraph (908–917) and the precedence sentence (919–921) move up into the lead ahead of the malformed-block and no-op paragraphs, so the first 1,500 characters carry the precondition and the `MCP-REFUSAL` line; the `yaml.safe_dump(default_flow_style=False, sort_keys=False, allow_unicode=True)` kwargs (897–898) **move** to `vault-tools.md` line 45 (already says "round-trips via `yaml.safe_dump`"); the docstring keeps "PyYAML does not preserve YAML comments; they are lost on the first call". Everything else stays.

### D5. Sentences an existing spec requires in these four descriptions (must survive)

The restructure is constrained by these, each already pinned by a test that must stay green **unmodified**:

| requirement (spec) | what the description must still say | pinning test |
| --- | --- | --- |
| vault-write "Section-mode docstrings state the round-trip contract" (line 1604) | `edit_note`: in section mode `content` is the body only, beginning on the line after the heading line; the whole body is replaced so omitted content — a fenced block included — is deleted; a wanted blank line belongs in `content`; `read_note(section=…)` is the matching read, its `heading` field and `content` field named; byte-identity holds only for LF bodies, non-LF terminators in the selected body come back as LF | `tests/test_issue_149_read_note_framing.py::test_both_edit_note_layers_name_the_content_field_as_the_body` |
| note-read "Section selection" (line 106) and its scenarios "Docstrings teach the field round trip" / "A CRLF note's section round trip is not byte-identical" | `read_note` and `edit_note`: no "split on", no "drop the first line", no `\n---\n`; `read_note` names `set_frontmatter` and `frontmatter_yaml`; declares "LF-normalized" | `test_issue_149…::test_no_docstring_prescribes_a_textual_recovery`, `::test_both_read_note_layers_point_frontmatter_mutation_elsewhere`, `::test_both_read_note_layers_declare_the_frontmatter_lf_residual` |
| vault-write "`edit_note` supports four mutually exclusive modes", scenario at line 260 | both layers say section responses belong to section mode and a truncated read must be completed before a full-replace write | (prose check in `openspec-verifier`; no regex test today) |
| vault-write "Section writes are refused on a note with an unmatched indented fence opener", scenario "The refusal is disclosed where callers read" (line 1692) | `edit_note` and `move_note`: "indented", "one to three spaces", "nothing below it closes", "refus", "frontmatter" beside it, "nothing written"/"without writing"; `move_note`: "rewrite_links=true", "rewrite_links=false", "before the rename", "every source", "own body"; `edit_note`: a parity sentence (`selector parity` / `same section` / `the same selectors`) carrying its qualifier (`admits` / `admitted` / `not a promise` / `about resolution`) **in the same sentence**; "read_note" present | `tests/test_issue_150_docstrings.py` (all of it) |
| vault-write "Write-tool docstrings use neutral framing" (line 963) and vault-guide "Other tools reference the guide neutrally" (line 61) | no "MUST call" / "IMPORTANT: Call … first" about `get_vault_guide`; no tool called "primary" or "default" | (prose check) |
| vault-write "Write tools report the content hash of the bytes they published" (line 2005) | the reported hash is what this call wrote, not what is on disk when the caller reads the message | (prose check) |
| vault-write "`move_note` rewrites incoming wikilinks only when opted in" + `vault-tools.md:44` | partial success after the rename is disclosed; "the link graph never disagrees with the vault bytes" is not claimed of the whole call | (prose check) |
| note-filters "Symmetric, non-ranking docstrings on the two search tools" | untouched — the search tools are not restructured | — |
| #128 D7 | the registered `edit_note` takes `replace_frontmatter` and documents it | `tests/test_issue_128_edit_note_frontmatter.py::test_the_registered_edit_note_takes_the_parameter` and neighbours |
| #89 | every tool reference in a description names a registered tool | `tests/test_issue_89_tool_names_in_copy.py` (covers rendered output); the metadata test adds the tool-shaped check (Tests, item 3) over every description and the instructions |

### D6. The schema snapshot

`tests/fixtures/tools_list_schema_5a391ca.json` is generated once, on `main` at 5a391ca, by iterating `await mcp.list_tools()` under the test modules' standard environment (`SECRET_KEY`, `DATABASE_URL`, `VAULT_PATH` defaults, `chdir` away from `.env`, `MCP_REJECT_UNKNOWN_ARGUMENTS` at its default `true`) and writing `[{"name", "inputSchema", "outputSchema"}]` in registry order. The test loads it and asserts the live list is equal element-wise: same length, same names in the same order, `inputSchema` equal as dicts, `outputSchema` equal as dicts. (Generating the fixture corrected an assumption in the first draft: on 5a391ca **24** tools carry an `outputSchema`, not one — FastMCP 1.30 wraps every `str` return as `{"result": string}`, `read_note` publishes its model's schema, and `read_file`, which returns content blocks, is the only tool without one. All 25 `inputSchema`s carry `additionalProperties: false`.) `title`, `description` and `annotations` are deliberately not in the fixture — they are what this change changes. The generating one-liner is recorded in the test module's docstring so the fixture can be regenerated when a later change deliberately alters a schema.

### D7. What does not change

- `_tracked`'s gate order, `_require_write`, `current_permission`, the OAuth scope check, the write bucket, `TOOL_CLASSES`, every refusal code, every result string.
- Every argument name, type, default and the generated `inputSchema` / `outputSchema` (D6).
- The 25 tool names and their registration order.
- `tools.py` implementation docstrings.
- `_forbid_unknown_arguments` and its test.
- The vault guide and its primer.

**Amendment after review (Codex adversarial round 1 → round 2 PASS).** "The other 21 descriptions untouched" became "the other 19 untouched, plus a one-sentence truth fix in `create_note` and in `delete_note`": both carried the pre-existing wording "Every refusal ends with one machine-readable `MCP-REFUSAL` line", which is false for an undeclared or mistyped argument (rejected by protocol validation before `_tracked`, no such line — the same inaccuracy review found in the three restructured descriptions). Each now reads "Every refusal the server raises ends with one machine-readable `MCP-REFUSAL {"code":…}` line (an undeclared or mistyped argument is rejected by protocol validation before the tool runs and carries no such line)". Nothing else in either description changed; both remain under 4,000 and `test_the_refusal_statement_is_qualified` covers all five.

**Second post-archive amendment, same class.** The `WRITE_PRECONDITION_REQUIRED` qualification of "`expected_hash` is optional" (Codex adversarial round 1, MINOR) was applied in the review-fixes commit to `edit_note` and `set_frontmatter` only; `move_note`, `delete_note`, `write_file` and `delete_file` still said "Optional; omit it and nothing changes" (or, for `delete_file`, a half-qualified "the deployment may require a hash"). Each now carries the same one-clause form — "optional by default — omitted, the move/delete/overwrite proceeds — unless the deployment requires it, when an omitted hash is refused `precondition_required`" — and `test_expected_hash_is_described_as_optional_by_default_only` parametrises over the six guardable write tools, with a guard test asserting that set is exactly the tools whose `inputSchema` declares `expected_hash` minus `create_note` (whose hash is always refused `no_incumbent`; creation is exempt from the setting). `move_note` gave up two Args sentences that duplicated its lead to stay under the cap. No spec change: the requirement is #131's standing one, that a description says what the tool does.

## Tests (contract for the implementer)

All in `tests/test_issue_343_tool_metadata.py`, following the `test_issue_150_docstrings.py` setup convention (env defaults and `chdir` before importing `src.mcp_server.server`), reading everything from `mcp._tool_manager.list_tools()` / `await mcp.list_tools()` rather than from module functions (#89's reason).

1. **Metadata completeness.** For every listed tool: `title` is a non-empty `str`; `annotations` is a `ToolAnnotations`; each of `readOnlyHint`, `destructiveHint`, `idempotentHint`, `openWorldHint` `is True` or `is False` (never `None`); `annotations.title is None or == title`. Titles are pairwise distinct. Exactly 25 tools.
2. **The classification reconciles with the code.** With `W = tools.production_write_bucket_tools()`: `W == {t for t, c in concurrency.TOOL_CLASSES.items() if c == "write"}`; `W <= registered names`; `{destructive} == (W - {"create_note"}) | {"request_upload"}`; `{not read-only} == W | {"request_upload", "request_download"}`; `{open-world} == {"import_from_url"}`; `{idempotent} == {read-only}`; every read-only tool has `destructiveHint is False`. The fifteen read-tool names are asserted explicitly as well, so that a read tool added without annotations fails on name, not only on count. **Pollution test:** the module decorates its own `_tracked("probe_write", [], write_class=True)` and `_tracked("edit_note", [], write_class=False)` functions at module scope (the same shape as the existing probes in `test_concurrency_provenance.py` and `test_mcp_rate_limits.py`) and asserts `production_write_bucket_tools()` is still exactly the eight production names.
3. **Instructions.** `mcp.instructions is server.SERVER_INSTRUCTIONS`; `len(SERVER_INSTRUCTIONS) <= 2000`; the first 512 characters contain every token listed in D3; the round-trip conditions and the absence of the unqualified "back to `edit_note` unchanged" per D3; the whole text contains neither `"must call"` nor `"important"` (case-insensitive); every tool-shaped backticked identifier in it is a registered tool, under **the tool-shaped rule**: a backticked span is a tool reference when it is in call form (`` `name(` ``, as in `test_issue_89`'s `_CALL`); or when it is a **dotted span** `head.rest` whose `head` is tool-shaped under the bare rule below (the head is what is validated — `` `read_note.frontmatter_yaml` `` in `read_file`'s description is the live case, and `` `read_notes.frontmatter_yaml` `` must be flagged; Codex spec round 1, MINOR); or when it is a bare identifier that **contains an underscore** and whose first `_`-delimited word is the first word of some registered tool name (`keyword`, `semantic`, `read`, `list`, `get`, `create`, `edit`, `move`, `delete`, `set`, `find`, `write`, `request`, `check`, `import` — derived from the registry at test time, not listed). `test_issue_89`'s bare-identifier rule is deliberately not reused here: descriptions legitimately backtick argument and field names (`` `content` ``, `` `expected_hash` ``, `` `find` ``), which that rule would flag. The underscore condition exists for `` `find` ``: `edit_note`'s argument shares its first word with `find_orphans`, and every registered name contains an underscore, so a single-word span can never be a tool. Applied to every backticked span in all 25 descriptions on 5a391ca, the bare rule matches 18 identifiers, all registered, and skips every argument, result field, refusal code and setting name (checked with an AST walk while writing this design). The extractor itself is unit-tested on four strings: `` `read_note` `` (counted), `` `read_note(` `` (counted), `` `read_note.frontmatter_yaml` `` (head counted), `` `read_notes.frontmatter_yaml` `` (head counted and unregistered → the assertion fails), `` `find` `` / `` `content` `` (not counted).
4. **Description cap and lead.** For every listed tool, `len(tool.description) <= 4000`. For each of `edit_note`, `move_note`, `set_frontmatter`: the first 1,500 characters of `tool.description` contain every key of `tool.parameters["properties"]`, the strings `expected_hash`, `MCP-REFUSAL` and `readwrite`. For `move_note` additionally (Codex spec round 1, MAJOR): the first 1,500 characters match `pins inode identity` and `(does not|without) (re-?)?compar` — the residual race stays agent-visible beside the precondition. For `read_note`: the first 1,500 characters contain every property key and the strings `` `content` ``, `` `heading` ``, `` `content_hash` ``, `` `error` ``.
5. **Schema snapshot** (D6).
6. **Every tool-shaped backticked identifier in every description is registered** (the tool-shaped rule from item 3 — call, dotted head, bare — applied to all 25 descriptions, so a moved sentence cannot leave a dangling name behind).
7. **The existing suite is green unmodified**, in particular the modules D5 names.

## Risks / Trade-offs

- **A shorter `edit_note` is a less-defended `edit_note`.** The long docstring grew out of real incidents (#128, #140, #149, #150, #205). The mitigation is D5: every sentence a spec pins survives and the pinning tests are untouched; the deletions are of reasoning that already lives in `vault-tools.md`.
- **Annotation-aware clients will stop prompting on read tools.** That is the point, and the read tools are read-only by construction (`test_no_read_tool_confirms_anything` in `test_issue_88_…` is the structural guard that no read path publishes).
- **A client might treat `destructiveHint=false` on `create_note` as "safe to run without asking".** `create_note` can only add a file that was not there; that is additive by the spec's own definition.

## Accepted limitations

See the proposal (L1–L8). The two worth repeating here: annotations are untrusted hints and gate nothing server-side (L1); claude.ai may drop `instructions`, so nothing depends on them (L2).

## Questions for the owner

1. **`request_upload` — decided (Codex spec round 1, 2026-10-28): `destructiveHint=true`.** With `overwrite=True` it mints a capability whose redemption overwrites a file; the tool call is the authorisation. The reconciliation is `{destructive} == (W - {create_note}) | {request_upload}` (eight tools).
2. **`semantic_search`: `openWorldHint=false` (proposed, L5) or `true`?** It calls the embedding provider, which may be OpenAI. The proposal says the provider is infrastructure. Not raised by the spec review; stands as proposed unless the owner objects.
3. **`idempotentHint` on `delete_note` / `delete_file` / `set_frontmatter`**: uniformly `false` (proposed, L6) or per-tool `true`? Not raised; stands as proposed.
4. **Titles**: the wording in D1 is a proposal; any title may be changed without affecting a test except the uniqueness check.

## Codex spec review, round 1 (2026-10-28) — all five findings folded

| # | severity | finding | where it landed |
| --- | --- | --- | --- |
| 1 | BLOCKER | instructions prescribed an unqualified round trip and implied unguarded edits refuse stale overwrites | D3 text rewritten and re-measured (509 / 1,425); spec requirement 2 and two scenarios; test item 3 |
| 2 | MAJOR | `request_upload` `destructiveHint=false` contradicted the owner decision | D1 table and equalities; spec requirement 1 and its scenario; proposal L4; tasks 1.2, 4.1 |
| 3 | MAJOR | a module-global `WRITE_BUCKET_TOOLS` is polluted by test probes | D2 rewritten (wrapper attributes + `production_write_bucket_tools()`), pollution scenario and test item 2 |
| 4 | MAJOR | `move_note`'s inode-pinned residual left the agent-visible description | D4 `move_note` `expected_hash` row; spec scenario "move_note keeps the residual race agent-visible"; test item 4 |
| 5 | MINOR | extractor ignored dotted spans (`read_note.frontmatter_yaml`) | tool-shaped rule gains the dotted-head branch; extractor unit cases; spec scenarios |

## Migration plan

None. No schema, no setting, no data. Deploy is merge to `main`; the post-deploy exercise is a `tools/list` against the live server (via the claude.ai connector or `mcp_stdio`) confirming 25 tools with titles and four booleans each, `instructions` present in the `initialize` result, and `len(description) <= 4000` for every tool — plus one `read_note` and one `edit_note(dry_run=True)` to show the shortened descriptions still drive a correct call.
