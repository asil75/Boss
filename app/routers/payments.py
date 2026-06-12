from fastapi import APIRouter, Depends, Request

from app.db import get_db
from app.schemas import UserOut
from app.auth import telegram_user_from_request
from app.services.payments import (
    confirm_paid_by_courier,
    mark_all_payable_paid,
    mark_paid_by_shop,
    summary,
)
from app.services.users import upsert_user

router = APIRouter(prefix="/api/payments", tags=["payments"])


@router.post("/{order_id}/mark-paid")
async def mark_paid(order_id: int, request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    return await mark_paid_by_shop(db, order_id, user)


@router.post("/{order_id}/confirm")
async def confirm(order_id: int, request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    return await confirm_paid_by_courier(db, order_id, user)


@router.post("/pay-all")
async def pay_all(request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    count = await mark_all_payable_paid(db, user)
    return {"ok": True, "count": count}


@router.get("/summary")
async def get_summary(request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    return await summary(db, user)


async def current_user(request: Request, db) -> UserOut:
    telegram_user = telegram_user_from_request(request)
    return await upsert_user(
        db,
        int(telegram_user["id"]),
        first_name=telegram_user.get("first_name"),
        last_name=telegram_user.get("last_name"),
        username=telegram_user.get("username"),
        language_code=telegram_user.get("language_code"),
    )
