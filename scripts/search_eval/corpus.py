"""Searchable membership per arm, for the snapshot and the replay corpus (#356, D6).

| membership             | a judged path is a member when ...                          |
|------------------------|-------------------------------------------------------------|
| `metadata`             | its `notes_metadata` row exists (owner matched NULL-safe)    |
| `keyword`              | `notes_metadata.content_tsvector IS NOT NULL`                |
| `semantic`             | at least one `note_embeddings` row has `note_id = m.id`      |
| `keyword_and_semantic` | both `keyword` and `semantic`                                |
| `keyword_or_semantic`  | `keyword` or `semantic` (a union-retrieving arm, #354)       |

Corpus loss is the replay database no longer holding what the snapshot held.
For every membership but one that is "not a member in the replay". For
`keyword_or_semantic` it is **per constituent** (`lost`): a path counts as
lost when any constituent the snapshot marked present (`keyword`, `semantic`
or both) is absent from the replay, even if the other one still reaches it —
the candidate changed what that constituent can see.

`embedded_content_hash` is **not** part of membership: under #200 a stale
vector is annotated and still returned. The snapshot records it as
`stale_vector`, for information only.

A corpus file is `{owner: {path: {...}}}`, where owner is the `user_id` as a
string or `"null"`. A database snapshot's entries are `{metadata, keyword,
semantic, stale_vector}`; the synthetic fixture's are `{title, body, keyword,
semantic}`, with `metadata` implied by the entry existing.
"""
from __future__ import annotations

import json
from pathlib import Path

MEMBERSHIPS: tuple[str, ...] = (
    "metadata", "keyword", "semantic", "keyword_and_semantic", "keyword_or_semantic",
)

#: The constituents whose loss is checked one by one (see `lost`).
_PER_CONSTITUENT = {"keyword_or_semantic": ("keyword", "semantic")}


def owner_key(user_id: int | None) -> str:
    return "null" if user_id is None else str(user_id)


class Corpus:
    def __init__(self, data: dict):
        if not isinstance(data, dict) or not all(isinstance(v, dict) for v in data.values()):
            raise ValueError("a corpus is {owner: {path: entry}}")
        self.data = data

    @classmethod
    def load(cls, path: Path) -> "Corpus":
        return cls(json.loads(Path(path).read_text(encoding="utf-8")))

    def entry(self, user_id: int | None, path: str) -> dict | None:
        return self.data.get(owner_key(user_id), {}).get(path)

    def has(self, user_id: int | None, path: str) -> bool:
        return self.entry(user_id, path) is not None

    def member(self, user_id: int | None, path: str, membership: str) -> bool:
        e = self.entry(user_id, path)
        if e is None:
            return False
        if membership == "metadata":
            return bool(e.get("metadata", True))
        if membership == "keyword":
            return bool(e.get("keyword"))
        if membership == "semantic":
            return bool(e.get("semantic"))
        if membership == "keyword_and_semantic":
            return bool(e.get("keyword")) and bool(e.get("semantic"))
        if membership == "keyword_or_semantic":
            return bool(e.get("keyword")) or bool(e.get("semantic"))
        raise ValueError(f"unknown membership {membership!r}")

    def owner_entries(self, user_id: int | None) -> dict:
        return self.data.get(owner_key(user_id), {})


def lost(snapshot: Corpus, replay: Corpus, user_id: int | None, path: str,
         membership: str) -> bool:
    """Whether a path the snapshot marks searchable under `membership` is
    corpus loss in `replay`. The caller has already checked
    `snapshot.member(...)`."""
    constituents = _PER_CONSTITUENT.get(membership)
    if constituents is None:
        return not replay.member(user_id, path, membership)
    return any(
        snapshot.member(user_id, path, c) and not replay.member(user_id, path, c)
        for c in constituents
    )
