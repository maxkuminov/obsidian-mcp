"""Shared SQL filter helper for NoteMetadata queries.

This is the single supported way to apply `folder`, `tags`, and `frontmatter`
filters to a `select` over `NoteMetadata`. Inlining the equivalents in callers
risks divergence (escape rules, containment semantics).
"""

from sqlalchemy import Select

from src.models.db import NoteMetadata


def _escape_like(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def apply_note_filters(
    stmt: Select,
    *,
    folder: str | None = None,
    tags: list[str] | None = None,
    frontmatter: dict | None = None,
    user_id: int | None = None,
) -> Select:
    """Append optional `folder`, `tags`, `frontmatter`, `user_id` predicates
    to a select over NoteMetadata.

    - `folder`: prefix match on `file_path`. LIKE wildcards (`%`, `_`, `\\`) are escaped.
    - `tags`: ARRAY containment (`notes_metadata.tags @> ARRAY[...]`). AND semantics.
    - `frontmatter`: JSONB containment (`notes_metadata.frontmatter @> :json`). Strict types.
    - `user_id`: **always** scoped, by a total mapping — `None` appends
      `notes_metadata.user_id IS NULL` and an `int` appends
      `notes_metadata.user_id = :uid`. `None` is a scoping value, not the
      absence of one.

    `folder`, `tags` and `frontmatter` are optional: a None or empty argument
    means "no filter" and the predicate is not appended. `user_id` is not one
    of those — see below.
    """
    # The owner predicate is total, and that is the whole of #127's read-path
    # fix. `None` used to append nothing, while every write path maps `None` to
    # `user_id IS NULL`; on a database that holds rows owned by named users —
    # which `MULTI_USER_MODE` being off does not prevent, because the flag can
    # be turned off after users exist — an ownerless credential read *every*
    # tenant's paths, titles, tags, frontmatter and chunk excerpts. The NULL
    # slice is exactly what such a credential owns, so that is what it reads.
    # A single-user deployment is unaffected: every row there is NULL-owned.
    #
    # A consequence the vector paths depend on: there is now no such thing as
    # an unfiltered query through this helper, so a zero-row approximate scan
    # is always ambiguous and always re-runs exact (see `semantic_search`).
    if folder:
        escaped = _escape_like(folder)
        stmt = stmt.where(NoteMetadata.file_path.like(f"{escaped}%", escape="\\"))
    if tags:
        stmt = stmt.where(NoteMetadata.tags.contains(tags))
    if frontmatter:
        stmt = stmt.where(NoteMetadata.frontmatter.contains(frontmatter))
    if user_id is None:
        stmt = stmt.where(NoteMetadata.user_id.is_(None))
    else:
        stmt = stmt.where(NoteMetadata.user_id == user_id)
    return stmt
