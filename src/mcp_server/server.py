from urllib.parse import urlparse

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import ConfigDict

from src.config import settings
from src.mcp_server.read_result import ReadNoteResult
from src.mcp_server.tools import (
    check_upload_impl,
    create_note_impl,
    delete_file_impl,
    delete_note_impl,
    edit_note_impl,
    find_orphans_impl,
    find_related_impl,
    get_backlinks_impl,
    get_changes_impl,
    get_links_impl,
    get_neighborhood_impl,
    get_recent_impl,
    get_tags_impl,
    get_vault_guide_impl,
    hybrid_search_impl,
    import_from_url_impl,
    list_files_impl,
    list_notes_impl,
    list_tasks_impl,
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


def _host_patterns(hosts) -> list[str]:
    """Expand configured hosts into the forms the MCP SDK actually matches.

    `TransportSecurityMiddleware._validate_host` compares the Host header
    exactly and additionally understands one wildcard form, a trailing `:*`
    meaning "this host on any port". It does **not** strip the port the way
    Starlette's `TrustedHostMiddleware` does, so `ALLOWED_HOSTS=["localhost"]`
    accepted `Host: localhost:8000` at the app boundary and then answered 421
    from the `/mcp` mount — the health check passes, the tool call does not.

    So every port-less entry is emitted twice, bare and with `:*`. An entry
    that already carries a colon (an explicit `host:port`, or an IPv6 literal)
    is passed through untouched: appending `:*` to it would produce a pattern
    that matches nothing.
    """
    out: list[str] = []
    for raw in hosts or ():
        host = str(raw).strip()
        if not host:
            continue
        candidates = [host] if ":" in host else [host, f"{host}:*"]
        for candidate in candidates:
            if candidate not in out:
                out.append(candidate)
    return out


def _origin_patterns(origins) -> list[str]:
    """Same expansion for Origin values, which carry a scheme.

    `urlparse` is what decides whether an origin already names a port —
    `"https://host"` contains a colon but has none, so the textual test used
    for hosts would get this wrong.
    """
    out: list[str] = []
    for raw in origins or ():
        origin = str(raw).strip().rstrip("/")
        if not origin:
            continue
        candidates = [origin]
        try:
            has_port = urlparse(origin).port is not None
        except ValueError:
            # A malformed port ("https://host:notaport"). Treat it as literal
            # rather than guessing; config validation owns the complaint.
            has_port = True
        if not has_port:
            candidates.append(f"{origin}:*")
        for candidate in candidates:
            if candidate not in out:
                out.append(candidate)
    return out


# ── Tool metadata (#343) ────────────────────────────────────────────────────
#
# Every registration below carries a `title` and one of these four
# `ToolAnnotations` shapes, with all four hints set explicitly — an absent hint
# is read by clients as the protocol's worst case (not read-only, destructive,
# open-world). The hints are a *client-side* signal (auto-approve read-only
# calls, prompt on destructive ones, run read-only tools in parallel); nothing
# server-side consults them. Enforcement is unchanged: `_tracked` resolves the
# vault root and runs the admission gates before any body, and `_require_write`
# refuses a read-only credential inside every write body.
#
# The classification, pinned by `tests/test_issue_343_tool_metadata.py`
# against the code rather than a list:
#   read-only   = the eighteen tools that neither change vault bytes nor mint a
#                 capability (idempotent, closed-world)
#   destructive = the write-bucket set (`_tracked(write_class=True)`) minus
#                 `create_note` (no-clobber, additive) plus `request_upload`
#                 (its `overwrite=True` capability overwrites on redemption)
#   open-world  = `import_from_url` only
# A `.trash/` undo path does not make a delete non-destructive, and the hints
# are per tool, not per call: `edit_note(dry_run=True)` is still "destructive".
# See docs/architecture/vault-tools.md, "Tool metadata".
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
_ADDITIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=False
)
_DESTRUCTIVE = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=False
)
_DESTRUCTIVE_OPEN_WORLD = ToolAnnotations(
    readOnlyHint=False, destructiveHint=True, idempotentHint=False, openWorldHint=True
)

# Published in the `initialize` result. Some clients load only tool names and
# this text at session start, so the first paragraph (≤ 512 characters) is the
# whole contract; the total stays ≤ 2,000. Every sentence must be true of the
# server (#131) — the round trip is stated with its conditions and
# `expected_hash` as optional, because an unqualified "pass `content` back"
# after a truncated read replaces the note with the fragment. Nothing may
# depend on a client having read this (claude.ai is reported to drop it).
SERVER_INSTRUCTIONS = (
    "Agent memory in a markdown Obsidian vault. Search with `keyword_search` "
    "(identifiers), `semantic_search` (paraphrase) or `hybrid_search` "
    "(unsure); read with `read_note`; write with `create_note` or "
    "`edit_note` (`dry_run=true` first). `get_vault_guide` describes this "
    "vault's conventions (one call per session). `create_note` never "
    "overwrites. Edits take `expected_hash` and refuse, writing nothing, if "
    "it changed; optional by default (an unguarded stale edit overwrites) "
    "unless the deployment requires it."
    "\n\n"
    "`expected_hash` is a read's `content_hash`; a deployment that requires "
    "it refuses an unguarded edit (`precondition_required`). Round trips: only `content` from a complete whole-note `read_note` "
    "(`offset=0`, `truncated=false`) may go back as a full replacement; a "
    "section's `content` goes back only with the same `section=` selector; "
    "finish a truncated read before writing, or the rest of the note is lost. "
    "Server refusals end with one machine-readable line, "
    '`MCP-REFUSAL {"code":…}`; act on the code, not the prose '
    "(protocol-layer argument errors do not carry it). Non-markdown files use "
    "`read_file`, `write_file`, `list_files` and `delete_file`. "
    "`request_upload` and `request_download` mint short-lived links for a "
    "person; `import_from_url` fetches a public https URL into the vault. The "
    "link graph is `get_backlinks`, `get_links`, `get_neighborhood`, "
    "`find_related` and `find_orphans`. Deletes are soft (`.trash/`) unless "
    "`permanent=true`. Calls are rate-limited per credential; a rate refusal "
    "says when to retry and spends no daily quota. `list_tasks` lists checkbox "
    "tasks across the vault from the index; Tasks-plugin query blocks are text "
    "it does not evaluate."
)


mcp = FastMCP(
    "obsidian-vault",
    instructions=SERVER_INSTRUCTIONS,
    stateless_http=True,
    streamable_http_path="/",
    # Derived from the write caps rather than the SDK's 4 MiB default, which
    # would reject a `write_file` far below our documented 25 MB cap. See
    # `Settings.mcp_max_request_body_bytes` for the arithmetic and the
    # (qualified) guarantee it provides.
    max_request_body_size=settings.mcp_max_request_body_bytes,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=not settings.mcp_sandbox_mode,
        allowed_hosts=_host_patterns(settings.allowed_hosts),
        # Not passing this leaves the SDK default `[]`, and an empty list is
        # not "no opinion" there: `_validate_origin` accepts an absent Origin
        # and rejects every present one, so any browser-based MCP client got a
        # blanket 403 while the same request from a CLI client succeeded.
        allowed_origins=_origin_patterns(settings.allowed_origins),
    ),
)


