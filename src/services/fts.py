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

## The vector's shape (#344)

Since migration 031 a keyword vector is **``A(title) || A(aliases) ||
D(body)``**, per configured config, concatenated in the order *title parts for
every config, aliases parts for every config, content parts for every config*.
The content part is byte-identical to the pre-031 fragment, and ``to_tsvector``
assigns weight ``D`` to every lexeme, so a pre-031 vector is exactly the body
part of a post-031 one. That is what lets :func:`rehead_tsvector_sql` replace a
vector's head from stored columns — ``head || ts_filter(content_tsvector,
'{d}')`` — with no file access: migration 031 uses it over every row, and both
move paths use it because the title is derived from the path.

What ``rehead`` preserves, measured on PostgreSQL 16: the body's lexemes, their
weights and their **relative** positions — ``@@``, the phrase operators and
``ts_rank_cd`` answer identically. What it does not preserve: the body's
*absolute* positions, which ``ts_filter`` keeps as the previous concatenation
assigned them, so each application shifts the body by the previous head's
length. The first application over a D-only vector is byte-exact; repeated
applications drift, which is why 031 skips a row that already carries a weight
``A`` lexeme and why the move paths are specified on matching and ranking, not
bytes (design D1/D6, L11).

Ranking uses :func:`rank_expression`: ``ts_rank_cd(RANK_WEIGHTS, vector,
query)`` with the weight array bound as ``float4[]``. At the default
``{0.02, 0.2, 0.4, 1.0}`` one adjacent two-word title cover (``2/(1+1) = 1.0``)
outweighs fifty body covers (``2/(50+50) = 0.02`` each); PostgreSQL's default
``D = 0.1`` lets ten body repetitions tie the title, which is the issue.

## The disjunctive fallback (#344, design D3)

