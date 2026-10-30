"""Mine implicit relevance judgments from the usage export (#356, design D2).

Pure Python over the JSON lines `scripts/search_eval_export.sql` writes, so the
join, the part most likely to be subtly wrong, is unit-tested without a
database. Nothing here does I/O except `load_export` / `load_manual`, which
read the one file the caller names.

**A judgment.** A usable `read_note` row R for path p, by principal P, credits
`(query, p)` only when a usable `keyword_search` / `semantic_search` /
`hybrid_search` (#354; a keyword-only hybrid row is usable) row S by
the same P satisfies `R.created_at - W < S.created_at < R.created_at` and S's
logged `result_paths` contains p by exact string equality. When several
searches qualify, only the **latest** is credited (ties on `created_at` are
broken by row id), so one read credits at most one search. A read that credits
nothing is counted as `unattributed_reads`.

**The principal** is the credential principal the rate limiter uses
(`src/auth/session.py`): `(user_id, "api_key", key_id)` when `key_id` is set,
else `(user_id, "oauth", grant_id)` when `oauth_token_id` resolved to a token
row carrying a `grant_id`. Any other row is excluded as
`unresolved_principal` and is **never** merged under `actor_ref`: one OAuth
`client_id` covers many independently revocable grants, so merging under it
would credit one agent's read to another agent's search.

**Usable rows.** A row is usable iff `params.error` is absent or null and
`params.over_quota` is not true: the Python mirror of
`search_analytics.analysable_sql` (every pre-body marker except `over_quota`
is an `error` value). A usable search also carries a `result_paths` array of
strings and a non-empty `query` string the 200-character param cap did not
truncate (length 201 ending in `…`); a usable read carries a non-truncated
`path` string.

**Queries.** A query id is the first 16 hex digits of the sha256 of the
canonical JSON of `[user_id, normalized text, {"folder", "tags",
"frontmatter"}]`, where the text has whitespace runs collapsed and is
stripped, case preserved. All usable searches sharing an id fold into one
query: its relevant set is the union of the credited paths (grade 1), its pool
the union of every logged `result_paths`.
"""
from __future__ import annotations

import bisect
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from scripts.search_eval import UsageError
from scripts.search_eval._io import canonical_json
from scripts.search_eval.classify import CLASSES, classify

SEARCH_TOOLS = ("keyword_search", "semantic_search", "hybrid_search")
READ_TOOL = "read_note"
EXPORT_TOOLS = (*SEARCH_TOOLS, READ_TOOL)

#: `src/mcp_server/tools.py` `_MAX_PARAM_LEN`: top-level strings longer than
#: this are cut to it and get `…` appended.
MAX_PARAM_LEN = 200
TRUNCATION_MARK = "…"

DEFAULT_WINDOW_SECONDS = 120

#: PostgreSQL's spellings of boolean true, as `->>`'s text form reaches
#: `::boolean` in `pre_body_refusal_sql`.
_PG_TRUE = frozenset({"t", "true", "y", "yes", "on", "1"})

_WS = re.compile(r"\s+")


class ManualError(UsageError, ValueError):
    """An operator-written judgment line is invalid (a usage error, exit 64)."""


# --------------------------------------------------------------------------
# Loading the export.
# --------------------------------------------------------------------------


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _opt_int(v: Any) -> bool:
    return v is None or _is_int(v)


def _opt_str(v: Any) -> bool:
    return v is None or isinstance(v, str)


def _valid_row(obj: Any) -> bool:
    if not isinstance(obj, dict):
        return False
    return (
        _is_int(obj.get("id"))
        and _is_int(obj.get("created_at"))
        and isinstance(obj.get("tool"), str)
        and _opt_int(obj.get("user_id"))
        and _opt_str(obj.get("actor_kind"))
        and _opt_str(obj.get("actor_ref"))
        and _opt_int(obj.get("key_id"))
        and _opt_int(obj.get("oauth_token_id"))
        and _opt_str(obj.get("grant_id"))
        and (obj.get("user_filter") is None or isinstance(obj.get("user_filter"), str)
             or _is_int(obj.get("user_filter")))
        and _opt_int(obj.get("deployment_users"))
        and (obj.get("params") is None or isinstance(obj.get("params"), dict))
    )


