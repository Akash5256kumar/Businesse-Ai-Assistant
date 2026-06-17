"""add notification logs table

Revision ID: c1a8f4e2b9d3
Revises: b7f9c2a4d6e1
Create Date: 2026-06-17 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1a8f4e2b9d3"
down_revision: Union[str, Sequence[str], None] = "b7f9c2a4d6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "notification_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("data", sa.JSON(), nullable=True),
        sa.Column(
            "is_read",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "sent_at",
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
    )
    op.create_index("ix_notification_logs_id", "notification_logs", ["id"])
    op.create_index("ix_notification_logs_user_id", "notification_logs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_notification_logs_user_id", table_name="notification_logs")
    op.drop_index("ix_notification_logs_id", table_name="notification_logs")
    op.drop_table("notification_logs")
