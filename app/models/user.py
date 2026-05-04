from __future__ import annotations

from sqlalchemy import Boolean, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.mixins import TimestampMixin


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True, nullable=True, index=True)
    phone_number: Mapped[str | None] = mapped_column(String(20), unique=True, nullable=True)
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="true")
    # "business" for shop owners, "customer" for buyers
    user_type: Mapped[str] = mapped_column(String(20), nullable=False, default="business", server_default="business")

    business: Mapped["Business"] = relationship(
        "Business", back_populates="owner", uselist=False
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} phone={self.phone_number}>"
