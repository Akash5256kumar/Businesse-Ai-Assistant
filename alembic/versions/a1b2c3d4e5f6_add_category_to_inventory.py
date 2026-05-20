"""add category to inventory

Revision ID: a1b2c3d4e5f6
Revises: f3b9d2e1c7a8
Create Date: 2026-05-20 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = "f3b9d2e1c7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("inventory", sa.Column("category", sa.String(100), nullable=True))
    op.create_index("ix_inventory_category", "inventory", ["category"])


def downgrade() -> None:
    op.drop_index("ix_inventory_category", "inventory")
    op.drop_column("inventory", "category")
