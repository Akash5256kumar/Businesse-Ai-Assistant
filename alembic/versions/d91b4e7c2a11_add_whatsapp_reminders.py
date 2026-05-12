"""add whatsapp reminder settings and logs

Revision ID: d91b4e7c2a11
Revises: b2d4f8a1c3e6
Create Date: 2026-05-12 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d91b4e7c2a11"
down_revision: Union[str, Sequence[str], None] = "b2d4f8a1c3e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "whatsapp_reminders_enabled",
            sa.Boolean(),
            nullable=False,
            server_default="true",
        ),
    )

    op.create_table(
        "reminder_logs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("customer_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False, server_default="whatsapp"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="sent"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("provider", sa.String(length=50), nullable=True),
        sa.Column("provider_message_id", sa.String(length=255), nullable=True),
        sa.Column("provider_response", sa.JSON(), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(["customer_id"], ["customers.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_reminder_logs_customer_id", "reminder_logs", ["customer_id"])
    op.create_index("ix_reminder_logs_id", "reminder_logs", ["id"])
    op.create_index("ix_reminder_logs_user_id", "reminder_logs", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_reminder_logs_user_id", table_name="reminder_logs")
    op.drop_index("ix_reminder_logs_id", table_name="reminder_logs")
    op.drop_index("ix_reminder_logs_customer_id", table_name="reminder_logs")
    op.drop_table("reminder_logs")
    op.drop_column("businesses", "whatsapp_reminders_enabled")
