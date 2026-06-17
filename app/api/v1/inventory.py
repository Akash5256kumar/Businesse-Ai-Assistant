from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth import get_current_user
from app.core.database import get_db
from app.models.user import User
from app.schemas.inventory import (
    ImportSummaryResponse,
    InventoryItemResponse,
    InventoryListResponse,
    InventoryUpsertRequest,
)
from fastapi import Query
from app.services import inventory_service

router = APIRouter(prefix="/api/v1/inventory", tags=["inventory"])


@router.get("/search", response_model=InventoryListResponse)
async def search_inventory(
    q: str = Query("", min_length=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InventoryListResponse:
    items = await inventory_service.search_inventory(db, current_user.id, q)
    return InventoryListResponse(items=items)


@router.get("/", response_model=InventoryListResponse)
async def list_inventory(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InventoryListResponse:
    return await inventory_service.list_inventory(db, current_user.id)


@router.post("/", response_model=InventoryItemResponse)
async def upsert_inventory(
    payload: InventoryUpsertRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> InventoryItemResponse:
    item = await inventory_service.upsert_inventory(db, current_user.id, payload)
    await db.commit()
    return item


@router.post("/import", response_model=ImportSummaryResponse)
async def import_inventory(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ImportSummaryResponse:
    filename = (file.filename or "").lower()
    if not (filename.endswith(".xlsx") or filename.endswith(".csv")):
        raise HTTPException(
            status_code=400,
            detail="Only .xlsx and .csv files are supported.",
        )
    content = await file.read()
    result = await inventory_service.import_inventory_from_file(
        db, current_user.id, content, filename
    )
    await db.commit()
    return result


@router.delete("/{item_id}", status_code=204)
async def delete_inventory(
    item_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    deleted = await inventory_service.delete_inventory_item(db, current_user.id, item_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Item not found")
    await db.commit()
