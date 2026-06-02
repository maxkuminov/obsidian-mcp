from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings


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

    embedding_provider: Literal["ollama", "openai"] = "ollama"
    openai_api_key: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    openai_embedding_model: str = "text-embedding-3-small"

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
