"""Support OAuth public clients for ChatGPT MCP connections.

Revision ID: 010
Revises: 009
Create Date: 2026-07-13
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "010"
down_revision: Union[str, None] = "009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("oauth_clients", "client_secret_hash", nullable=True)
    op.add_column(
        "oauth_clients",
        sa.Column(
            "token_endpoint_auth_method",
            sa.String(32),
            nullable=False,
            server_default="client_secret_post",
        ),
    )
    op.create_check_constraint(
        "ck_oauth_clients_auth_method_secret",
        "oauth_clients",
        "(token_endpoint_auth_method = 'none' AND client_secret_hash IS NULL) OR "
        "(token_endpoint_auth_method = 'client_secret_post' AND client_secret_hash IS NOT NULL)",
    )


def downgrade() -> None:
    # Public-client rows cannot satisfy the old NOT NULL secret constraint.
    # Removing them is safer than fabricating an unusable credential.
    op.execute(
        "DELETE FROM oauth_clients WHERE token_endpoint_auth_method = 'none'"
    )
    op.drop_constraint(
        "ck_oauth_clients_auth_method_secret", "oauth_clients", type_="check"
    )
    op.drop_column("oauth_clients", "token_endpoint_auth_method")
    op.alter_column("oauth_clients", "client_secret_hash", nullable=False)