@dataclass
class LoadResult:
    rows: list[dict]
    malformed: int = 0


def parse_export_lines(lines: Iterable[str]) -> LoadResult:
    """Type-check every line; count the malformed ones and never raise on them."""
    rows: list[dict] = []
    malformed = 0
    for line in lines:
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if not _valid_row(obj):
            malformed += 1
            continue
        rows.append(obj)
    return LoadResult(rows=rows, malformed=malformed)


def _decoded_lines(path: Path):
    """Each line decoded strictly; an undecodable line is yielded as None."""
    with open(path, "rb") as fh:
        for raw in fh:
            try:
                yield raw.decode("utf-8")
            except UnicodeDecodeError:
                yield None


def load_export(path: Path) -> LoadResult:
    """Read the export. A line that is not valid UTF-8 is malformed, never repaired."""
    lines = list(_decoded_lines(path))
    result = parse_export_lines(line for line in lines if line is not None)
    result.malformed += sum(1 for line in lines if line is None)
    return result


# --------------------------------------------------------------------------
# Row predicates.
# --------------------------------------------------------------------------


def principal_of(row: dict) -> tuple[str, Any] | None:
    """`("api_key", key_id)`, `("oauth", grant_id)`, or None (never `actor_ref`).

    An OAuth principal needs both `oauth_token_id` and the `grant_id` it
    resolved to; either alone is not a credential the log can attribute.
    """
    if row.get("key_id") is not None:
        return ("api_key", row["key_id"])
    if row.get("oauth_token_id") is not None and row.get("grant_id") is not None:
        return ("oauth", row["grant_id"])
    return None


def is_truncated(s: str) -> bool:
    """True when the param cap cut this logged string (201 chars ending in `…`)."""
    return len(s) == MAX_PARAM_LEN + 1 and s.endswith(TRUNCATION_MARK)


def _over_quota(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, str)):
        return str(value).strip().lower() in _PG_TRUE
    return False


def _refusal_reason(params: dict) -> str | None:
    if params.get("error") is not None:
        return "error"
    if _over_quota(params.get("over_quota")):
        return "over_quota"
    return None


def passes_usable_filter(params: dict | None) -> bool:
    """D1's usable filter alone: the Python mirror of `analysable_sql`."""
    return _refusal_reason(params or {}) is None


