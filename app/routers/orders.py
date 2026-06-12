from fastapi import APIRouter, Depends, Query, Request
from app.db import get_db
from app.schemas import OrderCreateRequest, OrderOut, OrderStatusRequest, UserOut
from app.auth import telegram_user_from_request
from app.services.orders import (
    change_order_status,
    create_order,
    get_order,
    list_orders,
    take_order,
)
from app.services.users import upsert_user

router = APIRouter(prefix="/api/orders", tags=["orders"])


@router.get("", response_model=list[OrderOut])
async def get_orders(
    request: Request,
    db=Depends(get_db),
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    user = await current_user(request, db)
    return await list_orders(db, user, status=status, limit=limit, offset=offset)


@router.post("", response_model=OrderOut)
async def post_order(payload: OrderCreateRequest, request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    return await create_order(db, user, payload)


@router.patch("/{order_id}/take", response_model=OrderOut)
async def post_take(order_id: int, request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    return await take_order(db, order_id, user)


@router.patch("/{order_id}/status", response_model=OrderOut)
async def post_status(order_id: int, payload: OrderStatusRequest, request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    return await change_order_status(db, order_id, user, payload.action)


@router.get("/{order_id}", response_model=OrderOut)
async def get_one_order(order_id: int, request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    order = await get_order(db, order_id)
    if not order:
        raise LookupError("Заказ топилмади.")
    if user.role == "shop" and order.shop_tg_id != user.tg_id:
        raise PermissionError("Бу сизнинг заказингиз эмас.")
    if user.role == "courier" and order.courier_tg_id != user.tg_id:
        raise PermissionError("Бу сизга бириктирилган заказ эмас.")
    return order


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