@mcp.tool(title="Keyword search", annotations=_READ_ONLY)
async def keyword_search(
    query: str,
    folder: str | None = None,
    limit: int = 20,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Full-text keyword search via PostgreSQL tsvector. Use this for exact identifiers,
    code symbols, proper nouns, titles, or known phrases — anywhere semantic noise hurts.

    Exact identifiers, code symbols, titles or known phrases: `keyword_search`.
    Conceptual or paraphrased questions: `semantic_search`. Unsure which:
    `hybrid_search`.

    **Bare terms are ANDed**: `budget forecast` matches only notes containing
    both stems. Notes matching every term come first, tagged `match: all`;
    when they do not fill the page, notes matching some of the terms follow,
    tagged `match: any`, in their own rank order. The operators:
    `"exact phrase"` (adjacent words), `budget OR forecast` (either), `-draft`
    (exclude; honoured in the `any` block too). A note's title and frontmatter
    `aliases` are indexed above its body, so a query that is a note's title
    ranks that note first.

    Each result carries `rank` (a relative score: a body-only match scores in
    the thousandths, a title match near 1, and an `any` row is scored against
    the fallback query, so ranks compare only within one block of one page),
    `match`, `matched_in` (`title` — title or aliases — and/or
    `body`, or `title+body` when the terms are split between them), `terms`
    (`k/n`: how many of the query's terms the note contains), and a second
    line: `snippet:` — the matching body text with the matches in `«»` — or
    `head:` — the note's opening words when the body alone does not match
    or the match is beyond the stored excerpt. Snippets are computed for the
    first rows of the page (the header says `snippets: first N` when the page
    is longer). The header always carries the `all`/`any` counts and how many
    rows have no stored excerpt yet (notes indexed before this version; the
    footer names the one-time rebuild). Call `read_note` on a result's path
    for the full note.

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


@mcp.tool(title="Read note", annotations=_READ_ONLY)
async def read_note(
    path: str,
    section: str | None = None,
    offset: int = 0,
    limit: int | None = None,
) -> ReadNoteResult:
    """Read a note from the Obsidian vault by its relative path.

    Arguments: `path`, an optional `section` (an ATX heading), and `offset` / `limit` for paging. Returns a
    **structured result**, not a rendered document — read the fields:

    - `content` — the selected note text. Whole-note reads: the body with a valid YAML frontmatter block
      stripped, which is byte-exact input for `edit_note(path, content)` full replacement only when the read is
      complete and unwindowed (`offset=0` and `truncated` false). Section reads: the section's **body only**,
      byte-exact input for `edit_note(path, content, section=...)` under the same condition. Pass it straight
      back; do not add, strip or split anything. Page a truncated read to the end before writing it back, or
      full replacement replaces the whole body with the fragment.
    - `heading` — section reads only: the matched heading line, with no line terminator. It is *not* part of
      `content`; a section write never rewrites the heading line.
    - `error` — set when the read failed (missing note, bad `offset`/`limit`, unknown section). A normal
      result, not a transport error; the content-bearing fields are absent beside it.
    - `content_hash` — the **whole file's** digest, `sha256:<64 lowercase hex>`, in every mode (a section or
      truncated read returns the value a whole-note read would), and the token the write tools take as
      `expected_hash`. It is not a hash of `content`: never compute it yourself, hand it back verbatim.
    - `path`, `title`, `tags` — metadata as data.
    - `frontmatter_yaml` — the frontmatter block's YAML source, fence lines excluded, LF-normalized (a CRLF
      block comes back with LF terminators — the same declared residual `content` carries, because this tool
      normalises and the write tools work on raw bytes; `edit_note` still reattaches the original block
      byte-identically). This is the authoritative copy. `frontmatter` is a best-effort JSON view of the same
      block and may be absent; `metadata_omissions` then says which fields were dropped and why. To change
      frontmatter use `set_frontmatter`, or edit the raw block with `edit_note(find=...)`; never write back a
      round trip of the JSON view.
    - `truncated`, `offset`, `next_offset`, `total_chars` — truncation as data. `outline` (truncated whole-note
      reads) lists every section with its `#N` ordinal so you can fetch the one you want directly, and `notice`
      carries the guidance in prose. Read the one section you need with `section=` rather than paging a large
      note; `content`, the outline and the metadata fields each have their own budget.
    - `metadata_omissions` — any metadata field this response had to drop, and why. Nothing is ever signalled
      by a marker inside a field.

    Byte-identity holds for notes whose body newlines are LF: terminators inside the selected content come back
    as LF. `read_file` on the same path is the byte-exact route to the file itself.

    Args:
        path: Vault-relative path to the note (e.g. "Cards/My Note.md")
        section: Optional ATX heading to read instead of the whole note. Plain text ("Balance Sheet"), a
            path-style chain ("Parent/Child") when the heading appears under different parents, or a "#N"
            ordinal ("#7", 1-based document order) — the only form that can address duplicate headings
            sharing the same parent; the outline of a truncated note carries the ordinal for every section.
            A bare "#N" always selects by position; use "Parent/#N" to reach a heading titled "#N".
        offset: Character offset to start reading from (default 0); continue from the reported `next_offset`.
        limit: Maximum characters of `content` to return. Only lowers the server cap; it cannot raise it.
    """
    return await read_note_impl(path, section=section, offset=offset, limit=limit)


@mcp.tool(title="List notes", annotations=_READ_ONLY)
async def list_notes(
    folder: str = "",
    limit: int = 50,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    since: str | None = None,
    until: str | None = None,
    order_by: str | None = None,
    cursor: str | None = None,
) -> str:
    """List notes in a vault folder, sorted by most recently modified.

    Results come from the index, so a note that exists on disk but has not yet been
    picked up by the indexer will not appear (lag is bounded by the index interval,
    typically up to 5 minutes).

    Notes whose content changed in a window: `order_by="content_changed"` with
    `since` (and optionally `until`). File mtime (the default order) also moves
    when a file is merely touched or re-synced; `content_changed` moves only when
    the indexed content hash changes.

    Args:
        folder: Vault-relative folder path (e.g. "Cards/", "Projects/"). Empty for vault root.
        limit: Maximum number of results (default 50).
        tags: Optional list of tag names; only notes carrying ALL listed tags match
            (e.g. ["idea"]).
        frontmatter: Optional dict of frontmatter key/value pairs; strict type match
            (e.g. {"status": "active"}).
        since: Optional UTC lower bound, inclusive: `YYYY-MM-DD` (that whole UTC
            day) or an ISO 8601 datetime with `Z` or `±HH:MM` (no offset is refused).
        until: Optional UTC upper bound, exclusive; a date includes that whole day.
        order_by: `"mtime"` (default) or `"content_changed"`; the bounds apply to it.
        cursor: The `more: cursor=…` value from the previous page of the same call.
            When any of since/until/order_by/cursor is given, a full page ends with
            that footer.
    """
    return await list_notes_impl(
        folder, limit=limit, tags=tags, frontmatter=frontmatter,
        since=since, until=until, order_by=order_by, cursor=cursor,
    )


@mcp.tool(title="List tags", annotations=_READ_ONLY)
async def get_tags(limit: int = 50) -> str:
    """List all tags used across the vault with note counts.

    Args:
        limit: Maximum number of tags to return (default 50)
    """
    return await get_tags_impl(limit=limit)


@mcp.tool(title="Recent notes", annotations=_READ_ONLY)
async def get_recent(
    limit: int = 20,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    since: str | None = None,
    until: str | None = None,
    order_by: str | None = None,
    cursor: str | None = None,
) -> str:
    """Get recently modified notes, newest first.

    Each line shows the note's modification time in UTC, as
    "YYYY-MM-DD HH:MM UTC".

    Notes whose content changed since a point in time:
    `order_by="content_changed"` with `since`. File mtime (the default order)
    also moves when a file is touched or re-synced without a content change;
    `content_changed` moves only when the indexed content hash changes, and each
    line is then labelled `changed`. For an exact list of creations, edits,
    moves and deletions since a stored cursor, see `get_changes`.

    Args:
        limit: Number of recent notes to return (default 20).
        folder: Optional folder prefix to filter (e.g. "Projects/").
        tags: Optional list of tag names; only notes carrying ALL listed tags match
            (e.g. ["meeting"]).
        frontmatter: Optional dict of frontmatter key/value pairs; strict type match
            (e.g. {"status": "active"}).
        since: Optional UTC lower bound, inclusive: `YYYY-MM-DD` (that whole UTC
            day) or an ISO 8601 datetime with `Z` or `±HH:MM` (no offset is refused).
        until: Optional UTC upper bound, exclusive; a date includes that whole day.
        order_by: `"mtime"` (default) or `"content_changed"`; the bounds apply to it.
        cursor: The `more: cursor=…` value from the previous page of the same call.
            When any of since/until/order_by/cursor is given, a full page ends with
            that footer.
    """
    return await get_recent_impl(
        limit=limit, folder=folder, tags=tags, frontmatter=frontmatter,
        since=since, until=until, order_by=order_by, cursor=cursor,
    )


@mcp.tool(title="Semantic search", annotations=_READ_ONLY)
async def semantic_search(
    query: str,
    limit: int = 15,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Vector similarity search over the vault's chunk embeddings. Use this for conceptual
    or paraphrased queries — anywhere exact word matching would miss the point.

    Exact identifiers, code symbols, titles or known phrases: `keyword_search`.
    Conceptual or paraphrased questions: `semantic_search`. Unsure which:
    `hybrid_search`.

    Each result is one note (deduped) with its closest-matching chunk as a ~200-character preview.
    Call `read_note` on a result's path to get the full note content.

    The header line carries a stale count and a truncated count, always —
    including when both are zero, so "nothing here is degraded" is
    distinguishable from a build that does not report it.

    `stale: true` on a row means the note changed after it was embedded: it was
    matched and ranked against its **previous** content, and its preview is
    **withheld** rather than shown, because that excerpt is text the note no
    longer has. Its path, title and tags are current — the indexer refreshed
    them, which is how the staleness is known at all — so `read_note` on that
    path returns the true content and is the remedy. Stale notes are never
    filtered out: during an embedding outage that would empty the result set
    rather than degrade it.

    `embedding_truncated: true` means the note is longer than the indexer's
    per-note chunk cap and only its head was embedded. A match against such a
    note is a match against its head; its tail is not reachable by semantic
    search at all, though `keyword_search` still covers the whole note.

    **The bound on the staleness signal, stated so it is not over-read:** it
    reports what the index has *committed*. An edit that the indexer has not
    yet scanned is not marked, so a note edited in the last few minutes may
    come back unmarked with a superseded preview. The guarantee is "no result
    presents text the index knows to be superseded", not "no result is ever
    out of date".

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


@mcp.tool(title="Create note", annotations=_ADDITIVE)
async def create_note(
    path: str, content: str, expected_hash: str | None = None
) -> str:
    """Create a new markdown note in the Obsidian vault. Requires write permission — a `readwrite` API key, or an OAuth token
    carrying the `readwrite` scope.

    See `get_vault_guide` for Obsidian syntax and any vault-specific conventions
    (naming, folder placement, frontmatter, tags).

    Refuses a path whose final component is a symlink, naming its target, so a
    write never lands on a note other than the one named; symlinked folders
    inside the vault work normally.

    The note is published no-clobber: the content is staged out of sight and
    linked into place in one kernel-atomic step, so an existing file at `path`
    can never be replaced by this tool. A vault filesystem that cannot stage an
    unnamed file refuses the write with an error naming
    `VAULT_ALLOW_NAMED_STAGING_FALLBACK` rather than staging under a visible
    name.

    **`expected_hash` — binding a write to the bytes you read.** Optional; omit
    it and nothing changes. Pass the `content_hash` a read returned, verbatim
    and canonical (`sha256:<64 lowercase hex>`), and the call is refused with
    nothing written if the file changed in between. It is always the **whole
    file's** hash, never a hash of the text you received. Two windows, both
    live: `expected_hash` covers your read → this call's read, the server's own
    pre-publication compare covers this call's read → its publication, and a
    match on the first does not exempt the second. Every refusal the server raises ends with one
    machine-readable `MCP-REFUSAL {"code":…}` line (an undeclared or mistyped argument is rejected by
    protocol validation before the tool runs and carries no such line) — `stale_precondition` (with
    the file's current hash, ready to resend), `concurrent_write`,
    `no_incumbent`, `malformed_precondition`, `precondition_unavailable`,
    `precondition_required` — and each states what resolves it.

    Here there are no incumbent bytes to bind: a supplied hash is answered
    `no_incumbent` before any filesystem work, nothing is created, and the
    remedy is to call again without it. A malformed hash is still reported as
    malformed first. Success reports the `content_hash` of the note this call
    published.

    Args:
        path: Vault-relative path for the new note (e.g. "Cards/New Topic.md"). The .md extension is added if missing.
        content: Full markdown content for the note, including any frontmatter.
        expected_hash: Accepted and always refused as `no_incumbent`, so the
            refusal is a normal result rather than a protocol error.
    """
    return await create_note_impl(path, content, expected_hash=expected_hash)


@mcp.tool(title="Edit note", annotations=_DESTRUCTIVE)
async def edit_note(
    path: str,
    content: str,
    append: bool = False,
    operation: str | None = None,
    find: str | None = None,
    section: str | None = None,
    replace_all: bool = False,
    dry_run: bool = False,
    replace_frontmatter: bool = False,
    expected_hash: str | None = None,
) -> str:
    """Edit an existing note in the Obsidian vault. Requires write permission (a `readwrite` API key or OAuth scope).

    Arguments: `path`, `content`, at most one mode flag — `append`, `find` or `section` (`operation` is a
    legacy alias) — plus `replace_all`, `dry_run`, `replace_frontmatter`, and the `expected_hash`
    precondition. Every refusal the server raises ends with one machine-readable `MCP-REFUSAL {"code":…}`
    line; an undeclared or mistyped argument is rejected by protocol validation before the tool runs and
    carries no such line. See `get_vault_guide` for vault conventions.

    Modes (mutually exclusive):
    1. **Full replace** (default): `content` becomes the note's **body**; an existing valid line-1 YAML
       frontmatter block is **preserved byte-identically** ahead of it. `replace_frontmatter=True` overwrites
       the entire file, frontmatter included. A note with no valid block is replaced wholesale.
       Change frontmatter with `set_frontmatter` or `find=`; never write back `read_note`'s `frontmatter` JSON
       view. The round-trip guarantee covers a complete, unwindowed whole-note read only — no `section`,
       `offset=0`, `truncated` false; page a truncated read to the end first, or full replacement replaces the
       whole body with the fragment. A section response belongs to section mode.
    2. **Append**: `append=True`; `content` is added at the end, after one newline.
    3. **Find & replace**: `find=<exact text>` is replaced with `content`; it must match exactly once unless
       `replace_all=True`.
    4. **Section**: `section=<heading>` replaces the **whole body** under the named ATX heading. `Parent/Child`
       disambiguates repeated headings; a `#N` ordinal ("#7", 1-based document order) is the only form that
       addresses duplicate siblings. A selector resolves to the same section here as in `read_note` on any
       write this tool admits — parity is about resolution, not admission.

    `dry_run=True` returns a unified diff and writes nothing.

    **`expected_hash`**: pass the `content_hash` a read returned, verbatim, and the call is refused with
    nothing written if the file changed since — in every mode, `dry_run` included. It is the **whole file's**
    hash, never a hash of the text you received. Optional by default — omitted, a stale edit overwrites —
    unless the deployment requires it, when an omitted hash is refused `precondition_required`. Codes:
    `stale_precondition`, `concurrent_write`, `no_incumbent`, `malformed_precondition`,
    `precondition_unavailable`, `precondition_required`. Success reports the `content_hash` of the bytes
    **this call wrote**, not what is on disk when you read it; `dry_run` reports none.

    **Section mode.** `content` is the section's **body**: the text beginning on the line immediately after the
    matched heading line; the heading line is never rewritten. The body is replaced **whole** — anything
    `content` does not resend, a fenced code block under the heading included, is deleted — so a blank
    line you want after the heading belongs in `content`. `read_note(path, section=...)` is the matching read:
    its `heading` field carries the heading line and its `content` field the body, and this tool takes exactly
    that `content`. Byte-identity holds for notes whose body newlines are LF: non-LF terminators inside the
    selected body come back as LF. `#N` ordinals agree with `read_note`; a heading inside a fenced code block is
    not a heading.

    **Two shapes refuse a section write outright, naming the problem and writing nothing:** a malformed
    frontmatter block, whose refusal names the `replace_frontmatter=True` repair; and a fence opener indented by
    one to three spaces that nothing below it closes — close the fence or unindent it to column zero, then
    reissue. Both are asymmetric with reads: `read_note` keeps working on such notes. A symlinked
    final component is refused.
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
        replace_frontmatter=replace_frontmatter,
        expected_hash=expected_hash,
    )


@mcp.tool(title="Vault guide", annotations=_READ_ONLY)
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


@mcp.tool(title="Backlinks", annotations=_READ_ONLY)
async def get_backlinks(path: str, limit: int = 50) -> str:
    """Notes that link TO `path`. Use this to discover what references a given
    note — projects citing a card, daily notes mentioning a person, etc.

    Resolved links only (dangling references are not counted as backlinks).

    Args:
        path: Vault-relative path to the target note (e.g. "Cards/Foo.md").
        limit: Maximum results (default 50, hard cap 500).
    """
    return await get_backlinks_impl(path, limit=limit)


@mcp.tool(title="Outgoing links", annotations=_READ_ONLY)
async def get_links(path: str, limit: int = 100) -> str:
    """Outgoing links from `path` — both resolved and dangling.

    Useful for "what does this note depend on?" or finding broken references
    that need follow-up notes.

    The result carries a `truncated` field. `truncated: true` means this note
    holds more links than the indexer's per-note cap and the list is the first
    N in document order only — treat it as incomplete rather than as the
    note's full outgoing-link set.

    Links come back in document order and are capped by `limit`; when more rows
    exist the result says how many of them were persisted, so a partial page is
    never read as the whole set. `limit` raises the page up to a hard cap of
    500 — this tool has no paging beyond that, so a note with more than 500
    link rows can only be read in full from the note itself.

    Each row's link text is clipped to 120 characters.

    Args:
        path: Vault-relative path to the source note.
        limit: Maximum links returned (default 100, hard cap 500).
    """
    return await get_links_impl(path, limit=limit)


@mcp.tool(title="Link neighborhood", annotations=_READ_ONLY)
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


@mcp.tool(title="Related notes", annotations=_READ_ONLY)
async def find_related(path: str, limit: int = 10) -> str:
    """Semantically similar notes based on the source note's chunk embeddings,
    averaged then queried via pgvector.

    Independent of the link graph — useful when the source is sparsely linked
    or when looking for thematic neighbors. For link-based exploration use
    `get_neighborhood`. For arbitrary topic queries use `semantic_search`.

    Carries `semantic_search`'s per-row markers and its header counts:
    `stale: true` means that neighbour changed after it was embedded, so it was
    ranked against its previous content and its preview is **withheld** —
    `read_note` returns the current text. `embedding_truncated: true` means
    only that note's head was embedded, so a match against it says nothing
    about its tail.

    **One marker is about the source rather than a neighbour.** If the note you
    asked about has itself changed since it was embedded, the result says so
    once, at the top — and it says so on the empty result too. The query vector
    is the average of the *source's stored* chunk vectors, so a stale source
    means every neighbour answers a question about content that note no longer
    has, and "no related notes" from a stale source means "nothing is near what
    this note used to say", not "this note has no neighbours". The next embed
    pass repairs it. A source with no vectors at all is a different message
    ("has not been embedded yet") with a different fix.

    **The bound on the staleness signal:** it reports what the index has
    committed, so an edit the indexer has not yet scanned is not marked.

    Args:
        path: Vault-relative path to the source note.
        limit: Maximum results (default 10, hard cap 50).
    """
    return await find_related_impl(path, limit=limit)


@mcp.tool(title="Orphan notes", annotations=_READ_ONLY)
async def find_orphans(folder: str | None = None, limit: int = 50) -> str:
    """Notes with zero incoming AND zero outgoing resolved links — useful for
    vault hygiene ("what's disconnected?") and cleanup decisions.

    Args:
        folder: Optional vault-relative folder prefix to scope the search
            (e.g. "Cards/").
        limit: Maximum results (default 50, hard cap 500).
    """
    return await find_orphans_impl(folder=folder, limit=limit)


@mcp.tool(title="Move note", annotations=_DESTRUCTIVE)
async def move_note(
    from_path: str,
    to_path: str,
    rewrite_links: bool = False,
    expected_hash: str | None = None,
) -> str:
    """Move or rename a note inside the vault. Requires write permission — a `readwrite` API key, or an OAuth token
    carrying the `readwrite` scope.

    Arguments: `from_path`, `to_path` (must not exist; parent directories are created), `rewrite_links`
    (default False) and the optional `expected_hash` precondition. The index follows the move, so backlinks
    keep working without rewriting source notes. Every refusal the server raises ends with one
    machine-readable `MCP-REFUSAL {"code":…}` line; an undeclared or mistyped argument is rejected by
    protocol validation before the tool runs and carries no such line.

    **`expected_hash`** binds **`from_path`'s own bytes and nothing else**: pass the `content_hash` a read
    returned, verbatim, and the move is refused with nothing written if that note
    changed since. It is compared at preflight, before the rename and before any rewrite; the rename then pins
    inode identity but does not compare the bytes again, so an in-place edit landing after that comparison is
    still moved. The backlink sources a `rewrite_links=True` move rewrites are *not* bound, because you never
    read them. Optional by default — omitted, the move proceeds — unless the deployment requires it, when
    an omitted hash is refused `precondition_required`. Codes: `stale_precondition`, `concurrent_write`,
    `no_incumbent`, `malformed_precondition`, `precondition_unavailable`, `precondition_required`.

    With `rewrite_links=True`, every source note that links to this note is rewritten in place — wikilinks,
    embeds, path-style links, and bare-href markdown links `[text](Old.md)`, whose new href is written
    relative to the linking note's own folder; angle-bracket hrefs `[text](<Old.md>)` are **not** rewritten.
    Aliases and anchors survive. **The moved note's own body is rewritten as well**, so a self-reference
    does not end up pointing at the old path. The rewrites are planned
    before anything changes: if one would push a source past the note size limit, or the planning would exceed
    the aggregate memory bound, the whole move is refused before any file is touched, naming the source or the
    count.

    **The same preflight refuses the whole move, before the rename, when any source it would rewrite — the
    moved note's own body included — contains a fence opener indented by one to three spaces that nothing below
    it closes.** The refusal names each such source and where its opener sits. Move with `rewrite_links=False`
    (unaffected by this refusal) and fix the links yourself, or close the fences first.

    **A rewrite can still fail after the move has committed.** The move is one rename and the rewrites follow
    it, so an I/O failure or a lost vault assignment stops the remaining rewrites and the result reads `partial
    success: …`, naming the sources that still link to the old path. The move is **not** rolled back; treat the
    link graph as agreeing with the vault bytes only on plain success, and fix the named sources with
    `edit_note`.

    Success reports the `content_hash` of the bytes actually published at `to_path` — the moved bytes for a
    plain move, the post-rewrite bytes when the moved note's own body was rewritten — and that value binds a
    following `edit_note(to_path, …, expected_hash=…)`. A failed rewrite stays a partial success and never
    claims nothing was written; if another writer changed `to_path` in between, **no hash is reported** and the
    result says to re-read `to_path` first.

    Writes are atomic; a symlinked final component on either path is refused. See `get_vault_guide` for
    vault folder conventions.

    Args:
        from_path: Vault-relative path of the existing note.
        to_path: Vault-relative path of the destination.
        rewrite_links: Also rewrite incoming links in source notes (off by default).
        expected_hash: `from_path`'s `content_hash` as you last read it.
    """
    return await move_note_impl(
        from_path,
        to_path,
        rewrite_links=rewrite_links,
        expected_hash=expected_hash,
    )


@mcp.tool(title="Delete note", annotations=_DESTRUCTIVE)
async def delete_note(
    path: str, permanent: bool = False, expected_hash: str | None = None
) -> str:
    """Delete a note from the vault. Requires write permission — a `readwrite` API key, or an OAuth token
    carrying the `readwrite` scope.

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

    **`expected_hash` — binding a write to the bytes you read.** Optional by default — omitted, the
    delete proceeds against whatever is on disk — unless the deployment requires it, when an omitted hash
    is refused `precondition_required`. Pass the `content_hash` a read returned, verbatim
    and canonical (`sha256:<64 lowercase hex>`), and the call is refused with
    nothing written if the file changed in between. It is always the **whole
    file's** hash, never a hash of the text you received. `expected_hash`
    covers your read through this call's preflight read. The delete acts through the
    pinned parent directory but does not compare the bytes again: an in-place edit after that
    comparison can still be deleted. Every refusal the server raises ends with one
    machine-readable `MCP-REFUSAL {"code":…}` line (an undeclared or mistyped argument is rejected by
    protocol validation before the tool runs and carries no such line) — `stale_precondition` (with
    the file's current hash, ready to resend), `concurrent_write`,
    `no_incumbent`, `malformed_precondition`, `precondition_unavailable`,
    `precondition_required` — and each states what resolves it.

    It applies in **both modes**, and the comparison runs before the `.trash`
    rename and before the unlink, so a refused delete leaves the note where it
    was and creates no trash entry. A successful delete reports **no**
    `content_hash`: nothing remains to hash.

    Args:
        path: Vault-relative path to the note.
        permanent: If True, unlink instead of soft-deleting.
        expected_hash: The note's `content_hash` as you last read it. Refuses
            the delete, changing nothing, if the note has changed since.
    """
    return await delete_note_impl(
        path, permanent=permanent, expected_hash=expected_hash
    )


@mcp.tool(title="Set frontmatter", annotations=_DESTRUCTIVE)
async def set_frontmatter(
    path: str,
    updates: dict | None = None,
    remove: list[str] | None = None,
    expected_hash: str | None = None,
) -> str:
    """Mutate a note's YAML frontmatter without touching its body. Requires write permission — a `readwrite` API key, or an OAuth token
    carrying the `readwrite` scope.

    Arguments: `path`, `updates` (keys to set), `remove` (keys to drop) and the optional `expected_hash`
    precondition. Parses the existing frontmatter, merges in `updates` (overwriting matching keys, adding new
    ones), then drops the keys in `remove`; the body is preserved byte-for-byte. A note with no frontmatter
    (no `---` fence on line 1) gets a fresh block prepended ahead of the unchanged body.

    **`expected_hash`**: pass the `content_hash` a read returned, verbatim (`sha256:<64 lowercase hex>`), and
    the call is refused with nothing written if the file changed since. It is the **whole file's** hash, never
    a hash of the text you received. Optional by default — omitted, a stale edit overwrites — unless the
    deployment requires it, when an omitted hash is refused `precondition_required`. The comparison runs ahead
    of the malformed-block diagnosis and ahead of the no-op check, so a stale base never yields "no changes"
    or a defect report about bytes you have not seen. Every refusal the server raises ends with one
    machine-readable `MCP-REFUSAL {"code":…}` line (an undeclared or mistyped argument is rejected by protocol
    validation before the tool runs and carries no such line) — `stale_precondition` (with the current hash),
    `concurrent_write`, `no_incumbent`, `malformed_precondition`, `precondition_unavailable`,
    `precondition_required` — each stating what resolves it. The server also re-compares the file immediately
    before publishing. A write
    reports the `content_hash` of the bytes **this call published**; a no-op publishes nothing and reports none.

    **A malformed block is refused, never worked around.** An unclosed line-1 fence, YAML that fails to parse,
    and YAML that is not a mapping (`null`, `~`, comments only, a list, a scalar) each return an error naming
    the defect and pointing at `edit_note(path, content, replace_frontmatter=True)` as the repair. Nothing is
    written — in particular no second block is prepended above the broken one — and `remove=` refuses
    identically rather than silently doing nothing, even for a call with no `updates` and no `remove`. An
    empty fenced block (`---` immediately followed by `---`) is *valid*: it is a valid empty mapping and is
    updated in place.

    **Only an effective change writes.** `updates` that set every named key to the value it already holds
    (compared type-sensitively, so `true` is not `1`) together with `remove` naming only absent keys report no
    changes and leave the file byte-identical. Removing the last key removes the block entirely — no fences, no
    separator, exactly the prior body.

    Re-serialization goes through PyYAML, which does NOT preserve YAML comments — any `# comment` in the
    original frontmatter is lost on the first `set_frontmatter` call. A path whose final component is a symlink
    is refused, naming its target; symlinked folders inside the vault work normally. See `get_vault_guide` for
    vault frontmatter conventions.

    Args:
        path: Vault-relative path to the note.
        updates: Mapping of keys to set. Use the empty dict (or omit) to skip.
        remove: List of keys to delete from the frontmatter. Missing keys are ignored (and, on their own, make
            the call a no-op rather than a write).
        expected_hash: The note's `content_hash` as you last read it. Refuses the write, changing nothing, if
            the note has changed since.
    """
    return await set_frontmatter_impl(
        path, updates=updates, remove=remove, expected_hash=expected_hash
    )


@mcp.tool(title="Read file", annotations=_READ_ONLY)
async def read_file(
    path: str,
    encoding: str = "auto",
    offset: int = 0,
    limit: int | None = None,
    hash_only: bool = False,
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
    reading large binaries. Any path with a component starting with `.` is
    rejected — dot-directories (`.obsidian`, `.git`, `.trash`, …) and dot-files
    alike — as is path traversal.

    Text results are additionally capped to a context-safe size: the cap bounds
    the returned window, and a truncated read appends a short notice carrying
    the `offset` to continue from. Base64 and image results are not windowed.

    The base64 header and `hash_only=True` return the whole raw file's
    `content_hash` (`sha256:<64 lowercase hex>`), which write/delete tools
    accept as `expected_hash`. Their path is a quoted JSON string. Text stays
    deliberately unenveloped. Use base64 for byte-exact frontmatter bytes;
    `read_note.frontmatter_yaml` has normalized line endings.

    Encoding is validated first, then hash_only/window compatibility, then
    ranges. `hash_only` refuses `offset != 0` or any non-None `limit`; explicitly
    passing `offset=0` is fine. A valid encoding has no effect in this mode.

    Args:
        path: Vault-relative path to the file (e.g. "Reference Docs/spec.pdf").
        hash_only: Return only path, byte count, MIME and hash, with no content.
        encoding: One of "auto" (default), "text", or "base64".
        offset: Character offset to start a text read from (default 0). Use the
            value the truncation notice reports to continue.
        limit: Maximum characters to return for a text read. Only lowers the
            server cap; it cannot raise it.
    """
    return await read_file_impl(
        path, encoding=encoding, offset=offset, limit=limit, hash_only=hash_only
    )


@mcp.tool(title="Write file", annotations=_DESTRUCTIVE)
async def write_file(
    path: str,
    content: str,
    encoding: str = "base64",
    overwrite: bool = False,
    expected_hash: str | None = None,
) -> str:
    """Write a file into the vault — including non-markdown (e.g. save a
    generated PDF or image). Requires write permission — a `readwrite` API key, or an OAuth token
    carrying the `readwrite` scope.
    Peer to `create_note`/`edit_note`, which stay markdown-only.

    `content` carries the bytes: with `encoding="base64"` (default) it is
    base64-decoded to raw bytes; with `encoding="text"` it is written verbatim
    as UTF-8. The write is atomic — the bytes are staged and flushed before
    anything is published — missing parent folders are created, and content over
    `MAX_FILE_WRITE_BYTES` (default 25 MB, decoded length) is refused.

    No-clobber by default: writing over an existing file requires
    `overwrite=True`. The default publishes by linking a staged, never-named
    inode into place in one kernel-atomic step, so an existing file cannot be
    replaced; `overwrite=True` publishes with a single same-directory rename
    instead. A vault filesystem that cannot stage an unnamed file refuses the
    no-clobber write with an error naming
    `VAULT_ALLOW_NAMED_STAGING_FALLBACK`, rather than staging under a visible
    name. Any path with a component starting with `.` (dot-directories and
    dot-files alike) and path traversal are rejected; invalid
    base64 errors without writing anything. A path whose final component is a
    symlink is refused, naming its target, so `overwrite=True` cannot clobber a
    file through an alias; symlinked folders inside the vault work normally.

    The MCP transport also bounds the whole request body (sized so a base64
    write at the cap always gets through). Base64 is therefore the always-safe
    encoding: `encoding="text"` content whose JSON escaping inflates past that
    bound is rejected by the transport with a bare HTTP 413 before this tool
    runs — send such content as base64 instead.

    `expected_hash` binds an existing whole file on `overwrite=True`; optional by default — omitted, the
    overwrite replaces whatever is on disk — unless the deployment requires it, when an omitted hash on
    an overwrite is refused `precondition_required`. Obtain its canonical `sha256:<64 lowercase hex>` value from `read_file`'s
    base64 header, `read_file(hash_only=True)`, or a prior write's success.
    Syntax is checked before path work; a hash with no-clobber or a missing
    destination is `no_incumbent`. An over-cap incumbent cannot be guarded.
    A stale hash refuses before publication; matching also enables the in-call
    comparison, which refuses an edit arriving during this call as
    `concurrent_write`. Without a hash, overwrite remains unconditional unless
    the deployment requires preconditions. Creation is exempt.

    Success reports the hash of the bytes this call published, not necessarily
    what remains when the response arrives. Over-cap incumbents or results omit
    the hash without failing an otherwise permitted unguarded write.

    Args:
        path: Vault-relative destination path (e.g. "Outputs/report.pdf").
        content: File contents — base64 string (default) or UTF-8 text.
        encoding: "base64" (default) or "text".
        overwrite: If True, replace an existing file. Off by default.
        expected_hash: Optional whole-file raw-byte digest of the incumbent.
    """
    return await write_file_impl(
        path, content, encoding=encoding, overwrite=overwrite, expected_hash=expected_hash
    )


@mcp.tool(title="List files", annotations=_READ_ONLY)
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
    subfolders and returns matching files. Anything with a path component
    starting with `.` is hidden — dot-directories (`.obsidian`, `.git`,
    `.trash`, …) **and dot-files** — and a `folder` with such a component is
    rejected.

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


@mcp.tool(title="Request upload link", annotations=_DESTRUCTIVE)
async def request_upload(
    path: str,
    overwrite: bool = False,
    expires_in: int | None = None,
) -> str:
    """Get a short-lived link a person can use to put a file into the vault.
    Requires write permission — a `readwrite` API key, or an OAuth token
    carrying the `readwrite` scope.
    Peer to `write_file`, which takes the bytes directly — use this one when you
    do not have them.

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


@mcp.tool(title="Check upload", annotations=_READ_ONLY)
async def check_upload(upload_id: str) -> str:
    """Ask what happened to an upload link you minted with `request_upload`.

    Returns one of `pending` (nothing sent yet), `uploading` (bytes are in
    flight), `completed` (with the path, size, sha256 and MIME type of what
    landed), `unknown` (a stream started and the server never recorded how it
    ended), `revoked` (the link is dead because the credential or vault root
    changed under it), or `expired`. Use it to confirm a transfer really
    finished before you tell the user it did, and to get the sha256 if they
    want to verify it.

    Visibility is scoped to the *principal* that minted the link, not to one
    credential row. For an API key that is the key itself. For OAuth it is the
    whole grant family behind the access token you are calling with, so a handle
    stays readable across the hourly token refresh that mints a new row. A
    different API key, a different client, or a separate approval of the same
    client reads as `not found`.

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


@mcp.tool(title="Request download link", annotations=_ADDITIVE)
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


@mcp.tool(title="Import from URL", annotations=_DESTRUCTIVE_OPEN_WORLD)
async def import_from_url(url: str, path: str, overwrite: bool = False) -> str:
    """Fetch a file from a public https URL straight into the vault. Requires write permission — a `readwrite` API key, or an OAuth token
    carrying the `readwrite` scope.
    Peer to `write_file` and `request_upload` — use this one when the bytes are
    already somewhere public.

    The server does the fetching, so nothing passes through your context: a
    20 MB PDF costs one tool call. Returns the path, size, sha256, MIME type
    and the final URL after any redirects.

    **Only genuinely public addresses.** This server sits on a private network
    next to a database and other services, so the fetch is restricted: https
    only — plain http is refused unless the operator has set
    `IMPORT_ALLOW_HTTP=true` — no credentials in the URL, no
    private/loopback/link-local/metadata addresses in any spelling, and the same
    rules re-checked at every redirect.
    A refusal names the rule that was violated — that is information about the
    URL, not a hint to work around it. Rewriting the URL to evade the check is
    never the right next step; ask the user for a public link instead.

    Size-capped at `MAX_FILE_WRITE_BYTES`, with one 30-second deadline for the
    whole fetch. No-clobber unless `overwrite=True`. Nothing is written unless
    the whole body arrives intact.

    This tool shares the transfer tools' preflight, so it also refuses when the
    server has no public origin configured (`MCP_HOSTNAME` or `BASE_URL`), even
    though it mints no link — that is an operator setting, not something to work
    around.

    Args:
        url: Public https URL of the file, at most 8,192 characters; a longer
            one is refused with `argument_too_long` before anything is fetched.
        path: Vault-relative destination (e.g. "Attachments/paper.pdf").
        overwrite: If True, allow replacing an existing file at `path`.
    """
    return await import_from_url_impl(url, path, overwrite=overwrite)


@mcp.tool(title="Delete file", annotations=_DESTRUCTIVE)
async def delete_file(
    path: str, permanent: bool = False, expected_hash: str | None = None
) -> str:
    """Delete a non-markdown file from the vault. Requires write permission — a `readwrite` API key, or an OAuth token
    carrying the `readwrite` scope.
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

    `expected_hash` binds the whole raw file in either mode; optional by default — omitted, the delete
    proceeds — unless the deployment requires it, when an omitted hash is refused `precondition_required`.
    Use the canonical `sha256:<64 lowercase hex>` from `read_file`'s base64 header,
    `read_file(hash_only=True)`, or a write success. Malformed hashes refuse
    before path checks; unavailable, required and stale preconditions refuse
    before any trash entry or unlink.
    This checks the caller-read-to-call window; a later concurrent replacement
    remains possible, so it is not an atomic filesystem compare-and-delete.
    A successful delete reports no content hash.

    Args:
        path: Vault-relative path to the file.
        permanent: If True, unlink instead of moving to `.trash/`.
        expected_hash: Optional whole-file raw-byte digest of the incumbent.
    """
    return await delete_file_impl(path, permanent=permanent, expected_hash=expected_hash)


# Registered after every other tool (#350, design D12), so the first 25 entries
# of `tools/list` keep the order `tests/fixtures/tools_list_schema_5a391ca.json`
# pins; tools added later append after it, in
# `tests/fixtures/tools_list_schema_additions.json`.
@mcp.tool(title="List tasks", annotations=_READ_ONLY)
async def list_tasks(
    folder: str | None = None,
    status: str = "open",
    due_by: str | None = None,
    scheduled_by: str | None = None,
    tags: list[str] | None = None,
    limit: int = 100,
    cursor: str | None = None,
) -> str:
    """List checkbox tasks (`- [ ] …`) across the vault in one call, from the index — no note is read.

    Reads the index, refreshed each pass (default every 300 s): a just-edited
    note may show its previous tasks until the next pass. Tasks-plugin and
    Dataview query blocks are fenced text and are not evaluated; only the
    checkbox lines themselves are tasks. A checkbox inside a fenced code block
    or inline code is not a task.

    Status: `[ ]`, `[/]` (in progress) and any other marker are `open`; `[x]`
    and `[X]` are `done`; `[-]` is `cancelled` — excluded from `open`,
    included in `all`. Each line shows the raw marker.

    Ordering: due date ascending, undated tasks last, then path, then line.
    When more rows match than fit, the header says `more: yes` and the
    response ends with `next: <cursor>`; pass it back as `cursor` with the same
    filters to continue. A cursor from other filters or another owner is
    refused (`invalid_argument`); so is a compact cursor whose note has since
    gone — start again without one.

    Each task is one line: the marker, the path in backticks with `:line`, then any of due,
    scheduled, start and done dates, priority, the task's own `#tags`, the
    heading path (`under:`), and `text:` last, as written (Tasks-plugin
    emoji included). Text past 1,000 characters is cut and labelled; a task
    whose tags were capped (64 kept) says `tags cut`. A first row too long for
    the response is shown abbreviated rather than skipped.

    The header always states `not yet scanned for tasks` (notes the index has
    not re-derived yet, e.g. right after an upgrade), `tasks could not be
    indexed` and `over the task cap` (5,000 per note), each even when zero, and
    with `tags` the count of tasks whose tags were cut. When either of the
    first two is non-zero the list is incomplete and the response says so —
    "no tasks" then does not mean none exist.

    Args:
        folder: Only notes under this path prefix (e.g. "Projects/").
        status: `open` (default), `done`, `cancelled` or `all`.
        due_by: `YYYY-MM-DD`; only tasks due on or before this date. Tasks
            with no due date are excluded when it is given.
        scheduled_by: `YYYY-MM-DD`; only tasks scheduled on or before this
            date. Tasks with no scheduled date are excluded when it is given.
        tags: Only tasks carrying every one of these inline tags on the task
            line itself (a leading `#` is optional; note-level tags are not
            consulted). At most 32, each 1–128 characters.
        limit: Maximum tasks per page (1–500, default 100). The response size
            cap can end a page sooner; `next` then continues it.
        cursor: The `next` value from the previous page of the same query.
    """
    return await list_tasks_impl(
        folder=folder, status=status, due_by=due_by, scheduled_by=scheduled_by,
        tags=tags, limit=limit, cursor=cursor,
    )


@mcp.tool(title="Change feed", annotations=_READ_ONLY)
async def get_changes(
    since: str,
    folder: str | None = None,
    kinds: list[str] | None = None,
    by: str = "any",
    limit: int = 50,
) -> str:
    """List what changed in your vault after a cursor: one line per note created, modified, moved or deleted, oldest first.

    `since` is either the `next_since` cursor a previous call returned
    (`ev:<number>`, exclusive and exact) or a UTC date/datetime
    (`YYYY-MM-DD`, or ISO 8601 with `Z`/`±HH:MM`). A date or datetime *starts*
    a feed and is approximate; `next_since` *continues* one without losing or
    repeating an event. Store `next_since` (it is in the header and the last
    line) in your own notes and pass it next session. A cursor means
    something only for your own vault; one above this vault's last event is
    refused (`invalid_argument`).

    Each line: `- ev:<n> <UTC time> <kind> "<path>"` (a move shows
    `"<from>" -> "<to>"`; paths are JSON-quoted, so a newline in a name is
    `\n`), then `reason=…` where it applies, then who: `by api_key "<name>"
    via <tool>` or `by oauth "<client>" via <tool>`, marked `(you)` when it was
    your own credential, or `by human/sync` for anything the index discovered
    — edits outside MCP (Obsidian, a sync client, git, a shell) are
    `human/sync`. `created` means first seen at this path: a new note, one half
    of a rename that could not be paired, or a note that became indexable.
    `deleted` always carries `reason=`: `removed` (the file is gone),
    `not_indexable` (still on disk but cannot be indexed) or `quarantined`.

    Two meta lines are always shown, whatever the filters: `gap` (events not
    itemised, with a count) and `reset` (the index was rebuilt from empty).
    After either, resync with `get_recent(order_by="content_changed",
    since=…)`. `history_truncated: true` in the header means events older than
    the retained history (default 90 days) were removed.

    Args:
        since: A `next_since` cursor (`ev:<n>`) or a UTC date/datetime.
        folder: Only events whose path or source path is under this prefix (a
            move out of the folder counts).
        kinds: Subset of `created`, `modified`, `moved`, `deleted`.
        by: `any` (default); `me` (your exact credential — the same API key,
            or the same OAuth grant across token refreshes); `agents` (any MCP
            caller of this vault); `human` (`human/sync`, discovered by the index).
        limit: Events per page (1–500, default 50). A full page says
            `more: true`; pass its `next_since` to continue.
    """
    return await get_changes_impl(
        since=since, folder=folder, kinds=kinds, by=by, limit=limit
    )


@mcp.tool(title="Hybrid search", annotations=_READ_ONLY)
async def hybrid_search(
    query: str,
    limit: int = 10,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
) -> str:
    """Keyword and semantic search fused into one ranking, for a query that is not clearly an identifier or a paraphrase.

    Exact identifiers, code symbols, titles or known phrases: `keyword_search`.
    Conceptual or paraphrased questions: `semantic_search`. Unsure which:
    `hybrid_search`.

    Both arms run for the same query and filters in one database snapshot:
    the `keyword_search` ranking and the `semantic_search` ranking, each taken
    to its top 50. They are fused by reciprocal rank fusion over note paths
    with equal votes: a note's score is the sum, over the arms that found it,
    of `1/(k + rank)` (`k` is the server's `HYBRID_RRF_K`, shown as `rrf_k`).
    A note outside both top-50 lists is not considered; the header's
    `arm_depth: keyword n/50, semantic c/50 chunks` says when an arm was full.
    Fusion helps when the query's kind is unclear, and a single arm can rank
    better than it on its own kind of query (an exact identifier, or a pure
    paraphrase); use that arm's tool then.

    One line per result: a JSON object of the note-controlled fields
    `{"title","path","tags","preview_kind","preview"}` (every value a JSON
    string, so note text cannot forge a field), then the server fields:
    `score`, semantic_rank (with similarity) or `-`, keyword_rank (with
    `match` and `matched_in`) or `-`, and `explain` (the `1/(k+rank)` terms
    that make the score). `preview_kind` is `snippet` (a keyword highlight in
    `«»`), `head`, `chunk` (semantic), `stale_notice` or `none`, from the arm
    that ranked the note better. `stale: true` and `embedding_truncated: true`
    mean what they mean in `semantic_search`. A long response drops whole rows
    from the end and says how many; a first row that does not fit is cut and
    labelled. Call `read_note` on a path for the full note.

    The header always says `arms:`. `arms: both` is a fused answer. `arms:
    keyword-only (semantic unavailable: provider_error | provider_input_rejected)`
    means the embedding provider failed or refused the query: the page is the
    keyword arm alone, in keyword order, and `semantic_search` will fail the
    same way until the provider recovers.

    Args:
        query: What you are looking for — words, a phrase or a question.
        limit: Maximum number of results (default 10, at most 50).
        folder: Optional folder prefix (e.g. "Projects/").
        tags: Optional list of tag names; only notes carrying ALL listed tags match.
        frontmatter: Optional dict of frontmatter key/value pairs; strict type matching —
            string "0" does not match integer 0 (e.g. {"status": "active"}).
    """
    return await hybrid_search_impl(
        query, limit=limit, folder=folder, tags=tags, frontmatter=frontmatter
    )


def _forbid_unknown_arguments(server: FastMCP) -> None:
    """Refuse undeclared tool arguments on every registered tool (#295).

    FastMCP builds each tool's argument model on `ArgModelBase`, which sets no
    `extra`, so pydantic's default `ignore` silently drops any name the tool
    does not declare: `keyword_search(folders=...)` returned *unfiltered*
    results that the calling agent took to be filtered. Each model is swapped
    for a same-named subclass with `extra="forbid"` (other config inherited),
    so the SDK's existing validation path refuses the call — naming the
    argument — before any tool body runs; the published schema gets
    `additionalProperties: false` so a client can catch it up front.

    One pass after the last registration, so a tool added later is covered
    without opting in. It reaches into SDK internals (`_tool_manager`,
    `Tool.fn_metadata.arg_model`, `Tool.parameters`); an SDK upgrade that moves
    them fails `tests/test_issue_295_unknown_arguments.py` rather than quietly
    reverting to ignore.
    """
    for tool in server._tool_manager.list_tools():
        base = tool.fn_metadata.arg_model
        tool.fn_metadata.arg_model = type(
            base.__name__,
            (base,),
            {
                "__module__": base.__module__,
                "__doc__": base.__doc__,
                "model_config": ConfigDict({**base.model_config, "extra": "forbid"}),
            },
        )
        tool.parameters["additionalProperties"] = False


# `MCP_REJECT_UNKNOWN_ARGUMENTS=false` is the rollback: the SDK's ignore
# behaviour and unmodified schemas. `src/main.py` logs it at WARNING on start.
if settings.mcp_reject_unknown_arguments:
    _forbid_unknown_arguments(mcp)
