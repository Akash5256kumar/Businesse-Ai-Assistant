"""add inventory table

Revision ID: f3b9d2e1c7a8
Revises: e5a2c1f8b3d9
Create Date: 2026-05-19 00:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f3b9d2e1c7a8"
down_revision: Union[str, Sequence[str], None] = "e5a2c1f8b3d9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "inventory",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("product_name", sa.String(200), nullable=False),
        sa.Column("quantity", sa.Numeric(14, 3), nullable=False, server_default="0"),
        sa.Column("unit", sa.String(30), nullable=False, server_default="piece"),
        sa.Column("last_purchase_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("last_sale_price", sa.Numeric(14, 2), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_inventory_user_id", "inventory", ["user_id"])
    op.create_index("ix_inventory_product_name", "inventory", ["product_name"])


def downgrade() -> None:
    op.drop_index("ix_inventory_product_name", "inventory")
    op.drop_index("ix_inventory_user_id", "inventory")
    op.drop_table("inventory")
