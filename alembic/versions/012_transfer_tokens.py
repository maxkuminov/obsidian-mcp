"""Add transfer_tokens for out-of-band binary upload/download capabilities.

Revision ID: 012
Revises: 011
Create Date: 2026-08-15
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "012"
down_revision: Union[str, None] = "011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "transfer_tokens",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("token_hash", sa.String(length=64), unique=True, nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("state", sa.String(length=16), nullable=False, server_default="pending"),
        sa.Column("path", sa.String(length=1024), nullable=False),
        sa.Column("vault_root", sa.String(length=1024), nullable=False),
        sa.Column("overwrite", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("expected_fingerprint", postgresql.JSONB(), nullable=True),
        sa.Column("key_id", sa.Integer(), nullable=True),
        sa.Column("oauth_token_id", sa.Integer(), nullable=True),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("size", sa.BigInteger(), nullable=True),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("mime", sa.String(length=255), nullable=True),
        sa.ForeignKeyConstraint(
            ["key_id"], ["api_keys.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["oauth_token_id"], ["oauth_tokens.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "direction IN ('upload', 'download')",
            name="ck_transfer_tokens_direction",
        ),
        sa.CheckConstraint(
            "state IN ('pending', 'claimed', 'completed', 'consumed')",
            name="ck_transfer_tokens_state",
        ),
    )
    op.create_index("ix_transfer_tokens_expires_at", "transfer_tokens", ["expires_at"])
    op.create_index("ix_transfer_tokens_key_id", "transfer_tokens", ["key_id"])
    op.create_index(
        "ix_transfer_tokens_oauth_token_id", "transfer_tokens", ["oauth_token_id"]
    )
    op.create_index("ix_transfer_tokens_user_id", "transfer_tokens", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_transfer_tokens_user_id", table_name="transfer_tokens")
    op.drop_index("ix_transfer_tokens_oauth_token_id", table_name="transfer_tokens")
    op.drop_index("ix_transfer_tokens_key_id", table_name="transfer_tokens")
    op.drop_index("ix_transfer_tokens_expires_at", table_name="transfer_tokens")
    op.drop_table("transfer_tokens")
