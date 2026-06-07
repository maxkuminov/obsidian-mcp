"""Single source of truth for the full-text-search (keyword) configuration.

PostgreSQL full-text search parses text into lexemes using a *text-search
configuration* (a stemmer + stop-word dictionary). ``'english'`` applies the
English Snowball stemmer; ``'simple'`` does neither (just lowercases and
tokenizes, preserving exact word forms); ``'norwegian'`` applies the Norwegian
stemmer, and so on.

The configuration used at index time (building ``content_tsvector``) and at
query time (building the ``tsquery``) must agree, or stems won't align. To keep
the two from drifting, both the indexer and the search service build their SQL
through the helpers here, driven by the single ``settings.fts_configs`` list.

Multiple configs compose cleanly:

* ``tsvector || tsvector`` concatenates lexeme sets (duplicates merge), so a
  note is indexed under *every* configured config.
* ``tsquery || tsquery`` is a logical OR, so a query matches if *any* config's
  parse of it hits.

A single-element list reproduces the historical single-call behavior exactly.

Config names originate from the environment, never from end users, but are
still passed as bound parameters (index side: bound param + ``::regconfig``
cast; query side: bound argument to ``websearch_to_tsquery``) and never
string-interpolated. The startup allowlist check in :func:`validate_fts_configs`
exists for clear error messages, not as the primary injection defense.
"""
from functools import reduce

from sqlalchemy import func, text

from src.config import settings


def index_tsvector_sql(content_bind: str = "content") -> tuple[str, dict]:
    """Build the SQL fragment + bind params for ``content_tsvector``.

    Returns ``(fragment, params)`` where ``fragment`` is a ``to_tsvector(...)``
    expression (concatenated with ``||`` across every configured FTS config)
    suitable for embedding in a larger ``UPDATE ... SET content_tsvector =
    <fragment>`` statement, and ``params`` are the config-name bind values to
    merge into the statement's parameter dict.

    The caller supplies the body text under the ``content_bind`` parameter name
    (default ``"content"``); this helper only varies *which configs* parse it.

    Example (two configs)::

        to_tsvector(CAST(:fts_cfg_0 AS regconfig), :content)
          || to_tsvector(CAST(:fts_cfg_1 AS regconfig), :content)
    """
    cfgs = settings.fts_configs
    frag = " || ".join(
        f"to_tsvector(CAST(:fts_cfg_{i} AS regconfig), :{content_bind})"
        for i in range(len(cfgs))
    )
    params = {f"fts_cfg_{i}": cfg for i, cfg in enumerate(cfgs)}
    return frag, params


def combined_tsquery(query: str):
    """Return a SQLAlchemy expression OR-ing ``websearch_to_tsquery`` over every
    configured FTS config.

    For a single config this is identical to a bare
    ``func.websearch_to_tsquery(cfg, query)`` call. Use the same expression for
    both the ``ts_rank_cd`` rank and the ``@@`` match so ranking and matching
    stay consistent.
    """
    parts = [func.websearch_to_tsquery(cfg, query) for cfg in settings.fts_configs]
    return reduce(lambda a, b: a.op("||")(b), parts)


async def validate_fts_configs(session) -> None:
    """Fail fast if any configured FTS config is not installed in this Postgres
    instance.

    A typo'd or missing config name would otherwise produce silent zero-result
    keyword searches (the ``::regconfig`` cast errors only when the query
    actually runs). Called during startup alongside the embedding-dimension
    guard so the failure surfaces immediately with a helpful message.
    """
    rows = await session.execute(text("SELECT cfgname FROM pg_ts_config"))
    available = {r[0] for r in rows}
    missing = [c for c in settings.fts_configs if c not in available]
    if missing:
        raise SystemExit(
            f"FTS_CONFIGS contains unknown text-search config(s): {missing}. "
            f"Available: {sorted(available)}"
        )
