from __future__ import annotations

from typing import Any

import aiosqlite

from app.db import utc_now
from app.schemas import OrderOut, UserOut
from app.services.orders import (
    COMPLETED_STATUSES,
    PAYMENT_STATUS_CONFIRMED,
    PAYMENT_STATUS_MARKED_PAID,
    PAYMENT_STATUS_UNPAID,
    get_order,
)


def _amount_for(order: OrderOut) -> float:
    price = float(order.price or 0)
    return round(price * 0.70, 2) if order.status == "cancelled_70_percent" else price


async def mark_paid_by_shop(db: aiosqlite.Connection, order_id: int, user: UserOut) -> OrderOut:
    order = await get_order(db, order_id)
    if not order:
        raise LookupError("Заказ топилмади.")
    if order.shop_tg_id != user.tg_id:
        raise PermissionError("Фақат магазин эгаси тўлов белгилай олади.")
    if order.status not in COMPLETED_STATUSES:
        raise PermissionError("Тўлов фақат якунланган заказлар учун.")
    if order.paid_to_courier >= PAYMENT_STATUS_MARKED_PAID:
        raise PermissionError("Бу заказ аллақачон тўланган деб белгиланган.")

    await db.execute(
        """
        UPDATE orders
        SET paid_to_courier=?, paid_at=?, log=COALESCE(log, '') || ?
        WHERE id=?
        """,
        (PAYMENT_STATUS_MARKED_PAID, utc_now(), f"\n[{utc_now()}] Магазин тўловни бошлади.", order_id),
    )
    await db.commit()
    updated = await get_order(db, order_id)
    assert updated
    return updated


async def confirm_paid_by_courier(db: aiosqlite.Connection, order_id: int, user: UserOut) -> OrderOut:
    order = await get_order(db, order_id)
    if not order:
        raise LookupError("Заказ топилмади.")
    if order.courier_tg_id != user.tg_id:
        raise PermissionError("Фақат курьер тўловни тасдиқлай олади.")
    if order.paid_to_courier != PAYMENT_STATUS_MARKED_PAID:
        raise PermissionError("Тўлов аввал магазин томонидан белгиланиши керак.")

    await db.execute(
        """
        UPDATE orders
        SET paid_to_courier=?, paid_at=?, log=COALESCE(log, '') || ?
        WHERE id=?
        """,
        (PAYMENT_STATUS_CONFIRMED, utc_now(), f"\n[{utc_now()}] Курьер тўловни тасдиқлади.", order_id),
    )
    await db.commit()
    updated = await get_order(db, order_id)
    assert updated
    return updated


async def mark_all_payable_paid(db: aiosqlite.Connection, user: UserOut) -> int:
    if user.role != "shop":
        raise PermissionError("Массовая тўлов фақат магазин учун.")
    placeholders = ", ".join("?" for _ in COMPLETED_STATUSES)
    cur = await db.execute(
        f"""
        SELECT id FROM orders
        WHERE shop_tg_id=? AND paid_to_courier < ? AND status IN ({placeholders})
        """,
        (user.tg_id, PAYMENT_STATUS_CONFIRMED, *COMPLETED_STATUSES),
    )
    rows = await cur.fetchall()
    await cur.close()
    for (order_id,) in rows:
        order = await get_order(db, order_id)
        assert order
        await mark_paid_by_shop(db, order_id, user)
    return len(rows)


async def summary(db: aiosqlite.Connection, user: UserOut) -> dict[str, Any]:
    if user.role == "shop":
        cur = await db.execute(
            f"""
            SELECT
                SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END)
            FROM orders
            WHERE shop_tg_id=? AND paid_to_courier=? AND status IN ({', '.join('?' for _ in COMPLETED_STATUSES)})
            """,
            (user.tg_id, PAYMENT_STATUS_CONFIRMED, *COMPLETED_STATUSES),
        )
        paid_row = await cur.fetchone()
        await cur.close()

        cur = await db.execute(
            f"""
            SELECT
                SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END)
            FROM orders
            WHERE shop_tg_id=? AND paid_to_courier < ? AND status IN ({', '.join('?' for _ in COMPLETED_STATUSES)})
            """,
            (user.tg_id, PAYMENT_STATUS_CONFIRMED, *COMPLETED_STATUSES),
        )
        unpaid_row = await cur.fetchone()
        await cur.close()
        return {
            "paid": round(float(paid_row[0] or 0), 2),
            "unpaid": round(float(unpaid_row[0] or 0), 2),
        }

    if user.role == "courier":
        cur = await db.execute(
            f"""
            SELECT
                SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END)
            FROM orders
            WHERE courier_tg_id=? AND status IN ({', '.join('?' for _ in COMPLETED_STATUSES)})
            """,
            (user.tg_id, *COMPLETED_STATUSES),
        )
        expected_row = await cur.fetchone()
        await cur.close()

        cur = await db.execute(
            f"""
            SELECT
                SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END)
            FROM orders
            WHERE courier_tg_id=? AND paid_to_courier=?
            """,
            (user.tg_id, PAYMENT_STATUS_CONFIRMED),
        )
        paid_row = await cur.fetchone()
        await cur.close()
        expected = round(float(expected_row[0] or 0), 2)
        paid = round(float(paid_row[0] or 0), 2)
        return {
            "expected": expected,
            "paid": paid,
            "unpaid": round(expected - paid, 2),
        }

    return {"paid": 0, "unpaid": 0, "expected": 0}
