import ipaddress
import logging
import resource
import sys
from typing import Annotated, Any, Literal
from urllib.parse import urlparse

from pydantic import Field, PrivateAttr, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import BaseSettings, NoDecode, PydanticBaseSettingsSource


# Hostnames that can only ever name this machine. Used by the sandbox guard
# below, which must fail *closed*: anything this cannot prove is loopback —
# a name it does not recognise, a `*` wildcard that matches every name — is
# treated as public, because the cost of a false positive is a refused boot
# and the cost of a false negative is an unauthenticated vault.
_LOOPBACK_HOSTNAMES = frozenset({"localhost", "::1", "0:0:0:0:0:0:0:1"})


def _is_loopback_host(host: str) -> bool:
    """True when `host` names only this machine (localhost, 127/8, ::1)."""
    name = (host or "").strip().lower().rstrip(".")
    if not name or "*" in name:
        return False
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name in _LOOPBACK_HOSTNAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


# Maximum size of a single note, in bytes. Lives here (rather than in
# `src/mcp_server/tools.py`, which imports it) so the derived transport limit
# below can reference it without a circular import.
MAX_NOTE_BYTES = 10 * 1024 * 1024  # 10 MB

# Links extracted and persisted for a single note. Unbounded, one 10 MiB note
# of `[[a]] ` yields 1.75 M `ExtractedLink` objects — an 802 MiB peak against
# a 2 GB container, multiplied by every such note in one index pass. The cap
# is applied in DOCUMENT order (see `extract_links_bounded`) so a note does
# not lose one whole link kind, and a capped note is a *declared* degradation:
# the first N rows are kept, `notes_metadata.links_truncated` is set, and
# `get_links` says `truncated: true`. 10,000 outgoing links is far beyond any
# real note, including a generated MOC.
MAX_LINKS_PER_NOTE = 10_000

# Chunks embedded and stored for a single note. Unbounded, the arithmetic is
# `MAX_NOTE_BYTES` (10 MiB) ÷ `CHUNK_SIZE` (512 tokens, ~4 characters each) ≈
# 5,120 chunks for one legal note, each of them one sequential, 30 s-bounded
# provider call — and the embed backlog has no LIMIT, so re-editing one such
# note keeps every later tenant's notes out of the index indefinitely.
#
# 1,000 chunks is ~2 MB of cleaned text, far beyond any real note. The cap is
# applied in DOCUMENT order (see `chunk_text_bounded`) so a note keeps its head
# rather than an arbitrary window, and a capped note is a *declared*
# degradation: the first N chunks are embedded, the note **is** certified (an
# uncertified note is re-selected by the backlog for ever, which is #127's
# permanent burn), `notes_metadata.chunks_truncated` is set, one ERROR line is
# logged after the certifying commit, and both vector paths say
# `embedding_truncated: true`.
#
# **Accepted worst case, written down rather than discovered:** this cap times
# the provider's per-call bound is one note's embedding time — 1,000 × 30 s ≈
# 8.3 hours on a provider answering every call at the very edge of its timeout
# — and that is the delay one tenant's last note can add to the next tenant,
# because the per-user budget is evaluated only *between* notes and never
# preempts one that has started. It is a pathological-provider figure, not a
# steady-state one, and the alternative is an aggregate deadline, which is
# exactly the construct #127 removed because it produced a note the pass could
# never finish. Lowering the cap trades that bound against how much of a large
# note is semantically searchable at all.
#
# In the embedding fingerprint (`src/services/index_state.py`): changing it
# changes what a note's stored vector set *is*, so a change is a declared reset
# rather than a silent under-embedding.
MAX_CHUNKS_PER_NOTE = 1_000

# Characters of a provider exception's message carried on an `EmbedNoteFailure`.
# Truncated at capture, not at the run row: `MAX_RUN_ERROR_CHARS` (4,000) bounds
# the *whole* `indexer_runs.error` text, and a single provider traceback can
# exceed it on its own and evict the stage labels beside it.
MAX_EMBED_FAILURE_MESSAGE_CHARS = 200

# Longest `pattern` `list_files` will accept. `fnmatch.translate` +
# `re.compile` is linear at ~10 µs/char and runs on the event loop, so a
# 500 KB pattern was a 5.4 s stall for every other tenant; the transport body
# cap admitted ~10 minutes of it. 1,024 characters compiles in ~5 ms and is
# far more than any real glob. Enforced in `vault.list_dir` — before the
# pattern is compiled and before the folder is validated or read.
MAX_LIST_PATTERN_CHARS = 1024

# Aggregate bound on the preflight of `move_note(rewrite_links=True)`. That
# preflight holds, for every backlink source, both the original bytes and the
# rewritten content in memory before a single byte is mutated — the price of
# never half-applying a move. Each source is individually bounded by
# `MAX_NOTE_BYTES`, but the *number* of sources is not: a heavily linked target
# with hundreds of near-cap backlinks would otherwise buffer gigabytes. This
# caps the sum (originals plus rewrites) and aborts the move before any
# mutation when it would be exceeded.
MAX_MOVE_REWRITE_BYTES = 256 * 1024 * 1024  # 256 MiB

