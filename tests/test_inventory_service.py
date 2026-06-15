from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.inventory import Inventory
from app.services import inventory_service


class _ScalarResult:
    def __init__(self, values: list):
        self._values = values

    def scalars(self) -> "_ScalarResult":
        return self

    def all(self) -> list:
        return list(self._values)

    def scalar_one_or_none(self):
        if not self._values:
            return None
        if len(self._values) > 1:
            raise AssertionError("Expected at most one row")
        return self._values[0]


class _FakeSession:
    def __init__(self, *results: _ScalarResult):
        self._results = list(results)
        self.added: list[Inventory] = []
        self.flush_calls = 0

    async def execute(self, *_args, **_kwargs):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, item: Inventory) -> None:
        self.added.append(item)

    async def flush(self) -> None:
        self.flush_calls += 1


def _inventory(
    name: str,
    *,
    quantity: str = "100",
    unit: str = "kg",
    category: str | None = "rice",
    purchase_price: str | None = "60",
    sale_price: str | None = "80",
) -> Inventory:
    item = Inventory(
        user_id=1,
        product_name=name,
        quantity=Decimal(quantity),
        unit=unit,
        category=category,
        last_purchase_price=Decimal(purchase_price) if purchase_price is not None else None,
        last_sale_price=Decimal(sale_price) if sale_price is not None else None,
    )
    item.updated_at = datetime(2026, 6, 15, tzinfo=UTC)
    return item


def test_identity_match_score_is_strict_for_variant_skus() -> None:
    assert inventory_service._identity_match_score("ali baba rice", "ali baba") == 1.0
    assert inventory_service._identity_match_score("basmti rice", "basmati rice") > 0.0

    assert inventory_service._identity_match_score("1060 basmati rice", "basmati rice") == 0.0
    assert inventory_service._identity_match_score("banskathi rice", "basmati rice") == 0.0
    assert inventory_service._identity_match_score("delhi pasand delhi", "delhi pasand aabha") == 0.0


@pytest.mark.asyncio
async def test_find_product_catalog_matches_marks_variant_as_ambiguous() -> None:
    db = _FakeSession(_ScalarResult([_inventory("basmati rice")]))

    result = await inventory_service.find_product_catalog_matches(db, 1, "1060 basmati rice")

    assert result["needs_clarification"] is True
    assert result["product_not_found"] is False
    assert result["top_match_confidence"] < 0.80
    assert result["matches"][0]["product_name"] == "basmati rice"


@pytest.mark.asyncio
async def test_find_product_catalog_matches_rejects_generic_false_positive() -> None:
    db = _FakeSession(_ScalarResult([_inventory("amma sona mansoori rice")]))

    result = await inventory_service.find_product_catalog_matches(db, 1, "dosa rice")

    assert result["product_not_found"] is True
    assert result["matches"] == []


@pytest.mark.asyncio
async def test_get_recent_price_allows_generic_suffix_only_match() -> None:
    db = _FakeSession(_ScalarResult([_inventory("ali baba", sale_price="82")]))

    result = await inventory_service.get_recent_price(db, 1, "ali baba rice")

    assert result["found"] is True
    assert result["product_name"] == "ali baba"
    assert result["rate"] == 82.0
    assert result["source"] == "inventory"


@pytest.mark.asyncio
async def test_get_recent_price_does_not_autoselect_variant_product() -> None:
    db = _FakeSession(
        _ScalarResult([_inventory("basmati rice", sale_price="81")]),
        _ScalarResult([]),
    )

    result = await inventory_service.get_recent_price(db, 1, "1060 basmati rice")

    assert result["found"] is False
    assert result["ambiguous"] is True
    assert "basmati rice" in result["candidates"]


@pytest.mark.asyncio
async def test_adjust_stock_does_not_mutate_similar_existing_product() -> None:
    basmati = _inventory("basmati rice", quantity="100")
    db = _FakeSession(_ScalarResult([basmati]))

    await inventory_service.adjust_stock(
        db,
        1,
        "1060 basmati rice",
        Decimal("-5"),
        "kg",
        sale_price=Decimal("81"),
    )

    assert basmati.quantity == Decimal("100")
    assert len(db.added) == 1
    assert db.added[0].product_name == "1060 basmati rice"
    assert db.added[0].quantity == Decimal("0")
