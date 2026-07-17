from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode


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
    chunk_size: int = Field(512, ge=1)  # bge-m3 design point
    # Overlap disabled: 2025 chunking benchmarks show no measurable retrieval
    # benefit; some research finds zero overlap optimal.
    chunk_overlap: int = Field(0, ge=0)
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

    multi_user_mode: bool = False
    session_max_age: int = 60 * 60 * 24 * 7
    session_cookie_name: str = "omcp_session"

    # Registry-eval only: when true, lifespan skips the DB dim check,
    # indexer, and embedding provider, and the /mcp auth middleware
    # short-circuits. Lets Glama's sandbox build the image and validate
    # MCP introspection without real external deps. Never enable in
    # production — tools register but cannot run.
    mcp_sandbox_mode: bool = False

    model_config = {"env_file": ".env"}

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
        only). Combined with a public MCP_HOSTNAME that would expose the
        vault unauthenticated to the internet, so reject the combination at
        startup — analogous to the SECRET_KEY placeholder guard above.
        """
        if self.mcp_sandbox_mode and (self.mcp_hostname or "").strip():
            raise ValueError(
                "MCP_SANDBOX_MODE disables all authentication on /mcp and must "
                "never run on a publicly-routed deployment. Either unset "
                "MCP_SANDBOX_MODE or remove MCP_HOSTNAME."
            )
        return self


settings = Settings()
