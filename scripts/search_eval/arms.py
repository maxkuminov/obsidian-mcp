"""The arm seam (#356, design D4).

An *arm* is an async callable `(EvalQuery, depth) -> list[str]`: ranked vault
paths, best first. A factory builds one from an `ArmContext`. `--arm` names
resolve in this order:

1. built-ins: `keyword` (`src.services.search.full_text_search`), `semantic`
   (`src.services.embeddings.semantic_search`), and the fixture-only
   `fixture-lexical` / `fixture-title`, which need no session;
2. `runfile:<path>`: an arm file this harness's own `run` step staged in a
   **complete** generation on another database (e.g. a re-embedded copy);
3. `<module>:<factory>`: imported with `importlib`. A database-backed factory
   (the default; set `factory.requires_db = False` otherwise) must declare
   `factory.membership`, one of `metadata`, `keyword`, `semantic`,
   `keyword_and_semantic` or `keyword_or_semantic` (see `corpus.py`), or the
   run exits 64. A factory may also declare `factory.required_options`, arm
   option names the run refuses (exit 64) to start without.

The built-in database arms import the search services lazily, inside the
factory, and open one fresh session per query from the read-only
`session_factory` (`full_text_search` sets its own snapshot transaction). They
call the services directly, never through `_tracked`, so replay records no
telemetry and writes nothing. An imported arm runs with whatever the replay URL
permits: the disposable restored copy is what protects data.
"""
from __future__ import annotations

import importlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable

from scripts.search_eval import UsageError
from scripts.search_eval._io import sha256_file
from scripts.search_eval.corpus import MEMBERSHIPS, Corpus


@dataclass(frozen=True)
class EvalQuery:
    qid: str
    user_id: int | None
    text: str
    folder: str | None
    tags: list[str] | None
    frontmatter: dict | None
    query_class: str          # "identifier" | "prose"
    sources: frozenset[str]   # subset of {"mined", "manual"}; both after a collision


Arm = Callable[[EvalQuery, int], Awaitable[list[str]]]


@dataclass(frozen=True)
class ArmContext:
    session_factory: Any | None      # read-only async_sessionmaker (D3); None in fixture mode
    corpus: Corpus                   # the replay corpus
    options: dict[str, str] = field(default_factory=dict)


ArmFactory = Callable[[ArmContext], Arm]


# --------------------------------------------------------------------------
# Built-in database arms.
# --------------------------------------------------------------------------


def keyword_arm(ctx: ArmContext) -> Arm:
    from src.services.search import full_text_search

    async def arm(q: EvalQuery, depth: int) -> list[str]:
        async with ctx.session_factory() as session:
            rows = await full_text_search(
                session, q.text, folder=q.folder, limit=depth, tags=q.tags,
                frontmatter=q.frontmatter, user_id=q.user_id,
            )
        return [r["path"] for r in rows]

    return arm


keyword_arm.membership = "keyword"
keyword_arm.requires_db = True


def semantic_arm(ctx: ArmContext) -> Arm:
    from src.services.embeddings import semantic_search

    async def arm(q: EvalQuery, depth: int) -> list[str]:
        async with ctx.session_factory() as session:
            rows = await semantic_search(
                session, q.text, limit=depth, folder=q.folder, tags=q.tags,
                frontmatter=q.frontmatter, user_id=q.user_id,
            )
        return [r["path"] for r in rows]

    return arm


semantic_arm.membership = "semantic"
semantic_arm.requires_db = True


# --------------------------------------------------------------------------
# Fixture arms (D7): deterministic, over ArmContext.corpus, no session.
# --------------------------------------------------------------------------

_TERM = re.compile(r"\w+")


def _terms(text: str) -> set[str]:
    return {t.lower() for t in _TERM.findall(text or "")}


def _overlap_arm(ctx: ArmContext, fields: tuple[str, ...]) -> Arm:
    async def arm(q: EvalQuery, depth: int) -> list[str]:
        wanted = _terms(q.text)
        scored = []
        for path, entry in ctx.corpus.owner_entries(q.user_id).items():
            if q.folder and not path.startswith(q.folder.rstrip("/") + "/"):
                continue
            have: set[str] = set()
            for f in fields:
                have |= _terms(entry.get(f, ""))
            score = len(wanted & have)
            if score:
                scored.append((-score, path))
        scored.sort()
        return [p for _, p in scored[:depth]]

    return arm


def fixture_lexical_arm(ctx: ArmContext) -> Arm:
    """Term overlap over title and body; ties broken by path."""
    return _overlap_arm(ctx, ("title", "body"))


def fixture_title_arm(ctx: ArmContext) -> Arm:
    """Term overlap over the title only; ties broken by path."""
    return _overlap_arm(ctx, ("title",))


for _f in (fixture_lexical_arm, fixture_title_arm):
    _f.membership = "metadata"
    _f.requires_db = False

BUILTIN_ARMS: dict[str, ArmFactory] = {
    "keyword": keyword_arm,
    "semantic": semantic_arm,
    "fixture-lexical": fixture_lexical_arm,
    "fixture-title": fixture_title_arm,
}


# --------------------------------------------------------------------------
# Resolution.
# --------------------------------------------------------------------------