def usable_search(row: dict) -> tuple[bool, str | None]:
    """`(True, None)` for a usable search row, else `(False, reason)`."""
    if row.get("tool") not in SEARCH_TOOLS:
        return False, "other_tool"
    params = row.get("params") or {}
    reason = _refusal_reason(params)
    if reason:
        return False, reason
    if "result_paths" not in params:
        return False, "no_telemetry"
    paths = params["result_paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths):
        return False, "bad_result_paths"
    query = params.get("query")
    if not isinstance(query, str) or not normalize_text(query):
        return False, "no_query"
    if is_truncated(query):
        return False, "query_truncated"
    folder = params.get("folder")
    tags = params.get("tags")
    frontmatter = params.get("frontmatter")
    if (
        not _opt_str(folder)
        or not (tags is None or (isinstance(tags, list) and all(isinstance(t, str) for t in tags)))
        or not (frontmatter is None or isinstance(frontmatter, dict))
    ):
        return False, "bad_filters"
    if isinstance(folder, str) and is_truncated(folder):
        # A cut folder filter cannot be replayed faithfully either.
        return False, "folder_truncated"
    return True, None


def usable_read(row: dict) -> tuple[bool, str | None]:
    """`(True, None)` for a usable read row, else `(False, reason)`."""
    if row.get("tool") != READ_TOOL:
        return False, "other_tool"
    params = row.get("params") or {}
    reason = _refusal_reason(params)
    if reason:
        return False, reason
    path = params.get("path")
    if not isinstance(path, str) or not path:
        return False, "no_path"
    if is_truncated(path):
        return False, "path_truncated"
    return True, None


# --------------------------------------------------------------------------
# Query identity.
# --------------------------------------------------------------------------


def normalize_text(text: str) -> str:
    return _WS.sub(" ", text).strip()


def filters_of(obj: dict) -> dict:
    return {
        "folder": obj.get("folder"),
        "tags": obj.get("tags"),
        "frontmatter": obj.get("frontmatter"),
    }


def query_id(user_id: int | None, text: str, filters: dict) -> str:
    payload = canonical_json([user_id, normalize_text(text), filters])
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------
# Provenance (D10).
# --------------------------------------------------------------------------


def derive_provenance(rows: list[dict], *, user_filter: int | None = None) -> dict:
    """`{"users": 1 | "multi", "basis": ...}` for the rows the miner read.

    `users` is 1 when the export was restricted to one user (every row carries
    the same non-null `user_filter` and that `user_id`, or the miner's own
    `--user-id` filtered it), or when the deployment has at most one user and
    the rows hold at most one distinct `user_id` (NULL counts as one value).
    Anything else is `"multi"`, including an empty export (no evidence).
    Malformed lines contribute nothing to any count but `malformed_lines`, so
    they do not change the provenance.
    """
    if not rows:
        return {"users": "multi", "basis": "empty"}
    if user_filter is not None:
        if all(r.get("user_id") == user_filter for r in rows):
            return {"users": 1, "basis": "user_filter"}
        return {"users": "multi", "basis": "inconsistent_user_filter"}
    filters = {None if r.get("user_filter") is None else str(r["user_filter"]) for r in rows}
    if filters != {None}:
        if len(filters) != 1:
            return {"users": "multi", "basis": "inconsistent_user_filter"}
        (only,) = filters
        try:
            wanted = int(only)
        except ValueError:
            return {"users": "multi", "basis": "inconsistent_user_filter"}
        if all(r.get("user_id") == wanted for r in rows):
            return {"users": 1, "basis": "user_filter"}
        return {"users": "multi", "basis": "inconsistent_user_filter"}
    deployment = [r.get("deployment_users") for r in rows]
    owners = {r.get("user_id") for r in rows}
    if all(d is not None and d <= 1 for d in deployment) and len(owners) <= 1:
        return {"users": 1, "basis": "single_user_deployment"}
    return {"users": "multi", "basis": "multi_user"}


# --------------------------------------------------------------------------
# The join.
# --------------------------------------------------------------------------


@dataclass
class MineResult:
    queries: list[dict]
    qrels: dict[str, dict[str, int]]
    report: dict = field(default_factory=dict)


@dataclass
class _Query:
    qid: str
    user_id: int | None
    text: str
    folder: Any
    tags: Any
    frontmatter: Any
    pool: set = field(default_factory=set)
    relevant: set = field(default_factory=set)
    source_tools: set = field(default_factory=set)
    sources: set = field(default_factory=set)
    n_searches: int = 0
    explicit_class: str | None = None

    def as_dict(self) -> dict:
        return {
            "qid": self.qid,
            "user_id": self.user_id,
            "text": self.text,
            "folder": self.folder,
            "tags": self.tags,
            "frontmatter": self.frontmatter,
            "class": self.explicit_class or classify(self.text),
            "sources": sorted(self.sources),
            "source_tools": sorted(self.source_tools),
            "pool": sorted(self.pool),
            "n_searches": self.n_searches,
        }


def mine(rows: list[dict], *, window_seconds: float = DEFAULT_WINDOW_SECONDS,
         manual: list[dict] | None = None, malformed: int = 0,
         user_filter: int | None = None) -> MineResult:
    """Pair reads with the searches that surfaced them (D2). Pure."""
    window_us = int(round(window_seconds * 1_000_000))
    excluded: Counter = Counter()
    excluded_by_kind: dict[str, Counter] = {"search": Counter(), "read": Counter()}

    if user_filter is not None:
        kept = [r for r in rows if r.get("user_id") == user_filter]
        filtered_out = len(rows) - len(kept)
        rows = kept
    else:
        filtered_out = 0

    searches: list[dict] = []
    reads: list[dict] = []
    for row in rows:
        tool = row.get("tool")
        if tool in SEARCH_TOOLS:
            kind, (ok, reason) = "search", usable_search(row)
        elif tool == READ_TOOL:
            kind, (ok, reason) = "read", usable_read(row)
        else:
            excluded["other_tool"] += 1
            continue
        if ok and principal_of(row) is None:
            ok, reason = False, "unresolved_principal"
        if not ok:
            excluded[reason] += 1
            excluded_by_kind[kind][reason] += 1
            continue
        (searches if kind == "search" else reads).append(row)

    # Fold searches into queries, and index them per (principal, path).
    queries: dict[str, _Query] = {}
    search_qid: dict[int, str] = {}
    index: dict[tuple, list[tuple[int, int, int]]] = defaultdict(list)
    for pos, s in enumerate(searches):
        p = s["params"]
        text = normalize_text(p["query"])
        filters = filters_of(p)
        qid = query_id(s.get("user_id"), text, filters)
        q = queries.get(qid)
        if q is None:
            q = queries[qid] = _Query(qid=qid, user_id=s.get("user_id"), text=text,
                                      sources={"mined"}, **filters)
        q.n_searches += 1
        q.pool.update(p["result_paths"])
        search_qid[pos] = qid
        principal = (s.get("user_id"), *principal_of(s))
        for path in set(p["result_paths"]):
            index[(principal, path)].append((s["created_at"], s["id"], pos))
    for entries in index.values():
        entries.sort()

    credited_pairs: set[tuple[str, str]] = set()
    credits = 0
    unattributed = 0
    for r in reads:
        principal = (r.get("user_id"), *principal_of(r))
        path = r["params"]["path"]
        entries = index.get((principal, path))
        hit = None
        if entries:
            # Latest search strictly earlier than the read.
            i = bisect.bisect_left(entries, (r["created_at"], -1, -1)) - 1
            if i >= 0 and entries[i][0] > r["created_at"] - window_us:
                hit = entries[i]
        if hit is None:
            unattributed += 1
            continue
        credits += 1
        pos = hit[2]
        qid = search_qid[pos]
        credited_pairs.add((qid, path))
        queries[qid].relevant.add(path)
        queries[qid].source_tools.add(searches[pos]["tool"])

    without_judgment = sum(1 for q in queries.values() if not q.relevant)
    emitted = {qid: q for qid, q in queries.items() if q.relevant}

    provenance = derive_provenance(rows, user_filter=user_filter)
    # Under a user filter (the export's USER_ID= or `--user-id`) the report may
    # carry one owner only, so another owner's manual judgment is excluded and
    # counted rather than let in under an attachable provenance.
    owner_filter = user_filter
    if owner_filter is None and provenance["basis"] == "user_filter":
        owner_filter = int(rows[0]["user_filter"])
    n_manual_lines = 0
    n_manual_other_owner = 0
    n_both = 0
    if manual:
        for m in manual:
            n_manual_lines += 1
            if owner_filter is not None and m["user_id"] != owner_filter:
                n_manual_other_owner += 1
                continue
            qid = m["qid"]
            q = emitted.get(qid)
            if q is None and qid in queries:
                # Mined, but no read credited it: keep its logged pool, and it
                # joins only the manual slice, having no mined judgment.
                q = emitted[qid] = queries[qid]
                q.sources = {"manual"}
            if q is None:
                q = emitted[qid] = _Query(qid=qid, user_id=m["user_id"], text=m["text"],
                                          folder=m["folder"], tags=m["tags"],
                                          frontmatter=m["frontmatter"], sources={"manual"})
            q.relevant.update(m["relevant"])
            q.pool.update(m["relevant"])
            q.sources.add("manual")
            if m.get("class"):
                q.explicit_class = m["class"]
        n_both = sum(1 for q in emitted.values() if q.sources == {"mined", "manual"})

    if provenance["users"] == 1:
        # Every judgment that enters the report counts, manual ones included.
        allowed = {owner_filter} if owner_filter is not None else {r.get("user_id") for r in rows}
        if {q.user_id for q in emitted.values()} - allowed:
            provenance = {"users": "multi", "basis": "manual_other_owner"}

    out_queries = [emitted[qid].as_dict() for qid in sorted(emitted)]
    qrels = {qid: {p: 1 for p in sorted(emitted[qid].relevant)} for qid in sorted(emitted)}
    class_counts = Counter(q["class"] for q in out_queries)
    source_counts = Counter(s for q in out_queries for s in q["sources"])
    report = {
        "window_seconds": window_seconds,
        "rows_total": len(rows) + filtered_out,
        "rows_filtered_by_user_id": filtered_out,
        "malformed_lines": malformed,
        "usable_searches": len(searches),
        "usable_reads": len(reads),
        "excluded": dict(sorted(excluded.items())),
        "excluded_by_kind": {k: dict(sorted(v.items())) for k, v in excluded_by_kind.items()},
        "credited_reads": credits,
        "judgments": len(credited_pairs),
        "unattributed_reads": unattributed,
        "mined_queries": len(queries),
        "queries_without_judgment": without_judgment,
        "manual_lines": n_manual_lines,
        "n_queries": len(out_queries),
        "n_both_sources": n_both,
        "per_class": {c: class_counts.get(c, 0) for c in CLASSES},
        "per_source": {s: source_counts.get(s, 0) for s in ("mined", "manual")},
        "manual_excluded_other_owner": n_manual_other_owner,
        "provenance": provenance,
    }
    return MineResult(queries=out_queries, qrels=qrels, report=report)


# --------------------------------------------------------------------------
# Operator-written judgments.
# --------------------------------------------------------------------------

_MANUAL_KEYS = {"user_id", "text", "folder", "tags", "frontmatter", "relevant", "class"}


def parse_manual_lines(lines: Iterable[str]) -> list[dict]:
    """Validate operator judgments; any invalid line is a `ManualError`."""
    out: list[dict] = []
    for n, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError as e:
            raise ManualError(f"manual line {n}: not JSON") from e
        if not isinstance(obj, dict):
            raise ManualError(f"manual line {n}: not an object")
        unknown = set(obj) - _MANUAL_KEYS
        if unknown:
            raise ManualError(f"manual line {n}: unknown keys {sorted(unknown)}")
        if "user_id" not in obj or not _opt_int(obj["user_id"]):
            raise ManualError(f"manual line {n}: user_id must be an integer or null")
        text = obj.get("text")
        if not isinstance(text, str) or not normalize_text(text):
            raise ManualError(f"manual line {n}: text must be a non-empty string")
        relevant = obj.get("relevant")
        if (not isinstance(relevant, list) or not relevant
                or not all(isinstance(p, str) and p for p in relevant)):
            raise ManualError(f"manual line {n}: relevant must be a non-empty list of paths")
        folder, tags, fm = obj.get("folder"), obj.get("tags"), obj.get("frontmatter")
        if not _opt_str(folder):
            raise ManualError(f"manual line {n}: folder must be a string or null")
        if not (tags is None or (isinstance(tags, list) and all(isinstance(t, str) for t in tags))):
            raise ManualError(f"manual line {n}: tags must be a list of strings or null")
        if not (fm is None or isinstance(fm, dict)):
            raise ManualError(f"manual line {n}: frontmatter must be an object or null")
        cls = obj.get("class")
        if cls is not None and cls not in CLASSES:
            raise ManualError(f"manual line {n}: class must be one of {list(CLASSES)}")
        norm = normalize_text(text)
        filters = {"folder": folder, "tags": tags, "frontmatter": fm}
        out.append({
            "qid": query_id(obj["user_id"], norm, filters),
            "user_id": obj["user_id"],
            "text": norm,
            **filters,
            "relevant": list(relevant),
            "class": cls,
        })
    return out


def load_manual(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as fh:
        return parse_manual_lines(fh)
