"""create otp codes table

Revision ID: f1c3e7a92b44
Revises: 8c2f9d8b7a1c
Create Date: 2026-04-24 18:45:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "f1c3e7a92b44"
down_revision: Union[str, Sequence[str], None] = "8c2f9d8b7a1c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "otp_codes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=20), nullable=False),
        sa.Column("destination", sa.String(length=255), nullable=False),
        sa.Column("purpose", sa.String(length=50), nullable=False),
        sa.Column("code_hash", sa.String(length=64), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_otp_codes_id"), "otp_codes", ["id"], unique=False)
    op.create_index(
        op.f("ix_otp_codes_channel"), "otp_codes", ["channel"], unique=False
    )
    op.create_index(
        op.f("ix_otp_codes_destination"), "otp_codes", ["destination"], unique=False
    )
    op.create_index(
        op.f("ix_otp_codes_purpose"), "otp_codes", ["purpose"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_otp_codes_purpose"), table_name="otp_codes")
    op.drop_index(op.f("ix_otp_codes_destination"), table_name="otp_codes")
    op.drop_index(op.f("ix_otp_codes_channel"), table_name="otp_codes")
    op.drop_index(op.f("ix_otp_codes_id"), table_name="otp_codes")
    op.drop_table("otp_codes")
