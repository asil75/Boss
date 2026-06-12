from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app.db import utc_now
from app.schemas import OrderCreateRequest, OrderOut, UserOut

ACTIVE_STATUSES = ("new", "taken", "at_shop", "on_delivery", "at_client", "failed_delivery")
COMPLETED_STATUSES = ("delivered", "failed_delivery", "cancelled_70_percent", "completed_with_return")
PAYABLE_STATUSES = COMPLETED_STATUSES
PAYMENT_STATUS_UNPAID = 0
PAYMENT_STATUS_MARKED_PAID = 1
PAYMENT_STATUS_CONFIRMED = 2

STATUS_TRANSITIONS = {
    "take": {"new": "taken"},
    "pickup_shop": {"taken": "at_shop"},
    "on_delivery": {"at_shop": "on_delivery"},
    "arrive_client": {"on_delivery": "at_client"},
    "finish": {"at_client": "delivered"},
    "finish_return": {"at_client": "delivered"},
    "client_not_home": {"at_client": "failed_delivery"},
}


def row_to_order(row: aiosqlite.Row | dict[str, Any]) -> OrderOut:
    return OrderOut(
        id=int(row["id"]),
        shop_tg_id=row["shop_tg_id"],
        courier_tg_id=row["courier_tg_id"],
        from_address=row["from_address"],
        shop_contact=row["shop_contact"],
        to_address=row["to_address"],
        to_apt=row["to_apt"],
        client_name=row["client_name"],
        client_phone=row["client_phone"],
        price=float(row["price"] or 0),
        status=row["status"],
        log=row["log"],
        created_at=row["created_at"],
        return_for=row["return_for"],
        paid_to_courier=int(row["paid_to_courier"] or 0),
        paid_at=row["paid_at"],
    )


async def create_order(
    db: aiosqlite.Connection,
    user: UserOut,
    payload: OrderCreateRequest,
    *,
    return_for: int | None = None,
) -> OrderOut:
    if user.role != "shop" and not user.is_blocked:
        raise PermissionError("Фақат магазин заказ яратиши мумкин.")
    if user.is_blocked:
        raise PermissionError("Сиз блоклангансиз.")

    now = utc_now()
    status = "completed_with_return" if return_for else "new"
    log = f"[{now}] Заказ яратилди."
    cur = await db.execute(
        """
        INSERT INTO orders (
            shop_tg_id, from_address, shop_contact, to_address, to_apt,
            client_name, client_phone, price, status, log, created_at,
            return_for, paid_to_courier
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user.tg_id,
            payload.from_address,
            payload.shop_contact,
            payload.to_address,
            payload.to_apt,
            payload.client_name,
            payload.client_phone,
            payload.price,
            status,
            log,
            now,
            return_for,
            PAYMENT_STATUS_UNPAID,
        ),
    )
    await db.commit()
    row = await get_order_row(db, int(cur.lastrowid))
    return row_to_order(row)


async def list_orders(
    db: aiosqlite.Connection,
    user: UserOut,
    *,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[OrderOut]:
    where = []
    params: list[Any] = []
    if user.role == "shop":
        where.append("shop_tg_id=?")
        params.append(user.tg_id)
    elif user.role == "courier":
        where.append("courier_tg_id=?")
        params.append(user.tg_id)
    if status:
        where.append("status=?")
        params.append(status)

    sql = "SELECT * FROM orders"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return [row_to_order(row) for row in rows]


async def get_order(db: aiosqlite.Connection, order_id: int) -> OrderOut | None:
    row = await get_order_row(db, order_id)
    return row_to_order(row) if row else None


async def get_order_row(db: aiosqlite.Connection, order_id: int) -> aiosqlite.Row | None:
    cur = await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))
    row = await cur.fetchone()
    await cur.close()
    return row


async def take_order(db: aiosqlite.Connection, order_id: int, user: UserOut) -> OrderOut:
    if user.role != "courier":
        raise PermissionError("Заказни фақат курьер олиши мумкин.")
    order = await get_order(db, order_id)
    if not order or order.status != "new":
        raise PermissionError("Бу заказ ҳозир олиш учун мавжуд эмас.")

    await _update_order(
        db,
        order_id,
        status="taken",
        courier_tg_id=user.tg_id,
        log_add=f"Курьер {user.tg_id} заказни олди.",
    )
    updated = await get_order(db, order_id)
    assert updated
    return updated


async def change_order_status(
    db: aiosqlite.Connection,
    order_id: int,
    user: UserOut,
    action: str,
) -> OrderOut:
    order = await get_order(db, order_id)
    if not order:
        raise LookupError("Заказ топилмади.")
    _ensure_order_access(user, order)

    if action == "cancel":
        return await cancel_order(db, order, user)

    expected = STATUS_TRANSITIONS.get(action)
    if not expected:
        raise ValueError("Нотўғри action.")
    if order.status not in expected:
        raise PermissionError(f"Бу action учун ҳозирги статус тўғри эмас: {order.status}")

    await _update_order(
        db,
        order_id,
        status=expected[order.status],
        log_add=f"Статус ўзгарди: {action}.",
    )
    updated = await get_order(db, order_id)
    assert updated
    return updated


async def cancel_order(
    db: aiosqlite.Connection,
    order: OrderOut,
    user: UserOut,
) -> OrderOut:
    if user.role == "courier" and order.status in ("taken", "at_shop", "on_delivery", "at_client"):
        paid_status = PAYMENT_STATUS_UNPAID
        new_status = "new" if order.status in ("taken", "at_shop") else "cancelled"
    elif user.role == "shop" and order.status in ("new", "taken", "at_shop"):
        paid_status = PAYMENT_STATUS_UNPAID
        new_status = "cancelled"
    elif user.role == "shop" and order.status in ("on_delivery", "at_client"):
        paid_status = PAYMENT_STATUS_UNPAID
        new_status = "cancelled_70_percent"
    else:
        raise PermissionError("Бу заказни бекор қилиб бўлмайди.")

    await _update_order(
        db,
        order.id,
        status=new_status,
        courier_tg_id=0 if user.role == "courier" else None,
        paid=paid_status,
        log_add=f"Заказ бекор қилинди: {user.role}.",
    )
    updated = await get_order(db, order.id)
    assert updated
    return updated


async def _update_order(
    db: aiosqlite.Connection,
    order_id: int,
    *,
    status: str | None = None,
    courier_tg_id: int | None = None,
    paid: int | None = None,
    log_add: str | None = None,
) -> None:
    updates = []
    params: list[Any] = []
    if status is not None:
        updates.append("status=?")
        params.append(status)
    if courier_tg_id is not None:
        updates.append("courier_tg_id=?")
        params.append(courier_tg_id)
    if paid is not None:
        updates.append("paid_to_courier=?")
        params.append(paid)
        updates.append("paid_at=?")
        params.append(utc_now())
    if log_add:
        updates.append("log=COALESCE(log, '') || ?")
        params.append(f"\n[{utc_now()}] {log_add}")

    if not updates:
        return
    params.append(order_id)
    await db.execute(
        f"UPDATE orders SET {', '.join(updates)} WHERE id=?",
        params,
    )
    await db.commit()


def _ensure_order_access(user: UserOut, order: OrderOut) -> None:
    if user.role == "shop" and order.shop_tg_id != user.tg_id:
        raise PermissionError("Бу сизнинг заказингиз эмас.")
    if user.role == "courier" and order.courier_tg_id != user.tg_id:
        raise PermissionError("Бу сизга бириктирилган заказ эмас.")