# Descriptors the same preflight may pin, for the same reason. Each planned
# rewrite holds one open parent descriptor from its phase-1 read until its
# phase-3 write — that single descriptor is what makes the read and the write
# provably refer to the same directory (#59), and the preflight has to finish
# before the move commits so an over-cap rewrite can still abort it. Sources
# that turn out to need no rewrite are released immediately, but a genuinely
# hub-like note can still plan hundreds.
#
# Unbounded, that exhausts the *process* descriptor table, which breaks every
# concurrent request rather than just this call. So the move aborts before any
# mutation once the plan would consume more than the running limit leaves
# spare. Derived from `RLIMIT_NOFILE` rather than pinned to a number: a
# container with a million descriptors should not be held to a laptop's 1024.
#
# **There is no floor.** An earlier version would not refuse below 64 planned
# rewrites whatever the limit said, on the theory that a small move should
# always be allowed. That inverts the purpose: on a process whose limit really
# is tiny, the floor guarantees the exhaustion the cap exists to prevent. If
# the budget says zero, the honest answer is to refuse and let the operator
# raise `RLIMIT_NOFILE`.
MOVE_REWRITE_FD_RESERVE = 256  # descriptors left for the rest of the process

# The rewrite phase also holds **one** vault-root descriptor for the whole
# phase, shared by every planned rewrite (`MutableTarget.share_root`). Charged
# here so the arithmetic is visible rather than absorbed into the reserve.
#
# It is one and not one-per-rewrite deliberately. Every rewrite target resolves
# the same vault root, so the alternative — giving each target its own root, the
# other way to make the post-publication ancestor flush work — would pin two
# descriptors per source and **halve** this cap, to hold N duplicate descriptors
# of one directory. At a 1024 soft limit that is 384 planned rewrites instead of
# 767. One shared descriptor costs exactly one.
MOVE_REWRITE_SHARED_ROOT_FDS = 1


def max_move_rewrite_sources() -> int:
    """How many planned link rewrites one `move_note` may hold open at once.

    Each planned rewrite pins one parent descriptor from its preflight read
    until its post-move write; the phase pins one shared root on top of that.

    Read at call time, not import time: the limit is a property of the running
    process, and a test (or an operator) may raise it.
    """
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft in (resource.RLIM_INFINITY, -1):
        return sys.maxsize
    return max(0, soft - MOVE_REWRITE_FD_RESERVE - MOVE_REWRITE_SHARED_ROOT_FDS)


# Headroom for the JSON-RPC envelope around a tool call's content argument:
# method name, tool name, request id, the other arguments. See
# `Settings.mcp_max_request_body_bytes`.
_MCP_ENVELOPE_ALLOWANCE_BYTES = 1024 * 1024  # 1 MiB


class _FieldFilteredSource(PydanticBaseSettingsSource):
    """Wraps a settings source and drops keys that are not `Settings` fields.

    Used for the **dotenv source only**. The repo-root `.env` is shared with
    `docker-compose.yml` and legitimately carries compose-only keys
    (`VAULT_HOST_PATH`, and the retired `BACKUPS_HOST_PATH` still present in
    older `.env` files) that are not settings; with
    pydantic-settings' `extra="forbid"` they make `Settings()` — and therefore
    a single-file `pytest` run from a checkout — fail at import.

    Filtering the source instead of relaxing `extra` on the model keeps
    `Settings(databse_url=...)` (a misspelled constructor kwarg) a hard error,
    and leaves process environment variables untouched (pydantic-settings never
    applies `extra` checks to those, which is why the container is unaffected).

    Field entries arrive keyed by field name regardless of `env_prefix` (only
    unknown extras keep their raw, prefixed name), so matching on field names
    and aliases is prefix-safe. Matching is case-insensitive.
    """

    def __init__(self, inner: PydanticBaseSettingsSource):
        super().__init__(inner.settings_cls)
        self._inner = inner

    def _set_current_state(self, state: dict[str, Any]) -> None:
        super()._set_current_state(state)
        self._inner._set_current_state(state)

    def _set_settings_sources_data(self, states: dict[str, dict[str, Any]]) -> None:
        super()._set_settings_sources_data(states)
        self._inner._set_settings_sources_data(states)

    def _allowed_keys(self) -> set[str]:
        allowed: set[str] = set()
        for name, field in self.settings_cls.model_fields.items():
            allowed.add(name.lower())
            for alias in (field.alias, field.validation_alias, field.serialization_alias):
                if isinstance(alias, str):
                    allowed.add(alias.lower())
        return allowed

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        return self._inner.get_field_value(field, field_name)

    def __call__(self) -> dict[str, Any]:
        allowed = self._allowed_keys()
        return {k: v for k, v in self._inner().items() if k.lower() in allowed}

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self._inner!r})"