@dataclass
class ResolvedArm:
    spec: str                 # exactly what was passed to --arm
    label: str                # the name the report uses (no filesystem path)
    kind: str                 # builtin | import | runfile
    membership: str
    requires_db: bool
    factory: ArmFactory | None = None
    runfile: dict | None = None
    slug: str = ""


def _slug(label: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_")[:80] or "arm"


def _factory_attrs(spec: str, factory: Any) -> tuple[str, bool]:
    requires_db = bool(getattr(factory, "requires_db", True))
    membership = getattr(factory, "membership", None)
    if membership is None and not requires_db:
        membership = "metadata"
    if membership not in MEMBERSHIPS:
        raise UsageError(
            f"arm {spec!r} must declare factory.membership as one of {list(MEMBERSHIPS)}"
        )
    return membership, requires_db


def _load_runfile(spec: str, path: Path, *, qrels_sha256: str, snapshot_sha256: str,
                  selected_qids_hash: str) -> dict:
    """Validate a `runfile:` arm against its producing generation and the current data."""
    if not path.is_file():
        raise UsageError(f"{spec}: no such run file")
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
        meta, runs = content["meta"], content["runs"]
    except (ValueError, KeyError, TypeError) as e:
        raise UsageError(f"{spec}: not a staged arm file") from e
    if not isinstance(meta, dict) or not isinstance(runs, dict) or not all(
        isinstance(v, list) and all(isinstance(p, str) for p in v) for v in runs.values()
    ):
        raise UsageError(f"{spec}: not a staged arm file")
    manifest_path = path.parent.parent / "manifest.json"
    if not manifest_path.is_file():
        raise UsageError(f"{spec}: no manifest.json beside its generation")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except ValueError as e:
        raise UsageError(f"{spec}: unreadable manifest") from e
    if manifest.get("status") != "complete":
        raise UsageError(f"{spec}: its generation is not complete ({manifest.get('status')!r})")
    if manifest.get("generation") != path.parent.name or meta.get("generation") != path.parent.name:
        raise UsageError(f"{spec}: the manifest does not publish this file's generation")
    digest = sha256_file(path)
    listed = [
        f for f in (manifest.get("files") or {}).values()
        if isinstance(f, dict) and isinstance(f.get("path"), str)
        and (manifest_path.parent / f["path"]).resolve() == path.resolve()
    ]
    if not listed or listed[0].get("sha256") != digest:
        raise UsageError(f"{spec}: the file is not listed with its sha256 in a complete manifest")
    for key, current in (("qrels_sha256", qrels_sha256), ("snapshot_sha256", snapshot_sha256),
                         ("selected_qids_hash", selected_qids_hash)):
        if meta.get(key) != current or manifest.get(key) != current:
            raise UsageError(f"{spec}: {key} differs from the current data directory")
    if meta.get("membership") not in MEMBERSHIPS:
        raise UsageError(f"{spec}: the file declares no membership")
    return content


def resolve_arm(spec: str, *, qrels_sha256: str, snapshot_sha256: str,
                selected_qids_hash: str) -> ResolvedArm:
    if spec in BUILTIN_ARMS:
        factory = BUILTIN_ARMS[spec]
        membership, requires_db = _factory_attrs(spec, factory)
        return ResolvedArm(spec, spec, "builtin", membership, requires_db, factory=factory)
    if spec.startswith("runfile:"):
        content = _load_runfile(spec, Path(spec[len("runfile:"):]), qrels_sha256=qrels_sha256,
                                snapshot_sha256=snapshot_sha256,
                                selected_qids_hash=selected_qids_hash)
        meta = content["meta"]
        label = f"runfile:{meta.get('label') or meta.get('arm')}@{meta['generation']}"
        return ResolvedArm(spec, label, "runfile", meta["membership"], False, runfile=content)
    if ":" in spec:
        module_name, _, attr = spec.partition(":")
        try:
            module = importlib.import_module(module_name)
            factory = getattr(module, attr)
        except (ImportError, AttributeError) as e:
            raise UsageError(f"cannot import arm {spec!r}: {type(e).__name__}") from e
        if not callable(factory):
            raise UsageError(f"arm {spec!r} is not callable")
        membership, requires_db = _factory_attrs(spec, factory)
        return ResolvedArm(spec, spec, "import", membership, requires_db, factory=factory)
    raise UsageError(f"unknown arm {spec!r} (built-ins: {', '.join(BUILTIN_ARMS)})")


def resolve_arms(specs: list[str], **hashes) -> list[ResolvedArm]:
    if not specs:
        raise UsageError("at least one --arm is required")
    if len(set(specs)) != len(specs):
        raise UsageError("an arm is named twice")
    arms = [resolve_arm(s, **hashes) for s in specs]
    seen_labels: dict[str, int] = {}
    seen_slugs: set[str] = set()
    for arm in arms:
        n = seen_labels.get(arm.label, 0)
        seen_labels[arm.label] = n + 1
        if n:
            arm.label = f"{arm.label}#{n + 1}"
        slug = _slug(arm.label)
        base, i = slug, 2
        while slug in seen_slugs:
            slug, i = f"{base}-{i}", i + 1
        seen_slugs.add(slug)
        arm.slug = slug
    return arms


def parse_arm_options(pairs: list[str] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    for pair in pairs or []:
        key, sep, value = pair.partition("=")
        if not sep or not key:
            raise UsageError(f"--arm-opt must be key=value (got {pair!r})")
        out[key] = value
    return out
