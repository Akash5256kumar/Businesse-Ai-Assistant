"""add device tokens table

Revision ID: b7f9c2a4d6e1
Revises: a1b2c3d4e5f6
Create Date: 2026-06-15 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7f9c2a4d6e1"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "device_tokens",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("token", sa.String(length=512), nullable=False),
        sa.Column("platform", sa.String(length=20), nullable=True),
        sa.Column("device_id", sa.String(length=255), nullable=True),
        sa.Column("app_version", sa.String(length=50), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token"),
    )
    op.create_index("ix_device_tokens_id", "device_tokens", ["id"])
    op.create_index("ix_device_tokens_user_id", "device_tokens", ["user_id"])
    op.create_index("ix_device_tokens_token", "device_tokens", ["token"])
    op.create_index("ix_device_tokens_device_id", "device_tokens", ["device_id"])


def downgrade() -> None:
    op.drop_index("ix_device_tokens_device_id", table_name="device_tokens")
    op.drop_index("ix_device_tokens_token", table_name="device_tokens")
    op.drop_index("ix_device_tokens_user_id", table_name="device_tokens")
    op.drop_index("ix_device_tokens_id", table_name="device_tokens")
    op.drop_table("device_tokens")