class Settings(BaseSettings):
    database_url: str = "postgresql+asyncpg://obsidian_mcp:changeme@postgres:5432/obsidian_mcp"
    ollama_url: str = "http://ollama:11434"
    # How long Ollama keeps the embedding model resident after a call.
    # "-1" pins it in VRAM indefinitely (sent as the integer Ollama requires),
    # which avoids the ~15s cold reload when semantic_search runs infrequently
    # and the model has been evicted. A Go duration like "30m" instead frees
    # VRAM when idle. Ollama provider only — ignored by the OpenAI provider.
    ollama_keep_alive: str = "-1"
    vault_path: str = "/obsidian"
    secret_key: str = "changeme"
    index_interval_seconds: int = Field(300, ge=1)
    embedding_model: str = "bge-m3"
    embedding_dimensions: int = Field(1024, ge=1, le=16000)
    # bge-m3 design point. Must stay strictly greater than `chunk_overlap`:
    # the chunker steps by `max(char_size - char_overlap, 1)`, so at equality
    # the step collapses to one character and `MAX_CHUNKS_PER_NOTE` stops
    # bounding a *note* and starts bounding ~3 KB of prose. Enforced by
    # `_reject_overlap_at_or_above_chunk_size` below.
    chunk_size: int = Field(512, ge=1)
    # Overlap disabled: 2025 chunking benchmarks show no measurable retrieval
    # benefit; some research finds zero overlap optimal. Must stay strictly
    # below `chunk_size` — see the note there and the validator below; the #10
    # infinite-loop guard turns the equal case from a hang into a quiet
    # catastrophe, which is not the same as making it sane.
    chunk_overlap: int = Field(0, ge=0)
    # Per-user bounds on one pass's *embed stage*, so one tenant's backlog
    # cannot hold every later tenant's new and edited notes out of the index.
    # `0` disables either. Both are checked only at a note boundary, after at
    # least one note, and **only when the pass serves more than one active user
    # scope** — in single-user mode, and in a multi-user deployment with one
    # active user, there is no other tenant to be fair to and a budget would
    # turn a first index of a few thousand notes into several passes separated
    # by five-minute sleeps for no benefit.
    #
    # The chunk budget debits chunks *submitted* to the provider, never chunks
    # stored: a budget debited by stored chunks is not debited at all when the
    # provider fails, so a tenant whose notes all fail would consume the whole
    # pass, every pass, without ever reaching its bound — the starvation the
    # budget exists to stop, surviving inside it.
    #
    # A budget stop is not a failure: it writes nothing to `indexer_runs.error`
    # and logs once per user per pass. The operator-visible signal for a tenant
    # permanently over budget is the dashboard's pending count.
    embed_chunk_budget_per_user: int = Field(5000, ge=0)
    # 300 s matches one `INDEX_INTERVAL_SECONDS`.
    embed_time_budget_seconds_per_user: int = Field(300, ge=0)
    # Path globs (fnmatch) skipped by the embedder — files remain
    # keyword-searchable but produce no vectors. Default skips Excalidraw
    # plugin files (drawings + downloaded scripts) which contain serialized
    # JSON or automation code rather than searchable prose.
    embedding_exclude_patterns: list[str] = ["*.excalidraw.md", "Excalidraw/*"]
    # Public hostname Traefik/Caddy routes to. When set, base_url, allowed_origins,
    # and allowed_hosts are auto-derived (https + this host) unless overridden.
    mcp_hostname: str | None = None
    base_url: str | None = None
    allowed_origins: list[str] | None = None
    allowed_hosts: list[str] | None = None

    # PostgreSQL text-search configuration(s) for full-text (keyword) search,
    # applied at both index and query time. A note is indexed under every
    # listed config (lexeme sets concatenated) and a query matches if ANY
    # config's parse hits (tsqueries OR'd). See `src/services/fts.py`.
    #   ["english"]            current/default behavior (English stemmer)
    #   ["simple"]             language-agnostic, exact word forms, no stemming
    #   ["english","norwegian"] both stemmers — mixed-language vault
    #   ["simple","norwegian"]  verbatim lexemes PLUS Norwegian stems
    # Named `fts_configs` (not `fts_languages`) because `simple` is a config,
    # not a language. Env `FTS_CONFIGS` accepts JSON (`["simple","norwegian"]`)
    # or comma-separated (`simple,norwegian`). Changing this makes stored
    # tsvectors stale — run `make rebuild-tsvectors` (keyword index only; no
    # embeddings touched, no API calls). `NoDecode` defers env parsing to the
    # validator below so the CSV form doesn't trip pydantic-settings' JSON
    # decode of complex (list) fields.
    fts_configs: Annotated[list[str], NoDecode] = ["english"]

    embedding_provider: Literal["ollama", "openai"] = "ollama"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_embedding_model: str = "text-embedding-3-small"

    # Caps for the raw file-access tools (read_file / write_file). Read is
    # checked against on-disk size before reading; write against the decoded
    # byte length. Read default (10 MB) matches MAX_NOTE_BYTES and guards
    # against context blowups from base64; write is intentional so it gets a
    # higher ceiling (25 MB). Env: MAX_FILE_READ_BYTES / MAX_FILE_WRITE_BYTES.
    max_file_read_bytes: int = Field(10 * 1024 * 1024, ge=1)
    max_file_write_bytes: int = Field(25 * 1024 * 1024, ge=1)

    # ── Out-of-band binary transfer (`/transfer/*` capability routes) ────────
    # Default lifetime of a mint (`request_upload` / `request_download`). A
    # per-call `expires_in` is clamped to [60, 3600]; this default is clamped
    # to the same window at load time so an operator cannot configure a
    # capability that outlives the bound it is documented to have.
    transfer_token_ttl_seconds: int = Field(600, ge=60, le=3600)
    # Wall-clock bound on one claimed upload's body: the deadline is
    # `min(expires_at, claimed_at + this)`. Bounds a slow-drip stream that
    # would otherwise hold a claim (and a semaphore slot) indefinitely.
    transfer_max_upload_seconds: int = Field(600, ge=1)
    # Simultaneous streaming uploads. Each holds an open temp file and a
    # request task; this is the only thing bounding aggregate upload memory
    # and disk churn on a public route.
    transfer_max_concurrent_uploads: int = Field(4, ge=1)
    # `import_from_url` refuses plain http by default. Turning this on also
    # admits ports 80/8080 for the http scheme (443/8443 stay https-only).
    import_allow_http: bool = False

    # ── Named-staging fallback (one flag, both write paths) ──────────────────
    # Both write paths stage into an unnamed `O_TMPFILE` inode and publish it
    # by descriptor, so no staging name ever exists for a peer to observe,
    # replace or race. Some servers refuse `O_TMPFILE` outright — TrueNAS
    # SCALE's NFS export answers `EOPNOTSUPP` as root, under NFSv4.1 and
    # NFSv4.2 alike (#103) — and on such a mount the no-clobber note writes and
    # every transfer publication would be refused.
    #
    # Setting this takes named staging back on **both** paths. It is one knob
    # on purpose: the failure is one filesystem property met on two paths for
    # one reason, and two knobs would permit a deployment with a working
    # `create_note` and a refusing upload — a state nobody chose and nobody can
    # diagnose from either symptom alone. There is deliberately no `TRANSFER_*`
    # variant and no per-path override.
    #
    # Default off, because it reopens the substitution window unnamed staging
    # exists to close. When it is taken, the server says so: one WARNING per
    # process the first time a call actually stages under a name, and
    # `vault_named_staging_fallback_active` on `/health`.
    # Env: VAULT_ALLOW_NAMED_STAGING_FALLBACK.
    vault_allow_named_staging_fallback: bool = False

    @property
    def mcp_max_request_body_bytes(self) -> int:
        """Maximum MCP streamable-HTTP request body, derived from the write caps.

        `max(2 × MAX_FILE_WRITE_BYTES, 6 × MAX_NOTE_BYTES) + 1 MiB`
        (61 MiB with the defaults). Passed to `FastMCP(max_request_body_size=)`;
        the SDK would otherwise apply its own 4 MiB default, which silently
        rejects a `write_file` well below our documented 25 MB cap.

        There is deliberately no separate env knob: the transport limit must
        track the tool caps, so that every *supported* write is decided by the
        tool (with an actionable message) and the transport only ever bounds
        unsupported shapes.

        The guarantee is qualified. For a canonical `tools/call` envelope —
        JSON-RPC framing plus all non-content arguments encoding to at most
        1 MiB − 2 bytes — these shapes always reach the tool:

        - `write_file(encoding="base64")` with decoded content up to
          `max_file_write_bytes`: base64 length is exactly `4·⌈n/3⌉ ≤ 2n + 2`
          for n ≥ 1, so it fits in `2 × cap` with the envelope allowance to spare.
        - Any note write (`create_note`, `edit_note`, `set_frontmatter`) whose
          content arguments are at most `MAX_NOTE_BYTES` of UTF-8 before JSON
          escaping: escaping expands a byte at most 6× (a control character
          becomes the six-character `\\u00XX`; BMP escapes under `ensure_ascii`
          are 2× per byte, astral surrogate pairs 3×), so `6 × MAX_NOTE_BYTES`
          covers the worst case. `MAX_NOTE_BYTES` is in the formula so this
          holds however small an operator sets `MAX_FILE_WRITE_BYTES`.
        - `write_file(encoding="text")` whose JSON-escaped content fits the
          limit; realistic prose in any script (≤ 2×) does with the defaults.

        Everything else is bounded by the transport with a bare HTTP 413 and is
        unsupported: text-mode content whose escaping exceeds the limit (use
        base64 — always safe), an envelope over 1 MiB, or arguments that are
        large but discarded.
        """
        return (
            max(2 * self.max_file_write_bytes, 6 * MAX_NOTE_BYTES)
            + _MCP_ENVELOPE_ALLOWANCE_BYTES
        )

    # Cap on how much note/file text a single read_note / read_file call may
    # return to the model. The byte caps above stop the server from reading a
    # huge file into memory; this one stops a legitimately-read file from
    # blowing the caller's context window. ~40k chars ≈ 10k tokens.
    # Env: MAX_READ_RESPONSE_CHARS.
    max_read_response_chars: int = Field(40_000, ge=1_000)

    multi_user_mode: bool = False
    session_max_age: int = 60 * 60 * 24 * 7
    session_cookie_name: str = "omcp_session"

    # ── Panel session registry (#198, migration 024) ───────────────────────
    #
    # How stale `user_sessions.last_seen_at` may get before a validated request
    # rewrites it. The touch is telemetry — nothing authorizes on it — and it
    # runs on `GET`/`HEAD` only, on the request's own database session.
    #
    # **`ge=1` is enforced, not assumed.** A zero or negative interval turns a
    # throttled hint into an `UPDATE` plus a commit on *every* panel request,
    # so the bound fails at settings construction and stops the container
    # rather than degrading it silently. Env: SESSION_TOUCH_INTERVAL_SECONDS.
    session_touch_interval_seconds: int = Field(60, ge=1)

    # How long a dead session row is kept after it dies. The purge deletes a
    # row only once *both* its expiry and, if set, its revocation are further
    # in the past than this — the later of the two, never `expires_at` alone.
    #
    # **`ge=1` is enforced for a specific failure.** An administrative password
    # reset revokes every unrevoked row of a user, already-expired rows
    # included; with a zero window such a row is deleted on the next indexer
    # tick, erasing the record of a revocation minutes after an operator
    # performed it. That is the #64 blank space this retention exists to
    # prevent. Env: SESSION_PURGE_RETAIN_DAYS.
    session_purge_retain_days: int = Field(7, ge=1)

    # Redirect **hosts** an operator recognises as belonging to a real
    # connector. The OAuth consent screen shows a badge when a client's
    # redirect host equals one of these and a warning naming the host when it
    # does not; an empty list means nothing is recognised, which is safe (every
    # client warns) rather than permissive.
    #
    # Matching is case-insensitive **equality** and nothing else — a suffix
    # test is the bug it looks like a convenience: `endswith("claude.ai")`
    # matches `evilclaude.ai`, `"claude.ai" in host` matches
    # `claude.ai.evil.example`, and even `endswith(".claude.ai")` hands the
    # badge to any subdomain an attacker can obtain. Neither real connector
    # needs one.
    #
    # `NoDecode` for `fts_configs`' reason: **a bare `list[str]` is
    # JSON-decoded by pydantic-settings**, so the comma-separated form an
    # operator naturally writes would abort startup. Env
    # `OAUTH_KNOWN_REDIRECT_HOSTS` accepts JSON (`["claude.ai"]`) or CSV
    # (`claude.ai,chatgpt.com`).
    oauth_known_redirect_hosts: Annotated[list[str], NoDecode] = [
        "claude.ai",
        "chatgpt.com",
    ]

    # Registry-eval only: when true, lifespan skips the DB dim check,
    # indexer, and embedding provider, and the /mcp auth middleware
    # short-circuits. Lets Glama's sandbox build the image and validate
    # MCP introspection without real external deps. Never enable in
    # production — tools register but cannot run.
    mcp_sandbox_mode: bool = False

    # ── Logging (see docs/architecture/security-event-logging.md) ───────────
    # The root level `src/logging_setup.configure_logging()` applies. Accepts
    # any standard level name, case-insensitively; an unknown name is refused
    # at startup rather than silently degrading to WARNING.
    log_level: str = "INFO"
    # `json` is the production rendering — one line per record, parsed natively
    # by Alloy/Loki. `text` renders the *same* allow-listed object as
    # `ts level logger msg k=v …` for reading `make logs` locally; it is a
    # second rendering, never a second field policy. `Literal` refuses anything
    # else when `Settings()` is constructed, i.e. at startup.
    log_format: Literal["json", "text"] = "json"

    # `extra` stays at pydantic-settings' default ("forbid") so a misspelled
    # constructor kwarg or an unknown init value is still a hard error; only the
    # dotenv source is filtered (see `settings_customise_sources` below).
    model_config = {"env_file": ".env"}

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        """Default source order, with the dotenv source filtered to known fields.

        The repo-root `.env` doubles as the compose env file and carries
        compose-only keys (`VAULT_HOST_PATH`, or a retired `BACKUPS_HOST_PATH` in
        an older `.env`) that are not `Settings` fields. Under `extra="forbid"` those abort `Settings()` at
        import time. Dropping them from the dotenv source only keeps every other
        surface strict.
        """
        return (
            init_settings,
            env_settings,
            _FieldFilteredSource(dotenv_settings),
            file_secret_settings,
        )

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalise_log_level(cls, v):
        """Upper-case the level name and refuse one `logging` does not know.

        `logging.getLevelName("LOUD")` answers the string `"Level LOUD"` rather
        than raising, and `setLevel` on that value throws at the first record —
        i.e. long after startup, from inside a logging call. Refusing here makes
        a typo a start-up failure like every other bad setting.
        """
        if isinstance(v, int):
            return logging.getLevelName(v)
        if isinstance(v, str):
            name = v.strip().upper()
            if name not in logging.getLevelNamesMapping():
                raise ValueError(
                    f"LOG_LEVEL must be a standard logging level name, got {v!r}"
                )
            return name
        return v

    @field_validator("fts_configs", mode="before")
    @classmethod
    def _parse_fts_configs(cls, v):
        """Accept a JSON list, a comma-separated string, or a list; then strip,
        lowercase, drop empties, and dedupe (order-preserving). Reject empty."""
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json

                try:
                    parsed = json.loads(s)
                except ValueError as e:
                    # Looks like JSON (leading "[") but isn't — fail loudly
                    # rather than silently CSV-splitting into junk config names.
                    raise ValueError(
                        f"FTS_CONFIGS looks like JSON but failed to parse: {e}. "
                        'Use a JSON list (["simple","norwegian"]) or a '
                        "comma-separated string (simple,norwegian)."
                    ) from e
                if not isinstance(parsed, list):
                    raise ValueError("FTS_CONFIGS JSON must be a list of config names")
                v = parsed
            else:
                v = s.split(",")
        if not isinstance(v, (list, tuple)):
            raise ValueError(
                "FTS_CONFIGS must be a list of PostgreSQL text-search config "
                "names (JSON or comma-separated)"
            )
        seen: set[str] = set()
        out: list[str] = []
        for item in v:
            name = str(item).strip().lower()
            if not name or name in seen:
                continue
            seen.add(name)
            out.append(name)
        if not out:
            raise ValueError("FTS_CONFIGS must contain at least one config name")
        return out

    @field_validator("oauth_known_redirect_hosts", mode="before")
    @classmethod
    def _parse_known_redirect_hosts(cls, v):
        """Accept a JSON list, a comma-separated string, or a list of hosts.

        Mirrors `_parse_fts_configs` — strip each entry's outer whitespace
        (`claude.ai, chatgpt.com` means two hosts, not one host and one with a
        leading space that would then equal nothing), lower-case, drop empties,
        dedupe order-preservingly — and adds one rule of its own.

        **A pattern is rejected, loudly, at configuration time.** An entry
        containing `*`, `/`, `@` or internal whitespace is somebody reaching
        for a wildcard, a path, a userinfo form or a mistyped separator; since
        matching is exact-host equality, such an entry would match nothing at
        all and every client would silently take the warning branch while the
        operator believed they had allow-listed one. Refusing at startup makes
        that a container that will not start rather than a badge that never
        appears.

        An **empty** list is accepted and means "nothing is recognised" — every
        consent screen warns, which is the safe direction and a legitimate
        thing for an operator to configure.
        """
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                import json

                try:
                    parsed = json.loads(s)
                except ValueError as e:
                    # Looks like JSON (leading "[") but isn't — fail loudly
                    # rather than CSV-splitting into junk host names.
                    raise ValueError(
                        "OAUTH_KNOWN_REDIRECT_HOSTS looks like JSON but failed "
                        f"to parse: {e}. Use a JSON list "
                        '(["claude.ai","chatgpt.com"]) or a comma-separated '
                        "string (claude.ai,chatgpt.com)."
                    ) from e
                if not isinstance(parsed, list):
                    raise ValueError(
                        "OAUTH_KNOWN_REDIRECT_HOSTS JSON must be a list of hosts"
                    )
                v = parsed
            else:
                v = s.split(",")
        if not isinstance(v, (list, tuple)):
            raise ValueError(
                "OAUTH_KNOWN_REDIRECT_HOSTS must be a list of bare hostnames "
                "(JSON or comma-separated)"
            )
        seen: set[str] = set()
        out: list[str] = []
        for item in v:
            host = str(item).strip().lower()
            if not host:
                continue
            bad = [c for c in ("*", "/", "@") if c in host]
            if any(c.isspace() for c in host):
                bad.append("whitespace")
            if bad:
                raise ValueError(
                    f"OAUTH_KNOWN_REDIRECT_HOSTS entry {host!r} contains "
                    f"{', '.join(repr(c) for c in bad)}; patterns are not "
                    "supported. Each entry is one bare hostname matched by "
                    "exact, case-insensitive equality — no wildcards, no "
                    "paths, no userinfo, no suffix matching."
                )
            if host in seen:
                continue
            seen.add(host)
            out.append(host)
        return out

    # Set by `_record_public_origin`, which must run *before*
    # `_derive_public_urls` fills in the localhost fallback. Once that
    # fallback has been applied, `base_url` is no longer evidence of what the
    # operator configured — which is exactly the state the transfer mint tools
    # must be able to distinguish (a capability URL on `http://localhost:8000`
    # is useless to the human who is supposed to open it).
    _public_origin_explicit: bool = PrivateAttr(default=False)

    @field_validator("mcp_hostname", mode="before")
    @classmethod
    def _normalize_mcp_hostname(cls, v):
        """Fold `MCP_HOSTNAME` to a bare lowercase host, or `None`.

        Field validators run before every `mode="after"` model validator, so
        every derivation below sees the normalized form — which is the point.
        `allowed_hosts` is derived from this value and Starlette's
        `TrustedHostMiddleware` compares the Host header *exactly*, while
        browsers and proxies send the host lowercased. So `Vault.Example.com`
        booted clean, kept its casing into `allowed_hosts`, and then 400'd
        every public request while the localhost health check (matched by the
        always-appended "localhost") stayed green — a deployment that looks
        healthy and serves nobody.

        A value that is only whitespace becomes `None` rather than a truthy
        blank: it would otherwise derive `https://` as the base URL and read
        as an operator-supplied public origin.
        """
        if not isinstance(v, str):
            return v
        return v.strip().lower() or None

    @model_validator(mode="after")
    def _record_public_origin(self) -> "Settings":
        """Record whether a public origin was operator-supplied.

        Pydantic runs `mode="after"` model validators in definition order, so
        at this point `mcp_hostname` and `base_url` still hold exactly what the
        environment / `.env` / constructor provided. Ordering matters: this
        method must stay above `_derive_public_urls`.
        """
        self._public_origin_explicit = bool(
            (self.mcp_hostname or "").strip() or (self.base_url or "").strip()
        )
        return self

    @property
    def public_base_url(self) -> str | None:
        """The origin a human-openable link may be built on, or `None`.

        `base_url` always has a value (the localhost fallback), so it cannot
        answer "did anyone configure a public origin?". This can: it is `None`
        unless `MCP_HOSTNAME` or `BASE_URL` was operator-supplied. The transfer
        mint tools refuse — naming both settings — rather than hand an agent a
        link that resolves to the container's own loopback.
        """
        return self.base_url if self._public_origin_explicit else None

    @model_validator(mode="after")
    def _derive_public_urls(self) -> "Settings":
        if self.mcp_hostname:
            public = f"https://{self.mcp_hostname}"
            if self.base_url is None:
                self.base_url = public
            if self.allowed_origins is None:
                self.allowed_origins = [public]
            if self.allowed_hosts is None:
                self.allowed_hosts = [self.mcp_hostname, "localhost"]
        else:
            if self.base_url is None:
                self.base_url = "http://localhost:8000"
            if self.allowed_origins is None:
                self.allowed_origins = ["http://localhost:8000"]
            if self.allowed_hosts is None:
                self.allowed_hosts = ["localhost"]
        # Always ensure localhost is allowed so Docker health checks (which hit
        # http://localhost:8000/health) are never blocked by TrustedHostMiddleware,
        # even when ALLOWED_HOSTS is set explicitly in the environment.
        if "localhost" not in self.allowed_hosts:
            self.allowed_hosts = list(self.allowed_hosts) + ["localhost"]
        return self

    @model_validator(mode="after")
    def _reject_wildcard_cors_origin(self) -> "Settings":
        """Refuse a `*` entry in ALLOWED_ORIGINS.

        `src/main.py` installs `CORSMiddleware(..., allow_credentials=True)`,
        and Starlette reads `allow_origins=["*"]` alongside credentials as
        "reflect whatever Origin the request carried" — it echoes the caller's
        origin back in `Access-Control-Allow-Origin` and sets
        `Access-Control-Allow-Credentials: true`. Every site the operator has
        open in the same browser could then make credentialed cross-site
        requests to the panel, the API and the OAuth endpoints and read the
        responses. A wildcard is therefore not a permissive setting here, it
        is the removal of the same-origin boundary the session cookie relies on.

        This runs after `_derive_public_urls`, so `allowed_origins` is always
        populated; the derived values (`https://$MCP_HOSTNAME`, or the
        localhost fallback) are never `*`, so the refusal can only ever fire
        on an explicit ALLOWED_ORIGINS override.
        """
        for origin in self.allowed_origins or ():
            if str(origin).strip() == "*":
                raise ValueError(
                    'ALLOWED_ORIGINS must not contain "*". CORS is configured '
                    "with allow_credentials=True, so a wildcard origin makes "
                    "the server reflect any Origin and accept credentialed "
                    "cross-site requests. List the exact origins instead."
                )
        return self

    @model_validator(mode="after")
    def _validate_public_transport(self) -> "Settings":
        """Permit plaintext OAuth only for loopback development."""
        base = self.base_url.rstrip("/")
        parsed = urlparse(base)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("BASE_URL must be an origin without credentials, path, query, or fragment")
        is_loopback = parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
            raise ValueError("BASE_URL must use HTTPS except for loopback development")
        if self.mcp_hostname and (
            parsed.scheme != "https" or parsed.hostname != self.mcp_hostname.lower()
        ):
            raise ValueError("BASE_URL must use HTTPS and match MCP_HOSTNAME")
        self.base_url = base
        return self

    @model_validator(mode="after")
    def _validate_provider_credentials(self) -> "Settings":
        if self.embedding_provider == "openai" and not (self.openai_api_key or "").strip():
            raise ValueError(
                "OPENAI_API_KEY is required when EMBEDDING_PROVIDER=openai"
            )
        return self

    @model_validator(mode="after")
    def _reject_overlap_at_or_above_chunk_size(self) -> "Settings":
        """`CHUNK_OVERLAP` must be strictly less than `CHUNK_SIZE`.

        The chunker steps by `max(char_size - char_overlap, 1)` — the #10
        infinite-loop guard. At `CHUNK_OVERLAP == CHUNK_SIZE` that step
        collapses to **one character**, so ~3 KB of prose produces ~3,000
        chunks and every ordinary note in the vault hits
        `MAX_CHUNKS_PER_NOTE`: a configuration typo silently truncating the
        embedding of the whole vault, with the cap's ERROR line firing
        thousands of times. Above it the step is the same floor with the
        arithmetic already nonsensical. The guard turned a hang into a quiet
        catastrophe; it did not make the configuration sane, so the
        configuration is refused here instead.
        """
        if self.chunk_overlap >= self.chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP ({self.chunk_overlap}) must be strictly less "
                f"than CHUNK_SIZE ({self.chunk_size}). At or above it the "
                "chunker's step collapses to one character, so a few kilobytes "
                "of prose becomes thousands of chunks and every note hits "
                f"MAX_CHUNKS_PER_NOTE ({MAX_CHUNKS_PER_NOTE})."
            )
        return self

    # Known weak placeholders shipped in .env.example / defaults. Matched
    # case-insensitively so e.g. CHANGE_ME and changeme are both rejected.
    _SECRET_KEY_PLACEHOLDERS = frozenset(
        {"changeme", "change_me", "change-me", ""}
    )

    @model_validator(mode="after")
    def _validate_multi_user_secret(self) -> "Settings":
        if self.secret_key.strip().lower() in self._SECRET_KEY_PLACEHOLDERS:
            raise ValueError(
                "SECRET_KEY must not be a placeholder value. Generate a strong "
                'key with: python -c "import secrets; print(secrets.token_hex(32))"'
            )
        return self

    @model_validator(mode="after")
    def _reject_sandbox_with_public_hostname(self) -> "Settings":
        """Refuse to boot a publicly-routed deployment with auth disabled.

        MCP_SANDBOX_MODE bypasses all authentication on /mcp (registry-eval
        only). Combined with a public route that would expose the vault
        unauthenticated to the internet, so reject the combination at
        startup — analogous to the SECRET_KEY placeholder guard above.

        MCP_HOSTNAME is not the only way to declare a public route, so all
        three of the settings that admit outside traffic are checked:

        * MCP_HOSTNAME — the Traefik/Caddy hostname;
        * BASE_URL — the origin OAuth redirects and transfer links are built
          on, which an operator can set without MCP_HOSTNAME;
        * ALLOWED_HOSTS — what TrustedHostMiddleware will answer for, i.e.
          the `Host` headers this process accepts at all.

        This validator runs after `_derive_public_urls`, so it sees the
        *effective* values. That is deliberate: the derived ones are
        loopback (`http://localhost:8000`, `["localhost"]`) and never trip
        the check, while an explicit env override does. `_is_loopback_host`
        fails closed, so an unrecognised name or a `*` wildcard entry — which
        makes TrustedHostMiddleware answer for every hostname — counts as
        public.
        """
        if not self.mcp_sandbox_mode:
            return self

        def _refuse(setting: str, detail: str) -> None:
            raise ValueError(
                "MCP_SANDBOX_MODE disables all authentication on /mcp and must "
                f"never run on a publicly-routed deployment. {setting} {detail}. "
                f"Either unset MCP_SANDBOX_MODE or remove {setting}."
            )

        if (self.mcp_hostname or "").strip():
            _refuse("MCP_HOSTNAME", "declares a public hostname")

        base_host = urlparse(self.base_url or "").hostname or ""
        if not _is_loopback_host(base_host):
            _refuse("BASE_URL", f"names the non-loopback host '{base_host}'")

        public_hosts = [
            str(h).strip()
            for h in (self.allowed_hosts or ())
            if not _is_loopback_host(str(h))
        ]
        if public_hosts:
            _refuse(
                "ALLOWED_HOSTS",
                "accepts the non-loopback host(s) " + ", ".join(
                    repr(h) for h in public_hosts
                ),
            )
        return self


settings = Settings()
