"""`run`: replay every arm over the selected queries, as one generation (#356, D4).

Order, which is the atomicity contract:

1. Validate everything that can be validated without executing an arm: the
   generation id (an existing one is refused), the inputs, the selection, the
   arm names, a `runfile:`'s provenance, and a URL for database-backed arms.
   Any failure here is exit 64 and touches nothing.
2. Reserve `runs/<generation>/` with an exclusive `mkdir` (an existing or
   concurrently taken name is exit 64; the directory is kept after any
   failure), then atomically write `runs/manifest.json`, and the
   generation's own `_manifest.json` copy, with `status: "running"`. From this
   moment no earlier generation is the published one.
3. Open the read-only replay engine and assert it, read the replay corpus,
   build each factory, and ask every arm every selected query, sequentially.
   Each answer must be a `list` of `str`; it is deduplicated to first rank and
   truncated to the depth. Each arm's file is staged under
   `runs/<generation>/`.
4. Any exception from the engine, a factory or an arm, and any answer of the
   wrong type, aborts the whole run: there is no per-query catch, no "treat as
   empty" and no retry. The manifest is rewritten with `status: "failed"`, the
   failing arm and the error (the exception type and the arm label only; the
   message, which can quote query text, is printed to stderr), and `run`
   exits 1. Staged files stay on disk, unreferenced.
5. Only after every arm answered every selected query is the manifest
   rewritten with `status: "complete"` and each staged file's sha256.

Corpus loss: for each arm, a judged path the snapshot marks searchable under
the arm's membership but the replay corpus does not is recorded in
`meta.corpus_loss`; a judged path not searchable under it even in the
snapshot is `meta.unsearchable_before`. A `runfile:` arm carries the loss its
own `run` recorded on its own database.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from scripts.search_eval import EXIT_FAILURE, EXIT_OK, UsageError
from scripts.search_eval._io import atomic_write, ensure_private_dir, pretty_json
from scripts.search_eval.arms import ArmContext, EvalQuery, ResolvedArm, resolve_arms
from scripts.search_eval.corpus import Corpus, lost
from scripts.search_eval.metrics import dedup
from scripts.search_eval.scoring import (
    MANIFEST_FILE,
    RUNS_DIR,
    SNAPSHOT_FILE,
    DataDir,
    drift_drop,
    select_qids,
    selected_hash,
)

DEFAULT_DEPTH = 20
#: The generation's own copy of its manifest. An arm slug never starts with
#: `_`, so it cannot collide with a staged arm file.
GENERATION_MANIFEST_FILE = "_manifest.json"
_GENERATION = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class ArmOutputError(TypeError):
    """An arm answered with something other than a list of strings."""


@dataclass
class RunOutcome:
    exit_code: int
    manifest: dict


def _install_fixture_corpus(data_dir: Path, fixture: Path) -> None:
    """In fixture mode the fixture corpus is the snapshot (D7)."""
    if not fixture.is_file():
        raise UsageError(f"no fixture corpus at {fixture}")
    target = data_dir / SNAPSHOT_FILE
    if target.exists():
        if target.read_bytes() != fixture.read_bytes():
            raise UsageError(f"{target} exists and differs from the fixture corpus")
        return
    atomic_write(target, fixture.read_bytes())


def _eval_query(q: dict) -> EvalQuery:
    return EvalQuery(
        qid=q["qid"], user_id=q["user_id"], text=q["text"], folder=q["folder"],
        tags=q["tags"], frontmatter=q["frontmatter"], query_class=q["class"],
        sources=frozenset(q["sources"]),
    )


def _loss(arm_membership: str, selected: list[str], data: DataDir, judged: dict[str, set[str]],
          replay: Corpus) -> tuple[dict, dict]:
    loss: dict[str, list[str]] = {}
    before: dict[str, list[str]] = {}
    for qid in selected:
        owner = data.queries[qid]["user_id"]
        for p in sorted(judged[qid]):
            if not data.snapshot.member(owner, p, arm_membership):
                before.setdefault(qid, []).append(p)
            elif lost(data.snapshot, replay, owner, p, arm_membership):
                loss.setdefault(qid, []).append(p)
    return loss, before


async def run_generation(
    data_dir: Path,
    arm_specs: list[str],
    *,
    generation: str,
    depth: int = DEFAULT_DEPTH,
    limit_queries: int | None = None,
    database_url: str | None = None,
    fixture_corpus: Path | None = None,
    replay_corpus: Corpus | None = None,
    arm_options: dict[str, str] | None = None,
) -> RunOutcome:
    """Run one generation. `replay_corpus` overrides the replay corpus (tests only)."""
    data_dir = Path(data_dir)
    options = dict(arm_options or {})
    if not isinstance(generation, str) or not _GENERATION.fullmatch(generation):
        raise UsageError(f"invalid generation id {generation!r}")
    if depth < 1:
        raise UsageError("--depth must be >= 1")
    if limit_queries is not None and limit_queries < 1:
        raise UsageError("--limit-queries must be >= 1")
    if fixture_corpus is not None:
        _install_fixture_corpus(data_dir, Path(fixture_corpus))
    data = DataDir.load(data_dir)
    drift = drift_drop(data.queries, data.qrels, data.snapshot)
    selected = select_qids(drift, limit_queries)
    sel_hash = selected_hash(selected)
    arms = resolve_arms(list(arm_specs), qrels_sha256=data.qrels_sha256,
                        snapshot_sha256=data.snapshot_sha256, selected_qids_hash=sel_hash)
    # A factory may name arm options it cannot run without (#354:
    # `make_arm_alt` needs `hybrid_alt_k`). Checked before the generation is
    # reserved, so a missing option is a usage error (exit 64), not a failed
    # generation.
    for arm in arms:
        for key in getattr(arm.factory, "required_options", ()) or ():
            if key not in options:
                raise UsageError(f"arm {arm.spec!r} needs --arm-opt {key}=…")
    runs_dir = data_dir / RUNS_DIR
    gen_dir = runs_dir / generation
    needs_db = any(a.requires_db for a in arms)
    url = None
    if needs_db:
        from scripts.search_eval.db import resolve_url

        url = resolve_url(database_url)

    manifest = {
        "generation": generation,
        "status": "running",
        "started_by": "run",
        "selected_qids_hash": sel_hash,
        "selection": {"limit_queries": limit_queries},
        "qrels_sha256": data.qrels_sha256,
        "snapshot_sha256": data.snapshot_sha256,
        "arms": [a.spec for a in arms],
        "options": options,
        "depth": depth,
    }
    ensure_private_dir(runs_dir)
    # Reserve the generation atomically before anything can fail: an early
    # database or corpus failure, or a concurrent run with the same id, must
    # find the name taken. The directory is kept after every failure.
    try:
        gen_dir.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError as e:
        raise UsageError(f"generation {generation} already exists; choose another id") from e
    os.chmod(gen_dir, 0o700)
    manifest_path = runs_dir / MANIFEST_FILE

    def publish(m: dict) -> None:
        # The generation keeps its own copy; runs/manifest.json is the published one.
        atomic_write(gen_dir / GENERATION_MANIFEST_FILE, pretty_json(m))
        atomic_write(manifest_path, pretty_json(m))

    publish(manifest)

    engine = None
    current: ResolvedArm | None = None
    try:
        if needs_db:
            from scripts.search_eval.db import fetch_membership, open_readonly

            engine, maker = await open_readonly(url)
            async with maker() as session:
                replay = Corpus(await fetch_membership(session))
        else:
            maker = None
            replay = replay_corpus or (Corpus.load(fixture_corpus) if fixture_corpus else data.snapshot)
        ctx = ArmContext(session_factory=maker, corpus=replay, options=options)
        files: dict[str, dict] = {}
        for arm in arms:
            current = arm
            meta = {
                "arm": arm.spec, "label": arm.label, "membership": arm.membership,
                "options": options, "depth": depth, "generation": generation,
                "selected_qids_hash": sel_hash, "qrels_sha256": data.qrels_sha256,
                "snapshot_sha256": data.snapshot_sha256,
            }
            if arm.kind == "runfile":
                src_meta, src_runs = arm.runfile["meta"], arm.runfile["runs"]
                runs = {qid: dedup(src_runs[qid])[:depth] for qid in selected if qid in src_runs}
                meta.update({
                    "source": "runfile",
                    "source_generation": src_meta.get("generation"),
                    "options": src_meta.get("options") or {},
                    "corpus_loss": {k: v for k, v in (src_meta.get("corpus_loss") or {}).items()
                                    if k in selected},
                    "unsearchable_before": {
                        k: v for k, v in (src_meta.get("unsearchable_before") or {}).items()
                        if k in selected},
                    "missing_queries": [qid for qid in selected if qid not in src_runs],
                })
            else:
                fn = arm.factory(ctx)
                runs = {}
                for qid in selected:
                    q = data.queries[qid]
                    out = await fn(_eval_query(q), depth)
                    if not isinstance(out, list) or not all(isinstance(p, str) for p in out):
                        raise ArmOutputError(
                            f"arm {arm.label} returned {type(out).__name__}"
                            + ("" if not isinstance(out, list) else " with a non-string element")
                        )
                    runs[qid] = dedup(out)[:depth]
                loss, before = _loss(arm.membership, selected, data, drift.judged, replay)
                meta.update({"source": "replay", "corpus_loss": loss,
                             "unsearchable_before": before, "missing_queries": []})
            rel = f"{generation}/{arm.slug}.json"
            digest = atomic_write(runs_dir / rel, pretty_json({"meta": meta, "runs": runs}))
            files[arm.spec] = {"path": rel, "sha256": digest}
        current = None
        manifest = {**manifest, "status": "complete", "files": files}
        publish(manifest)
        return RunOutcome(EXIT_OK, manifest)
    except Exception as e:  # noqa: BLE001 - any failure fails the generation
        label = current.label if current is not None else None
        # The manifest records the exception type and the arm only (D4): an
        # exception message can quote query text. The full message goes to
        # the operator's terminal and nowhere else.
        print(f"search-eval: generation {generation} failed in arm {label}: "
              f"{type(e).__name__}: {e}", file=sys.stderr)
        manifest = {
            **manifest,
            "status": "failed",
            "failed_arm": label,
            "error": f"{type(e).__name__} in arm {label}" if label else type(e).__name__,
        }
        publish(manifest)
        return RunOutcome(EXIT_FAILURE, manifest)
    finally:
        if engine is not None:
            await engine.dispose()
