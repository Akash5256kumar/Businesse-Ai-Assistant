"""add user_type to users and location to businesses

Revision ID: b2d4f8a1c3e6
Revises: a3f1c9d2e845
Create Date: 2026-04-30 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2d4f8a1c3e6"
down_revision: Union[str, Sequence[str], None] = "a3f1c9d2e845"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "user_type",
            sa.String(20),
            nullable=False,
            server_default="business",
        ),
    )
    op.add_column(
        "businesses",
        sa.Column("location", sa.String(255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("businesses", "location")
    op.drop_column("users", "user_type")
