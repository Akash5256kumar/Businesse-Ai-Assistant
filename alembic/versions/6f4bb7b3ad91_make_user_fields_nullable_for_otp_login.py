"""make user fields nullable for otp login

Revision ID: 6f4bb7b3ad91
Revises: f1c3e7a92b44
Create Date: 2026-04-24 19:05:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "6f4bb7b3ad91"
down_revision: Union[str, Sequence[str], None] = "f1c3e7a92b44"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column("users", "full_name", existing_type=sa.String(length=255), nullable=True)
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=True)
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.String(length=255),
        nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "users",
        "hashed_password",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.alter_column("users", "email", existing_type=sa.String(length=255), nullable=False)
    op.alter_column(
        "users",
        "full_name",
        existing_type=sa.String(length=255),
        nullable=False,
    )