``websearch_to_tsquery`` ANDs bare terms. When the conjunctive statement
under-fills the page, the service asks PostgreSQL for the **text form** of each
config's parse and :func:`parse_conjunction` reads PostgreSQL's own output
grammar — never the caller's string. A *plain conjunction* has no ``|`` and no
parenthesis and splits on ``&`` into units that are a negated lexeme or a phrase
chain; a config whose parse is a plain conjunction with at least two positive
units is *eligible*, and :func:`fallback_plan` gives each config either its OR
form (``(u1 | … | uk) & !n1 …``, built from the verbatim tokens and cast with
the ``tsquery`` input function, which does not re-stem) or its own unchanged
AND expression. The OR over configs of those forms is implied by the OR over
configs of the AND parses — a superset by construction, whatever mix of
eligible and ineligible configs a query produces.
"""
import math
import re
from dataclasses import dataclass, field
from functools import reduce

from sqlalchemy import bindparam, cast, func, text
from sqlalchemy.dialects.postgresql import ARRAY, REAL, TSQUERY

from src.config import settings

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

#: The joined aliases text bound to the vector is cut here. The head must stay
#: small against the 1 MiB tsvector limit, because the halving retreat in
#: `write_tsvector_bounded` only ever shrinks the **body** bind.
FTS_ALIASES_MAX_CHARS = 2048

#: `matched_terms` is computed per config only when that config's plain
#: conjunction has at most this many positive units — one `@@` per unit per
#: page row, so the bound keeps a pathological query from turning the snippet
#: statement into thousands of operator evaluations.
MATCHED_TERMS_MAX_UNITS = 32

#: `ts_headline` sentinels. U+0002 / U+0003 are removed from every stored
#: excerpt and alias text (`strip_sentinels`), so their presence in a headline
#: means "PostgreSQL highlighted something" and nothing else. Rendered as
#: `«` / `»` by the tool.
HEADLINE_START = "\x02"
HEADLINE_STOP = "\x03"

#: Fragment mode: the highlighted snippet. With no cover PostgreSQL returns only
#: `MinWords` words here (measured), which is why the head has its own options.
HEADLINE_OPTIONS = (
    "MaxFragments=2, MaxWords=18, MinWords=6, "
    f"StartSel={HEADLINE_START}, StopSel={HEADLINE_STOP}, "
    'FragmentDelimiter=" … "'
)

#: Head mode: the excerpt's leading words for a row whose body part did not
#: match (a title/alias-only or split match) or whose excerpt held no
#: highlight (a match beyond the stored excerpt). `MaxFragments=0` with no
#: cover returns the document's first `MinWords` words (measured: eighteen).
#: PostgreSQL still marks single query words inside those words, so the head
#: carries the same sentinels as the fragment mode and the service strips
#: them (`strip_sentinels`) — never PostgreSQL's default `<b>…</b>`, which a
#: note may contain literally (#344, V-B2).
HEAD_OPTIONS = (
    "MaxFragments=0, MinWords=18, MaxWords=24, "
    f"StartSel={HEADLINE_START}, StopSel={HEADLINE_STOP}"
)

_SENTINEL_RE = re.compile("[\x00\x02\x03]")


def strip_sentinels(value: str) -> str:
    """`value` with U+0000, U+0002 and U+0003 removed.

    NUL for the database's sake (#308, D1); the other two so the headline
    sentinels cannot be forged from note text (L7).
    """
    return _SENTINEL_RE.sub("", value)


# --------------------------------------------------------------------------
# the vector builders
# --------------------------------------------------------------------------


def _configs(configs: list[str] | None) -> list[str]:
    """`configs` when given (migration 031 builds under the configs the stored
    fingerprint names, not the environment's), else the running setting —
    **sorted** either way. The fingerprint stores its configs sorted, so 031
    and the writers build the same bytes whatever order `FTS_CONFIGS` lists
    them in (#344, V-N4). The query side keeps the setting's order: a tsquery
    OR is order-free, and `fts_configs[0]` there is the head's config."""
    return sorted(configs if configs is not None else settings.fts_configs)


def _cfg_params(configs: list[str] | None = None) -> dict:
    return {f"fts_cfg_{i}": cfg for i, cfg in enumerate(_configs(configs))}


def _per_config(template: str, configs: list[str] | None = None) -> list[str]:
    return [
        template.format(cfg=f"CAST(:fts_cfg_{i} AS regconfig)")
        for i in range(len(_configs(configs)))
    ]


def head_tsvector_sql(
    title_bind: str = "title",
    aliases_bind: str = "aliases",
    configs: list[str] | None = None,
) -> tuple[str, dict]:
    """The weight-`A` head: title parts for every config, then aliases parts.

    Returns ``(fragment, params)`` like :func:`index_tsvector_sql`; the caller
    binds the title under ``title_bind`` and the joined aliases text under
    ``aliases_bind`` (``aliases_text``).
    """
    parts = _per_config(
        f"setweight(to_tsvector({{cfg}}, :{title_bind}), 'A')", configs
    ) + _per_config(
        f"setweight(to_tsvector({{cfg}}, :{aliases_bind}), 'A')", configs
    )
    return " || ".join(parts), _cfg_params(configs)


def body_tsvector_sql(
    content_bind: str = "content", configs: list[str] | None = None
) -> tuple[str, dict]:
    """The weight-`D` body — the pre-031 fragment, byte for byte, for one
    config (or configs listed in sorted order; the builders sort, see
    :func:`_configs`).

    Example (two configs)::

        to_tsvector(CAST(:fts_cfg_0 AS regconfig), :content)
          || to_tsvector(CAST(:fts_cfg_1 AS regconfig), :content)
    """
    parts = _per_config(f"to_tsvector({{cfg}}, :{content_bind})", configs)
    return " || ".join(parts), _cfg_params(configs)


def index_tsvector_sql(
    content_bind: str = "content",
    title_bind: str = "title",
    aliases_bind: str = "aliases",
    configs: list[str] | None = None,
) -> tuple[str, dict]:
    """Build the SQL fragment + bind params for a full ``content_tsvector``.

    Returns ``(fragment, params)`` where ``fragment`` is
    ``head || body`` — :func:`head_tsvector_sql` followed by
    :func:`body_tsvector_sql` — suitable for embedding in a larger ``UPDATE
    ... SET content_tsvector = <fragment>`` statement, and ``params`` are the
    config-name bind values to merge into the statement's parameter dict.

    The caller supplies the title, the aliases text and the body under the
    three bind names; this helper only varies *which configs* parse them.
    Every writer of ``content_tsvector`` goes through this function, the
    rebuild included, so the stored shape is one shape.
    """
    head, params = head_tsvector_sql(title_bind, aliases_bind, configs)
    body, _ = body_tsvector_sql(content_bind, configs)
    return f"{head} || {body}", params


def rehead_tsvector_sql(
    title_bind: str = "title",
    aliases_bind: str = "aliases",
    configs: list[str] | None = None,
) -> tuple[str, dict]:
    """Replace a stored vector's head from stored columns, no body needed.

    ``head || ts_filter(content_tsvector, '{d}')``: the filter keeps the
    weight-`D` body (lexemes, weights, relative positions) and drops any
    existing head. See the module docstring for what it does and does not
    preserve, and why 031 guards it with a skip predicate.
    """
    head, params = head_tsvector_sql(title_bind, aliases_bind, configs)
    return f"{head} || ts_filter(content_tsvector, '{{d}}')", params


# --------------------------------------------------------------------------
# the aliases rule
# --------------------------------------------------------------------------


def _render_alias_scalar(value) -> str | None:
    """One alias as text, by the shared title rule's scalar rendering.

    `str()` of the value, except that a non-finite float takes its canonical
    YAML token (`.nan` / `.inf` / `-.inf`, #154) — the same token the JSONB
    sanitiser stores, so a freshly parsed mapping and the stored JSONB render
    the same alias. Nested lists and mappings are not aliases and yield None.
    """
    if value is None or isinstance(value, (list, dict)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return ".nan"
        return ".inf" if value > 0 else "-.inf"
    return str(value)


def note_aliases(frontmatter: dict | None) -> list[str]:
    """The note's aliases, by the one rule every keyword-vector writer uses.

    Reads `aliases` then `alias` (Obsidian's documented key and the legacy
    spelling), keeping the first occurrence of each alias. A list contributes
    each scalar item; a scalar string is comma-split and stripped exactly as
    `vault.extract_tags` treats a scalar `tags` string; any other scalar is
    rendered the same way. U+0000, U+0002 and U+0003 are removed and empty
    strings dropped. Total over a parsed mapping and over the stored sanitised
    JSONB alike, and the same list from either for the same note.
    """
    out: list[str] = []
    seen: set[str] = set()

    def _add(raw) -> None:
        rendered = _render_alias_scalar(raw)
        if rendered is None:
            return
        for piece in (rendered.split(",") if isinstance(raw, str) else [rendered]):
            alias = strip_sentinels(piece).strip()
            if alias and alias not in seen:
                seen.add(alias)
                out.append(alias)

    for key in ("aliases", "alias"):
        value = (frontmatter or {}).get(key)
        if isinstance(value, list):
            for item in value:
                _add(item)
        elif value is not None and not isinstance(value, dict):
            _add(value)
    return out


def aliases_text(frontmatter: dict | None) -> str:
    """The aliases joined for the vector's `:aliases` bind, cut at
    `FTS_ALIASES_MAX_CHARS` characters."""
    return "\n".join(note_aliases(frontmatter))[:FTS_ALIASES_MAX_CHARS]


def excerpt_of(content: str) -> str:
    """The stored `body_excerpt` for a body the vector was built from: its
    first `KEYWORD_EXCERPT_CHARS` characters, sentinel-free (design D4)."""
    return strip_sentinels(content[: settings.keyword_excerpt_chars])


# --------------------------------------------------------------------------
# the query side
# --------------------------------------------------------------------------


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


def rank_weights_param():
    """`RANK_WEIGHTS` as a bound `float4[]` — a value, never interpolated."""
    return bindparam(
        "rank_weights", value=list(settings.rank_weights), type_=ARRAY(REAL)
    )


def rank_expression(vector_column, tsquery):
    """``ts_rank_cd(RANK_WEIGHTS, vector, query)`` for both ranking blocks."""
    return func.ts_rank_cd(rank_weights_param(), vector_column, tsquery)


def tsquery_text_sql() -> str:
    """One row per configured config: the text form of its websearch parse.

    No table access; the only caller-derived input is the same ``:q`` bind the
    conjunctive statement uses. Rows come back in config order.
    """
    selects = [
        f"SELECT {i} AS ord, CAST(websearch_to_tsquery(CAST(:fts_cfg_{i} AS regconfig), :q) AS text) AS parsed"
        for i in range(len(settings.fts_configs))
    ]
    return " UNION ALL ".join(selects) + " ORDER BY ord"


# -- PostgreSQL's tsquery output grammar ---------------------------------------

_LEXEME = "lexeme"
_OP = "op"

#: `<->` (adjacent) or `<N>` (N positions apart, a stop-word gap).
_PHRASE_RE = re.compile(r"<(-|\d+)>")
_SUFFIX_RE = re.compile(r":[A-D*]+")


def _tokenize(tsquery_text: str) -> list[tuple[str, str]] | None:
    """Tokens of PostgreSQL's ``tsquery::text`` output, or None if a character
    outside that grammar appears.

    A lexeme is single-quoted; inside, ``''`` is an embedded quote and ``\\``
    escapes the next character (PostgreSQL doubles both on output). The token
    is kept **verbatim**, quotes and escapes included, because it is passed
    straight back to the ``tsquery`` input function. An optional ``:*`` /
    weight suffix is kept with it (``websearch_to_tsquery`` never emits one,
    measured, but the input grammar admits it).
    """
    tokens: list[tuple[str, str]] = []
    i, n = 0, len(tsquery_text)
    while i < n:
        ch = tsquery_text[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "'":
            j = i + 1
            while j < n:
                c = tsquery_text[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "'":
                    if j + 1 < n and tsquery_text[j + 1] == "'":
                        j += 2
                        continue
                    break
                j += 1
            if j >= n:
                return None  # unterminated quote
            j += 1
            suffix = _SUFFIX_RE.match(tsquery_text, j)
            if suffix:
                j = suffix.end()
            tokens.append((_LEXEME, tsquery_text[i:j]))
            i = j
            continue
        if ch in "&|!()":
            tokens.append((_OP, ch))
            i += 1
            continue
        if ch == "<":
            m = _PHRASE_RE.match(tsquery_text, i)
            if not m:
                return None
            tokens.append((_OP, m.group(0)))
            i = m.end()
            continue
        return None
    return tokens


@dataclass(frozen=True)
class Conjunction:
    """A plain conjunction: positive units (a lexeme or a phrase chain, as
    verbatim tsquery text) and negated lexemes (verbatim)."""

    positives: tuple[str, ...] = field(default_factory=tuple)
    negations: tuple[str, ...] = field(default_factory=tuple)


def parse_conjunction(tsquery_text: str | None) -> Conjunction | None:
    """PostgreSQL's ``tsquery::text`` as a plain conjunction, or None.

    None for: an empty parse (stop words only), any ``|`` (the user wrote
    ``OR``), any parenthesis (a negated phrase, ``-"a b"``), a ``!`` not
    followed by exactly one lexeme, two lexemes without an operator between
    them, or any character outside the output grammar. The positive units are
    kept as verbatim text (``'beta' <-> 'gamma'``) so the fallback's stems are
    exactly the AND parse's.
    """
    if not tsquery_text or not tsquery_text.strip():
        return None
    tokens = _tokenize(tsquery_text)
    if not tokens:
        return None
    if any(kind == _OP and value in "|()" for kind, value in tokens):
        return None
    units: list[list[tuple[str, str]]] = [[]]
    for tok in tokens:
        if tok == (_OP, "&"):
            units.append([])
        else:
            units[-1].append(tok)
    positives: list[str] = []
    negations: list[str] = []
    for unit in units:
        if not unit:
            return None
        if unit[0] == (_OP, "!"):
            if len(unit) != 2 or unit[1][0] != _LEXEME:
                return None
            negations.append(unit[1][1])
            continue
        # A phrase chain: LEX (PHRASE_OP LEX)*
        expect_lexeme = True
        for kind, value in unit:
            if expect_lexeme:
                if kind != _LEXEME:
                    return None
            else:
                if kind != _OP or not _PHRASE_RE.fullmatch(value):
                    return None
            expect_lexeme = not expect_lexeme
        if expect_lexeme:  # ended on an operator
            return None
        positives.append(" ".join(value for _kind, value in unit))
    return Conjunction(tuple(positives), tuple(negations))


def fallback_tsquery_text(conj: Conjunction) -> str | None:
    """``(u1 | … | uk) & !n1 & … & !nm`` for an eligible conjunction, else None.

    Eligible means at least two positive units: with one, the OR *is* the AND.
    Built from the verbatim unit tokens, so casting the text with the
    ``tsquery`` input function round-trips the lexemes without re-stemming.
    """
    if len(conj.positives) < 2:
        return None
    body = "(" + " | ".join(conj.positives) + ")"
    for neg in conj.negations:
        body += f" & !{neg}"
    return body


@dataclass(frozen=True)
class FallbackPlan:
    """Per-config fallback decision (design D3)."""

    #: Per config: the OR-form text for an eligible config, else None (that
    #: config keeps its own `websearch_to_tsquery` expression).
    forms: tuple[str | None, ...]
    #: Per config: the parsed conjunction when the parse was one (any unit
    #: count), for `matched_terms`; None otherwise.
    conjunctions: tuple[Conjunction | None, ...]

    @property
    def eligible(self) -> bool:
        return any(form is not None for form in self.forms)


def fallback_plan(parsed_texts: list[str | None]) -> FallbackPlan:
    """Decide the fallback per config from the per-config parse texts."""
    forms: list[str | None] = []
    conjs: list[Conjunction | None] = []
    for parsed in parsed_texts:
        conj = parse_conjunction(parsed)
        conjs.append(conj)
        forms.append(fallback_tsquery_text(conj) if conj is not None else None)
    return FallbackPlan(tuple(forms), tuple(conjs))


def fallback_tsquery(plan: FallbackPlan, query: str):
    """The combined fallback tsquery expression: per config, the eligible OR
    form cast with the ``tsquery`` input function, or that config's own
    ``websearch_to_tsquery`` expression retained; OR'd across configs."""
    parts = []
    for i, (cfg, form) in enumerate(zip(settings.fts_configs, plan.forms)):
        if form is None:
            parts.append(func.websearch_to_tsquery(cfg, query))
        else:
            parts.append(cast(bindparam(f"fallback_{i}", value=form), TSQUERY))
    return reduce(lambda a, b: a.op("||")(b), parts)


def matched_terms_units(plan: FallbackPlan) -> list[tuple[int, tuple[str, ...]]]:
    """``(config index, positive units)`` for every config whose parse is a
    plain conjunction of 1..MATCHED_TERMS_MAX_UNITS positive units."""
    out = []
    for i, conj in enumerate(plan.conjunctions):
        if conj is not None and 1 <= len(conj.positives) <= MATCHED_TERMS_MAX_UNITS:
            out.append((i, conj.positives))
    return out


def best_matched_terms(pairs: list[tuple[int, int]]) -> tuple[int, int] | None:
    """The rendered `matched_terms` pair from per-config ``(matched, total)``
    pairs: the greatest ratio, ties broken by the greater total, then by
    config order (the list's order)."""
    best: tuple[int, int] | None = None
    best_key = None
    for matched, total in pairs:
        if total <= 0:
            continue
        key = (matched / total, total)
        if best_key is None or key > best_key:
            best, best_key = (matched, total), key
    return best


# --------------------------------------------------------------------------
# startup validation
# --------------------------------------------------------------------------


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
