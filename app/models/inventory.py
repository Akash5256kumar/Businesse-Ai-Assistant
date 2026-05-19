from __future__ import annotations

from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.mixins import TimestampMixin


class Inventory(Base, TimestampMixin):
    __tablename__ = "inventory"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    product_name: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(14, 3), nullable=False, default=Decimal("0"), server_default="0")
    unit: Mapped[str] = mapped_column(String(30), nullable=False, default="piece", server_default="piece")
    last_purchase_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)
    last_sale_price: Mapped[Decimal | None] = mapped_column(Numeric(14, 2), nullable=True)

    def __repr__(self) -> str:
        return f"<Inventory user={self.user_id} product={self.product_name} qty={self.quantity}>"
