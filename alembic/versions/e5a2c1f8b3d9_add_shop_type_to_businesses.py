"""add shop_type to businesses

Revision ID: e5a2c1f8b3d9
Revises: d91b4e7c2a11
Create Date: 2026-05-19 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e5a2c1f8b3d9"
down_revision: Union[str, Sequence[str], None] = "d91b4e7c2a11"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "businesses",
        sa.Column(
            "shop_type",
            sa.String(50),
            nullable=False,
            server_default="general",
        ),
    )


def downgrade() -> None:
    op.drop_column("businesses", "shop_type")
