from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from src.config import settings
from src.mcp_server.tools import (
    check_upload_impl,
    create_note_impl,
    delete_file_impl,
    delete_note_impl,
    edit_note_impl,
    find_orphans_impl,
    find_related_impl,
    get_backlinks_impl,
    get_links_impl,
    get_neighborhood_impl,
    get_recent_impl,
    get_tags_impl,
    get_vault_guide_impl,
    import_from_url_impl,
    list_files_impl,
    list_notes_impl,
    move_note_impl,
    read_file_impl,
    read_note_impl,
    request_download_impl,
    request_upload_impl,
    search_notes_impl,
    semantic_search_impl,
    set_frontmatter_impl,
    write_file_impl,
)

mcp = FastMCP(
    "obsidian-vault",
    stateless_http=True,
    streamable_http_path="/",
    # Derived from the write caps rather than the SDK's 4 MiB default, which
    # would reject a `write_file` far below our documented 25 MB cap. See
    # `Settings.mcp_max_request_body_bytes` for the arithmetic and the
    # (qualified) guarantee it provides.
    max_request_body_size=settings.mcp_max_request_body_bytes,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=not settings.mcp_sandbox_mode,
        allowed_hosts=settings.allowed_hosts,
    ),
)


@mcp.tool()
async def keyword_search(
    query: str,
    folder: str | None = None,
    limit: int = 20,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Full-text keyword search via PostgreSQL tsvector. Use this for exact identifiers,
    code symbols, proper nouns, or known phrases — anywhere semantic noise hurts.

    For conceptual or paraphrased queries, use semantic_search instead.

    Args:
        query: Keywords or phrase to match (websearch tsquery syntax: "foo bar", "foo OR bar", "-bar").
        folder: Optional folder prefix (e.g. "Cards/", "Projects/").
        limit: Maximum number of results (default 20).
        tags: Optional list of tag names; only notes carrying ALL listed tags match
            (e.g. ["project", "active"]).
        frontmatter: Optional dict of frontmatter key/value pairs; only notes whose JSONB
            frontmatter contains every pair match. Strict type matching — string "0" does
            not match integer 0 (e.g. {"status": "draft"}).
    """
    return await search_notes_impl(
        query, folder=folder, limit=limit, tags=tags, frontmatter=frontmatter
    )


@mcp.tool()
async def read_note(
    path: str,
    section: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> str:
    """Read a note from the Obsidian vault by its relative path.

    Responses are capped to a context-safe size. If the note is larger than the
    cap, you get the first window plus an outline of the note's headings — read
    the one section you need with `section=` rather than paging through the
    whole note.

    Args:
        path: Vault-relative path to the note (e.g. "Cards/My Note.md")
        section: Optional ATX heading to read instead of the whole note. Plain
            text ("Balance Sheet"), a path-style chain ("Parent/Child") when the
            heading appears under different parents, or a "#N" ordinal ("#7",
            1-based document order) — the ordinal is the only form that can
            address duplicate headings sharing the same parent. The outline
            printed with a truncated note lists the ordinal for every section.
            A bare "#N" always selects by position and is never shadowed by a
            heading whose text happens to be "#N"; use "Parent/#N" to reach
            such a heading by title.
        offset: Character offset to start reading from (default 0). Use the
            value the truncation notice reports to continue.
        limit: Maximum characters to return. Only lowers the server cap; it
            cannot raise it.
    """
    return await read_note_impl(path, section=section, offset=offset, limit=limit)


@mcp.tool()
async def list_notes(
    folder: str = "",
    limit: int = 50,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """List notes in a vault folder, sorted by most recently modified.

    Results come from the index, so a note that exists on disk but has not yet been
    picked up by the indexer will not appear (lag is bounded by the index interval,
    typically up to 5 minutes).

    Args:
        folder: Vault-relative folder path (e.g. "Cards/", "Projects/"). Empty for vault root.
        limit: Maximum number of results (default 50).
        tags: Optional list of tag names; only notes carrying ALL listed tags match
            (e.g. ["idea"]).
        frontmatter: Optional dict of frontmatter key/value pairs; strict type match
            (e.g. {"status": "active"}).
    """
    return await list_notes_impl(folder, limit=limit, tags=tags, frontmatter=frontmatter)


@mcp.tool()
async def get_tags(limit: int = 50) -> str:
    """List all tags used across the vault with note counts.

    Args:
        limit: Maximum number of tags to return (default 50)
    """
    return await get_tags_impl(limit=limit)


@mcp.tool()
async def get_recent(
    limit: int = 20,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Get recently modified notes.

    Args:
        limit: Number of recent notes to return (default 20).
        folder: Optional folder prefix to filter (e.g. "Projects/").
        tags: Optional list of tag names; only notes carrying ALL listed tags match
            (e.g. ["meeting"]).
        frontmatter: Optional dict of frontmatter key/value pairs; strict type match
            (e.g. {"status": "active"}).
    """
    return await get_recent_impl(
        limit=limit, folder=folder, tags=tags, frontmatter=frontmatter
    )


@mcp.tool()
async def semantic_search(
    query: str,
    limit: int = 15,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Vector similarity search using bge-m3 embeddings. Use this for conceptual or paraphrased
    queries — anywhere exact word matching would miss the point.

    For exact identifiers, code symbols, proper nouns, or known phrases, use keyword_search instead.

    Each result is one note (deduped) with its best-matching chunk as a ~500-char preview.
    Call `read_note` on a result's path to get the full note content.

    Args:
        query: Natural language description of what you're looking for.
        limit: Maximum number of distinct notes to return (default 15).
        folder: Optional folder prefix (e.g. "Projects/").
        tags: Optional list of tag names; only notes carrying ALL listed tags match
            (e.g. ["product"]).
        frontmatter: Optional dict of frontmatter key/value pairs; strict type matching —
            string "0" does not match integer 0 (e.g. {"status": "active"}).
    """
    return await semantic_search_impl(
        query, limit=limit, folder=folder, tags=tags, frontmatter=frontmatter
    )


@mcp.tool()
async def create_note(path: str, content: str) -> str:
    """Create a new markdown note in the Obsidian vault. Requires a readwrite API key.

    See `get_vault_guide` for Obsidian syntax and any vault-specific conventions
    (naming, folder placement, frontmatter, tags).

    Refuses a path whose final component is a symlink, naming its target, so a
    write never lands on a note other than the one named; symlinked folders
    inside the vault work normally.

    Args:
        path: Vault-relative path for the new note (e.g. "Cards/New Topic.md"). The .md extension is added if missing.
        content: Full markdown content for the note, including any frontmatter.
    """
    return await create_note_impl(path, content)


@mcp.tool()
async def edit_note(
    path: str,
    content: str,
    append: bool = False,
    operation: str | None = None,
    find: str | None = None,
    section: str | None = None,
    replace_all: bool = False,
    dry_run: bool = False,
) -> str:
    """Edit an existing note in the Obsidian vault. Requires a readwrite API key.

    See `get_vault_guide` for Obsidian syntax and any vault-specific conventions
    (naming, folder placement, frontmatter, tags).

    Four mutually exclusive modes (set at most one of append/find/section):
    1. **Full replace** (default): provide only `content`; the entire file is overwritten.
    2. **Append**: `append=True`; `content` is added at the end (preceded by a single newline).
    3. **Find & replace**: `find=<exact text>`; replaced with `content`. Must match
       exactly once unless `replace_all=True`.
    4. **Section**: `section=<heading>`; replaces the body under the named ATX heading.
       Use the path-style form `Parent/Child` to disambiguate when the same heading
       appears more than once. Setext (`====`/`----`) headings are not matched.

    Flags:
    - `operation="append"`: legacy alias for `append=True`. This is accepted to
      prevent older clients from silently falling through to full replacement.
      `operation="replace"` explicitly selects full replacement.
    - `replace_all=True`: with `find`, replace every occurrence rather than failing on
      multiple matches. Ignored when `find` is unset.
    - `dry_run=True`: compute the would-be result and return a unified diff without
      writing. Works for all four modes.

    Writes are atomic (tmp file + os.replace) so a crash mid-write cannot truncate
    the destination. Frontmatter mutation is better done via `set_frontmatter` —
    PyYAML serialization there discards YAML comments. A path whose final
    component is a symlink is refused in every mode (`dry_run` included), naming
    the link's target; symlinked folders inside the vault work normally.

    Args:
        path: Vault-relative path to the note.
        content: New full content, replacement text, text to append, or section body.
        append: If True, append content to the end of the note.
        operation: Legacy mode selector; accepts "append" or "replace".
        find: Exact text to find and replace.
        section: ATX heading text identifying the section whose body to replace.
            Use `Parent/Child` to disambiguate repeated headings.
        replace_all: With `find`, replace every match instead of requiring uniqueness.
        dry_run: Return a unified diff and do not write.
    """
    return await edit_note_impl(
        path,
        content,
        append=append,
        operation=operation,
        find=find,
        section=section,
        replace_all=replace_all,
        dry_run=dry_run,
    )


@mcp.tool()
async def get_vault_guide() -> str:
    """Returns a two-part guide for working with this Obsidian vault:

    1. **Obsidian primer** — generic syntax (wikilinks, embeds, block refs,
       heading refs, tags, frontmatter, callouts, comments, highlights,
       math, mermaid, footnotes, tasks, plugin literals).
    2. **Vault-specific conventions** — folder structure, naming rules,
       frontmatter requirements, and tag taxonomy as configured by the
       vault owner in `CLAUDE.md`. If `CLAUDE.md` is absent, the response
       includes instructions for creating one.
    """
    return await get_vault_guide_impl()


@mcp.tool()
async def get_backlinks(path: str, limit: int = 50) -> str:
    """Notes that link TO `path`. Use this to discover what references a given
    note — projects citing a card, daily notes mentioning a person, etc.

    Resolved links only (dangling references are not counted as backlinks).

    Args:
        path: Vault-relative path to the target note (e.g. "Cards/Foo.md").
        limit: Maximum results (default 50, hard cap 500).
    """
    return await get_backlinks_impl(path, limit=limit)


@mcp.tool()
async def get_links(path: str) -> str:
    """Outgoing links from `path` — both resolved and dangling.

    Useful for "what does this note depend on?" or finding broken references
    that need follow-up notes.

    Args:
        path: Vault-relative path to the source note.
    """
    return await get_links_impl(path)


@mcp.tool()
async def get_neighborhood(path: str, depth: int = 1, limit: int = 50) -> str:
    """The connected subgraph reachable from `path` via links or backlinks,
    up to `depth` hops (treated as undirected).

    Use this when an agent needs the local cluster around a topic — e.g.
    "summarize everything connected to this project". Prefer this over
    `find_related` when explicit links are the signal you want; prefer
    `find_related` when the connection is conceptual rather than linked.

    Args:
        path: Vault-relative path to the seed note.
        depth: Maximum BFS depth (default 1, capped at 5).
        limit: Maximum distinct neighbor notes (default 50, hard cap 200).
    """
    return await get_neighborhood_impl(path, depth=depth, limit=limit)


@mcp.tool()
async def find_related(path: str, limit: int = 10) -> str:
    """Semantically similar notes based on the source note's chunk embeddings,
    averaged then queried via pgvector.

    Independent of the link graph — useful when the source is sparsely linked
    or when looking for thematic neighbors. For link-based exploration use
    `get_neighborhood`. For arbitrary topic queries use `semantic_search`.

    Args:
        path: Vault-relative path to the source note.
        limit: Maximum results (default 10, hard cap 50).
    """
    return await find_related_impl(path, limit=limit)


@mcp.tool()
async def find_orphans(folder: str | None = None, limit: int = 50) -> str:
    """Notes with zero incoming AND zero outgoing resolved links — useful for
    vault hygiene ("what's disconnected?") and cleanup decisions.

    Args:
        folder: Optional vault-relative folder prefix to scope the search
            (e.g. "Cards/").
        limit: Maximum results (default 50, hard cap 500).
    """
    return await find_orphans_impl(folder=folder, limit=limit)


@mcp.tool()
async def move_note(
    from_path: str, to_path: str, rewrite_links: bool = False
) -> str:
    """Move or rename a note inside the vault. Requires a readwrite API key.

    Updates `notes_metadata.file_path` for the moved note and `note_links.target_path`
    rows whose stored target matched the old path. Backlinks via `target_note_id`
    keep working without rewriting source notes (the moved note's id is unchanged).

    With `rewrite_links=True`, also opens every source note that linked to this
    note and rewrites the link title in-place: `[[Old]]` → `[[New]]`,
    `[[Old|alias]]` → `[[New|alias]]`, `[[Old#anchor]]` → `[[New#anchor]]`,
    `![[Old]]` → `![[New]]`, and path-style `[[folder/Old]]` → `[[new/folder/New]]`.
    Aliases and anchors are preserved; only the title portion is rewritten.
    All rewrites are computed before anything changes: if one would push a
    source note past the 10 MiB note limit the whole move is refused, naming
    that source, so the link graph never disagrees with the vault bytes.
    That preflight is also bounded in aggregate: if the originals plus rewrites
    for all backlink sources would exceed 256 MiB in memory the move is refused
    before anything changes, naming the note count and the limit.

    Writes are atomic. Either path is refused, naming the link's target, when
    its final component is a symlink; symlinked folders inside the vault work
    normally and the recorded paths are the real ones behind them. See
    `get_vault_guide` for vault folder conventions.

    Args:
        from_path: Vault-relative path of the existing note.
        to_path: Vault-relative path of the destination. Must not exist. Parent
            directories are created automatically.
        rewrite_links: If True, also rewrite incoming wikilinks and embeds in
            source notes. Off by default — opting in is destructive (it modifies
            other notes' bodies).
    """
    return await move_note_impl(
        from_path, to_path, rewrite_links=rewrite_links
    )


@mcp.tool()
async def delete_note(path: str, permanent: bool = False) -> str:
    """Delete a note from the vault. Requires a readwrite API key.

    By default this is a soft-delete: the file is moved to
    `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` inside the vault root, by a
    single non-replacing rename, so an existing trash entry is never
    overwritten and two deletes in the same second land on distinct names. The
    indexer skips dot-prefixed directories, so search and embeddings drop the
    note automatically on the next reindex pass (≤ 5 minutes). Soft-deleted
    files accumulate in `.trash/` — emptying that directory is the user's
    responsibility.

    A vault filesystem that cannot perform that non-replacing rename into
    `.trash/` makes the soft delete refuse with an error naming the limitation
    rather than fall back to a rename that could overwrite; pass
    `permanent=True` to unlink instead.

    With `permanent=True`, the file is unlinked directly with no recovery path
    inside this server. Existing backups are the rollback story.

    A path whose final component is a symlink is refused, naming its target, so
    a delete never removes a note other than the one named; symlinked folders
    inside the vault work normally.

    Dangling backlinks left behind by a delete are surfaced via
    `get_backlinks` and `find_orphans`. See `get_vault_guide` for context.

    Args:
        path: Vault-relative path to the note.
        permanent: If True, unlink instead of soft-deleting.
    """
    return await delete_note_impl(path, permanent=permanent)


@mcp.tool()
async def set_frontmatter(
    path: str,
    updates: dict | None = None,
    remove: list[str] | None = None,
) -> str:
    """Mutate a note's YAML frontmatter without touching its body. Requires a
    readwrite API key.

    Parses the existing frontmatter, merges in `updates` (overwriting matching
    keys, adding any new ones), then drops keys listed in `remove`. The note
    body is preserved byte-for-byte. If the note has no frontmatter (no `---`
    fence on line 1), a fresh block is prepended ahead of the unchanged body.

    Re-serialization uses `yaml.safe_dump(default_flow_style=False,
    sort_keys=False, allow_unicode=True)`. **Caveat:** PyYAML does NOT preserve
    YAML comments — any `# comment` in the original frontmatter will be lost on
    the first `set_frontmatter` call.

    A path whose final component is a symlink is refused, naming its target, so
    the frontmatter of an unnamed note is never rewritten; symlinked folders
    inside the vault work normally.

    See `get_vault_guide` for vault frontmatter conventions.

    Args:
        path: Vault-relative path to the note.
        updates: Mapping of keys to set. Use the empty dict (or omit) to skip.
        remove: List of keys to delete from the frontmatter. Missing keys are
            silently ignored.
    """
    return await set_frontmatter_impl(path, updates=updates, remove=remove)


@mcp.tool()
async def read_file(
    path: str,
    encoding: str = "auto",
    offset: int = 0,
    limit: int | None = None,
):
    """Read any file in the vault — including non-markdown (PDFs, images,
    skill HTML/JS, data files). Peer to `read_note`, which stays markdown-only.

    This is pure byte transport: the server does NOT extract or parse PDFs and
    cannot interpret binary bytes. Non-text/non-image files come back as an
    opaque base64 string intended for a client-side skill to decode — not as
    something the model can read directly.

    Encoding:
    - `"auto"` (default): text-like files (HTML, JSON, CSV, source, …) return
      as readable text; images (PNG/JPEG/GIF/WebP) return as an inline image
      block that renders in-client; everything else returns as a labeled
      base64 string.
    - `"text"`: force a UTF-8 text decode; errors if the file is not valid UTF-8.
    - `"base64"`: force a raw-bytes base64 string regardless of type.

    Files larger than `MAX_FILE_READ_BYTES` (default 10 MB) are refused with a
    size report. Base64 reads pass through the model context and inflate ~33%,
    so they are token-heavy — check a file's size with `list_files` before
    reading large binaries. Dot-directories (`.obsidian`, `.git`, `.trash`, …)
    and path traversal are rejected.

    Text results are additionally capped to a context-safe size and continue
    via `offset`; base64 and image results are not windowed.

    Args:
        path: Vault-relative path to the file (e.g. "Reference Docs/spec.pdf").
        encoding: One of "auto" (default), "text", or "base64".
        offset: Character offset to start a text read from (default 0). Use the
            value the truncation notice reports to continue.
        limit: Maximum characters to return for a text read. Only lowers the
            server cap; it cannot raise it.
    """
    return await read_file_impl(path, encoding=encoding, offset=offset, limit=limit)


@mcp.tool()
async def write_file(
    path: str,
    content: str,
    encoding: str = "base64",
    overwrite: bool = False,
) -> str:
    """Write a file into the vault — including non-markdown (e.g. save a
    generated PDF or image). Requires a readwrite API key. Peer to
    `create_note`/`edit_note`, which stay markdown-only.

    `content` carries the bytes: with `encoding="base64"` (default) it is
    base64-decoded to raw bytes; with `encoding="text"` it is written verbatim
    as UTF-8. The write is atomic (tmp file + `os.replace`), missing parent
    folders are created, and content over `MAX_FILE_WRITE_BYTES` (default
    25 MB, decoded length) is refused.

    No-clobber by default: writing over an existing file requires
    `overwrite=True`. Dot-directories and path traversal are rejected; invalid
    base64 errors without writing anything. A path whose final component is a
    symlink is refused, naming its target, so `overwrite=True` cannot clobber a
    file through an alias; symlinked folders inside the vault work normally.

    The MCP transport also bounds the whole request body (sized so a base64
    write at the cap always gets through). Base64 is therefore the always-safe
    encoding: `encoding="text"` content whose JSON escaping inflates past that
    bound is rejected by the transport with a bare HTTP 413 before this tool
    runs — send such content as base64 instead.

    Args:
        path: Vault-relative destination path (e.g. "Outputs/report.pdf").
        content: File contents — base64 string (default) or UTF-8 text.
        encoding: "base64" (default) or "text".
        overwrite: If True, replace an existing file. Off by default.
    """
    return await write_file_impl(
        path, content, encoding=encoding, overwrite=overwrite
    )


@mcp.tool()
async def list_files(
    folder: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    limit: int = 200,
) -> str:
    """Browse the vault filesystem (`ls`-style), including non-markdown files.
    Peer to `list_notes`, which lists indexed markdown only; `list_files` reads
    the filesystem directly and reports sizes so you can gauge a binary before
    `read_file`.

    By default lists the immediate children of `folder` — subdirectories and
    files, each file with size and modification time. `pattern` is a glob that
    filters file entries (e.g. "*.pdf"); `recursive=True` descends into
    subfolders and returns matching files. Dot-directories (`.obsidian`,
    `.git`, `.trash`, …) are hidden, and a dot-directory `folder` is rejected.

    At most `limit` entries are returned (default 200, hard cap 1000); the
    response indicates when the listing was truncated.

    Args:
        folder: Vault-relative folder (default "." = vault root).
        pattern: Glob applied to file names (default "*").
        recursive: If True, descend into subfolders. Off by default.
        limit: Maximum entries to return (default 200, hard cap 1000).
    """
    return await list_files_impl(
        folder, pattern=pattern, recursive=recursive, limit=limit
    )


@mcp.tool()
async def request_upload(
    path: str,
    overwrite: bool = False,
    expires_in: int | None = None,
) -> str:
    """Get a short-lived link a person can use to put a file into the vault.
    Requires a readwrite API key. Peer to `write_file`, which takes the bytes
    directly — use this one when you do not have them.

    No MCP client can hand a tool the bytes of a file the user is looking at,
    and your shell cannot reach their machine. This mints a link bound to
    exactly one destination path: hand it to the person you are helping, they
    open it and pick a file, and it lands at `path`. Nothing else can be
    written with it.

    The token lives in the URL's `#` fragment, which browsers never send to a
    server, so it stays out of access logs. **Treat the whole URL as a secret**
    — whoever holds it can write that one path, once, until it expires. Never
    put it in a query string: that *would* log it.

    Single use, and no-clobber unless you ask otherwise. With
    `overwrite=True` the link also remembers what the file looked like now and
    refuses to publish if it changed in the meantime, so a stale link cannot
    silently undo an edit someone made while it was waiting.

    From a shell you can upload without the page:
    `curl -H "Authorization: Bearer <token>" -T <file> <base>/transfer/upload`.

    Then call `check_upload(upload_id)` to confirm the bytes landed and get
    their sha256. See `get_vault_guide` for how files fit into the vault.

    Args:
        path: Vault-relative destination (e.g. "Attachments/photo.png").
        overwrite: If True, allow replacing an existing file at `path`.
        expires_in: Seconds until the link dies. Clamped to 60–3600; defaults
            to `TRANSFER_TOKEN_TTL_SECONDS` (600). A link can never outlive the
            credential you are calling with, so the deadline in the result may
            be earlier than you asked for — it says so when that happens.
    """
    return await request_upload_impl(path, overwrite=overwrite, expires_in=expires_in)


@mcp.tool()
async def check_upload(upload_id: str) -> str:
    """Ask what happened to an upload link you minted with `request_upload`.

    Returns one of `pending` (nothing sent yet), `uploading` (bytes are in
    flight), `completed` (with the path, size, sha256 and MIME type of what
    landed), `unknown` (a stream started and the server never recorded how it
    ended), `revoked` (the link is dead because the credential or vault root
    changed under it), or `expired`. Use it to confirm a transfer really
    finished before you tell the user it did, and to get the sha256 if they
    want to verify it.

    Only links minted by this same API key or OAuth token are visible; anyone
    else's `upload_id` reads as `not found`.

    `uploading` names the deadline the stream has. Check again after it: past
    that point the answer becomes either `completed` or `unknown`. **`unknown`
    does not mean nothing arrived** — a publish can succeed and still fail to
    record its completion — so read or list the path before minting another
    link or telling anyone the file did not arrive.

    Pass the `upload_id` itself — the short handle from `request_upload`, not
    the upload URL and not the token after the `#`. Anything else is refused
    without a lookup.

    Args:
        upload_id: The `upload_id` that `request_upload` returned.
    """
    return await check_upload_impl(upload_id)


@mcp.tool()
async def request_download(path: str, expires_in: int | None = None) -> str:
    """Get a short-lived link a person can use to save a vault file. Peer to
    `read_file`, which returns the bytes to you — use this one when the file is
    for the human, not for you.

    Handy for anything `read_file` would waste context on or cannot render: a
    PDF, a large image, an archive. Reading works with a read-only key.

    The token lives in the URL's `#` fragment, so it never reaches an access
    log. **Treat the whole URL as a secret** — whoever holds it can read that
    one file until it expires. Never put it in a query string.

    The link is bound to the file *as it is now*: if it is edited or replaced,
    the link stops working rather than serving different content than you
    described. Unlike an upload link it can be used more than once, so the
    person can preview and then save.

    From a shell: `curl -H "Authorization: Bearer <token>" -o <file>
    <base>/transfer/download/file`.

    Args:
        path: Vault-relative path of the file to share.
        expires_in: Seconds until the link dies. Clamped to 60–3600; defaults
            to `TRANSFER_TOKEN_TTL_SECONDS` (600). A link can never outlive the
            credential you are calling with, so the deadline in the result may
            be earlier than you asked for — it says so when that happens.
    """
    return await request_download_impl(path, expires_in=expires_in)


@mcp.tool()
async def import_from_url(url: str, path: str, overwrite: bool = False) -> str:
    """Fetch a file from a public https URL straight into the vault. Requires a
    readwrite API key. Peer to `write_file` and `request_upload` — use this one
    when the bytes are already somewhere public.

    The server does the fetching, so nothing passes through your context: a
    20 MB PDF costs one tool call. Returns the path, size, sha256, MIME type
    and the final URL after any redirects.

    **Only genuinely public addresses.** This server sits on a private network
    next to a database and other services, so the fetch is restricted: https
    only, no credentials in the URL, no private/loopback/link-local/metadata
    addresses in any spelling, and the same rules re-checked at every redirect.
    A refusal names the rule that was violated — that is information about the
    URL, not a hint to work around it. Rewriting the URL to evade the check is
    never the right next step; ask the user for a public link instead.

    Size-capped at `MAX_FILE_WRITE_BYTES`, with one 30-second deadline for the
    whole fetch. No-clobber unless `overwrite=True`. Nothing is written unless
    the whole body arrives intact.

    Args:
        url: Public https URL of the file.
        path: Vault-relative destination (e.g. "Attachments/paper.pdf").
        overwrite: If True, allow replacing an existing file at `path`.
    """
    return await import_from_url_impl(url, path, overwrite=overwrite)


@mcp.tool()
async def delete_file(path: str, permanent: bool = False) -> str:
    """Delete a non-markdown file from the vault. Requires a readwrite API key.
    Peer to `delete_note`, which stays markdown-only.

    By default this is a soft delete: the file moves to
    `.trash/<YYYYMMDD-HHMMSS>-<basename>-<8 hex>` inside the vault, keeping a
    copy the user can recover. Two files with the same name deleted in the same
    second both survive — the trash never clobbers.

    With `permanent=True` the file is unlinked outright and this server has no
    recovery path; the user's backups are the only rollback.

    Refuses markdown files (use `delete_note`, which understands the index and
    backlinks), directories, and symlinks. Non-markdown files are not indexed,
    so search and embeddings are unaffected either way.

    Args:
        path: Vault-relative path to the file.
        permanent: If True, unlink instead of moving to `.trash/`.
    """
    return await delete_file_impl(path, permanent=permanent)
