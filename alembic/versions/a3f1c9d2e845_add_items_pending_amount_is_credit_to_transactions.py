"""add items, pending_amount, is_credit to transactions; drop quantity

Revision ID: a3f1c9d2e845
Revises: c4d7e8f9a2b1
Create Date: 2026-04-29 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a3f1c9d2e845"
down_revision: Union[str, Sequence[str], None] = "c4d7e8f9a2b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("transactions", sa.Column("pending_amount", sa.Numeric(14, 2), nullable=True))
    op.add_column(
        "transactions",
        sa.Column("is_credit", sa.Boolean(), nullable=False, server_default="false"),
    )
    op.add_column("transactions", sa.Column("items", sa.JSON(), nullable=True))
    op.drop_column("transactions", "quantity")


def downgrade() -> None:
    op.add_column("transactions", sa.Column("quantity", sa.Integer(), nullable=True))
    op.drop_column("transactions", "items")
    op.drop_column("transactions", "is_credit")
    op.drop_column("transactions", "pending_amount")
