import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, TSVECTOR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from src.config import settings




class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    session_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    vault_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    last_login_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    api_keys: Mapped[list["APIKey"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    oauth_clients: Mapped[list["OAuthClient"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    oauth_tokens: Mapped[list["OAuthToken"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    notes: Mapped[list["NoteMetadata"]] = relationship(back_populates="user", cascade="all, delete-orphan")


class APIKey(Base):
    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    key_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    key_prefix: Mapped[str] = mapped_column(String(12), nullable=False)
    permission: Mapped[str] = mapped_column(String(20), nullable=False, default="read")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    expires_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_used_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    usage_logs: Mapped[list["UsageLog"]] = relationship(back_populates="api_key")
    user: Mapped["User | None"] = relationship(back_populates="api_keys")


class UsageLog(Base):
    __tablename__ = "usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("api_keys.id"), nullable=True)
    oauth_token_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("oauth_tokens.id", ondelete="SET NULL"), nullable=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    tool: Mapped[str] = mapped_column(String(100), nullable=False)
    params: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Denormalised attribution (issue #77, migration 015). The FK columns above
    # are *both* allowed to lose their target while the log row stays:
    # `oauth_token_id` is ON DELETE SET NULL, so deleting an OAuth client
    # cascades its tokens and unattributes every line that client produced, and
    # `key_id` has no ON DELETE at all, so the panel NULLs it by hand before
    # deleting an API key. Resolving the actor by LEFT JOIN at read time
    # therefore rendered exactly that history as "unknown" — the evidence an
    # operator opens /admin/usage to read, destroyed by the button they pressed
    # to stop the client.
    #
    # Written at call time from the credential the request authenticated with,
    # so it is a fact about what happened rather than a lookup that a later
    # delete can invalidate. Nullable: rows written before 015 whose credential
    # was already gone have nothing to backfill from, and they render
    # "unknown (credential deleted)". Never read for authorization — this is
    # display and audit only.
    #
    # `actor_kind` is 'api_key' | 'oauth'; `actor_label` is the key's name or
    # the OAuth `client_name`; `actor_ref` is the key's `omcp_` prefix or the
    # `client_id`. Name and identifier stay separate columns on purpose: joined
    # into one string the row stops being a record — a key named "audit (prod)"
    # is not recoverable from "audit (prod) (omcp_a1b2c3)".
    actor_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    actor_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actor_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    api_key: Mapped["APIKey | None"] = relationship(back_populates="usage_logs")

    __table_args__ = (
        Index("ix_usage_logs_created_at", "created_at"),
        Index("ix_usage_logs_oauth_token_id", "oauth_token_id"),
    )


class NoteMetadata(Base):
    __tablename__ = "notes_metadata"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    tags: Mapped[list[str] | None] = mapped_column(ARRAY(String), nullable=True)
    frontmatter: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    embedded_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    content_tsvector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True)
    file_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    modified_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    indexed_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    embeddings: Mapped[list["NoteEmbedding"]] = relationship(
        back_populates="note", cascade="all, delete-orphan"
    )
    user: Mapped["User | None"] = relationship(back_populates="notes")

    __table_args__ = (
        # NULLS NOT DISTINCT so single-user-mode rows (user_id IS NULL)
        # collide on file_path alone and the indexer's upsert fires.
        # See migration 009 for the matching DDL.
        UniqueConstraint(
            "user_id",
            "file_path",
            name="uq_notes_metadata_user_id_file_path",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_notes_metadata_tsvector", "content_tsvector", postgresql_using="gin"),
        Index("ix_notes_metadata_tags", "tags", postgresql_using="gin"),
    )


class NoteEmbedding(Base):
    __tablename__ = "note_embeddings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    note_id: Mapped[int] = mapped_column(Integer, ForeignKey("notes_metadata.id", ondelete="CASCADE"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_text: Mapped[str] = mapped_column(Text, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dimensions), nullable=False)

    note: Mapped["NoteMetadata"] = relationship(back_populates="embeddings")

    __table_args__ = (
        Index("ix_note_embeddings_note_id", "note_id"),
        Index(
            "ix_note_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
            postgresql_with={"m": "16", "ef_construction": "64"},
        ),
    )


class NoteLink(Base):
    __tablename__ = "note_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_note_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("notes_metadata.id", ondelete="CASCADE"),
        nullable=False,
    )
    target_note_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("notes_metadata.id", ondelete="SET NULL"),
        nullable=True,
    )
    target_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    link_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="link")
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_note_links_source", "source_note_id"),
        Index("ix_note_links_target", "target_note_id"),
        Index("ix_note_links_target_path", "target_path"),
    )


class OAuthClient(Base):
    __tablename__ = "oauth_clients"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    client_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    # Public PKCE clients (token_endpoint_auth_method="none") do not have a
    # client secret. Confidential clients continue to store only its hash.
    client_secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    token_endpoint_auth_method: Mapped[str] = mapped_column(
        String(32), nullable=False, default="client_secret_post"
    )
    client_name: Mapped[str] = mapped_column(String(255), nullable=False)
    redirect_uris: Mapped[list] = mapped_column(JSONB, nullable=False)
    scope: Mapped[str] = mapped_column(String(50), nullable=False, default="read")
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User | None"] = relationship(back_populates="oauth_clients")

    __table_args__ = (
        CheckConstraint(
            "(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL) OR "
            "(token_endpoint_auth_method = 'client_secret_post' AND client_secret_hash IS NOT NULL)",
            name="ck_oauth_clients_auth_method_secret",
        ),
    )


class OAuthCode(Base):
    __tablename__ = "oauth_codes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False
    )
    redirect_uri: Mapped[str] = mapped_column(String(2048), nullable=False)
    scope: Mapped[str] = mapped_column(String(50), nullable=False)
    code_challenge: Mapped[str] = mapped_column(String(128), nullable=False)
    code_challenge_method: Mapped[str] = mapped_column(String(10), nullable=False, default="S256")
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class TransferToken(Base):
    """Capability token for one out-of-band binary transfer.

    Never stores the token itself — only its SHA-256, exactly like `api_keys`.
    Everything the redemption routes are allowed to act on is committed here at
    mint time (direction, canonical vault-relative `path`, the absolute
    `vault_root` in effect for the minting user, the minting identity, and — for
    overwrite uploads and for downloads — the target's fingerprint). The routes
    never take a path from the request.

    `expected_fingerprint` is `{dev, inode, size, mtime_ns, ctime_ns, sha256}`
    where `sha256` is null for targets above `MAX_FILE_WRITE_BYTES` (hashing
    multi-GB media at mint is not acceptable tool latency — a documented
    metadata-only binding). A **null column value** is different: on an
    overwrite token it is the *expected-absence sentinel* ("the target did not
    exist at mint"), and the publish step requires it to still be absent. It
    never means "skip the comparison".

    Identity FKs are `ON DELETE CASCADE` so revoking a key or deleting a user
    stays a simple delete; an in-flight upload whose row was cascaded away
    fails its locked pre-publication re-read and publishes nothing.
    """

    __tablename__ = "transfer_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # The handle the tools hand back to the agent. Deliberately *not* `id`: the
    # row id is a small sequential integer, so an `upload_id` built on it is
    # enumerable and an agent that leaks one leaks the shape of the table.
    # Identity scoping still applies on top — this is defence in depth, not a
    # replacement for it.
    public_id: Mapped[str] = mapped_column(String(43), unique=True, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    path: Mapped[str] = mapped_column(String(1024), nullable=False)
    vault_root: Mapped[str] = mapped_column(String(1024), nullable=False)
    overwrite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    expected_fingerprint: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    key_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("api_keys.id", ondelete="CASCADE"), nullable=True, index=True
    )
    oauth_token_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("oauth_tokens.id", ondelete="CASCADE"), nullable=True, index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    claimed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    mime: Mapped[str | None] = mapped_column(String(255), nullable=True)

    __table_args__ = (
        Index("ix_transfer_tokens_expires_at", "expires_at"),
        CheckConstraint(
            "direction IN ('upload', 'download')",
            name="ck_transfer_tokens_direction",
        ),
        CheckConstraint(
            "state IN ('pending', 'claimed', 'completed', 'consumed')",
            name="ck_transfer_tokens_state",
        ),
    )


class OAuthToken(Base):
    __tablename__ = "oauth_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    token_type: Mapped[str] = mapped_column(String(10), nullable=False)
    client_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("oauth_clients.client_id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(50), nullable=False)
    # The grant family this token belongs to (migration 014, issue #64). Every
    # row minted from one `/authorize` approval shares it, and every rotation
    # inherits it, so revocation and scope changes can act on the unit the
    # operator actually consented to instead of a single row whose sibling
    # refresh token would immediately undo the change. NOT NULL: the decision
    # in #64 was explicit that a nullable grant_id with a fallback "find the
    # family" path is how this bug comes back.
    grant_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    expires_at: Mapped[datetime.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    user: Mapped["User | None"] = relationship(back_populates="oauth_tokens")
