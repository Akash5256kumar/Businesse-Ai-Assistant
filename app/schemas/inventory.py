from __future__ import annotations

from pydantic import BaseModel, field_validator


class InventoryItemResponse(BaseModel):
    id: int
    category: str | None = None
    product_name: str
    quantity: float
    unit: str
    last_purchase_price: float | None = None
    last_sale_price: float | None = None


class InventoryListResponse(BaseModel):
    items: list[InventoryItemResponse] = []


class InventoryUpsertRequest(BaseModel):
    category: str | None = None
    product_name: str
    quantity: float
    unit: str = "piece"
    last_purchase_price: float | None = None
    last_sale_price: float | None = None

    @field_validator("category")
    @classmethod
    def validate_category(cls, v: str | None) -> str | None:
        if v is not None:
            v = v.strip().lower()
            return v if v else None
        return None

    @field_validator("product_name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        v = v.strip().lower()
        if not v:
            raise ValueError("product_name cannot be empty")
        return v

    @field_validator("quantity")
    @classmethod
    def validate_qty(cls, v: float) -> float:
        if v < 0:
            raise ValueError("quantity cannot be negative")
        return v


class InventoryAdjustRequest(BaseModel):
    product_name: str
    delta: float        # positive = add stock, negative = reduce stock
    unit: str = "piece"
    price: float | None = None


class ImportRowResult(BaseModel):
    row: int
    product_name: str
    status: str          # "imported" | "updated" | "skipped"
    reason: str | None = None


class ImportSummaryResponse(BaseModel):
    total_rows: int
    imported: int
    updated: int
    skipped: int
    rows: list[ImportRowResult] = []
