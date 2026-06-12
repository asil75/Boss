from __future__ import annotations

from typing import Any

import aiosqlite

from app.schemas import UserOut
from app.services.orders import COMPLETED_STATUSES


async def stats(db: aiosqlite.Connection, user: UserOut) -> dict[str, Any]:
    if user.role == "shop":
        return await _shop_stats(db, user.tg_id)
    if user.role == "courier":
        return await _courier_stats(db, user.tg_id)
    return await _global_stats(db)


async def _shop_stats(db: aiosqlite.Connection, tg_id: int) -> dict[str, Any]:
    cur = await db.execute(
        """
        SELECT
            COUNT(*) total_orders,
            SUM(CASE WHEN status IN ('delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return') THEN 1 ELSE 0 END) completed_orders,
            SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) cancelled_orders,
            SUM(CASE WHEN paid_to_courier < 2 AND status IN ('delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return') THEN price ELSE 0 END) unpaid_amount
        FROM orders
        WHERE shop_tg_id=?
        """,
        (tg_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return {
        "total_orders": int(row[0] or 0),
        "completed_orders": int(row[1] or 0),
        "cancelled_orders": int(row[2] or 0),
        "unpaid_amount": round(float(row[3] or 0), 2),
    }


async def _courier_stats(db: aiosqlite.Connection, tg_id: int) -> dict[str, Any]:
    cur = await db.execute(
        """
        SELECT
            COUNT(*) total_deliveries,
            SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) delivered_orders,
            SUM(CASE WHEN status='failed_delivery' THEN 1 ELSE 0 END) failed_orders,
            SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) cancelled_orders
        FROM orders
        WHERE courier_tg_id=?
        """,
        (tg_id,),
    )
    row = await cur.fetchone()
    await cur.close()
    return {
        "total_deliveries": int(row[0] or 0),
        "delivered_orders": int(row[1] or 0),
        "failed_orders": int(row[2] or 0),
        "cancelled_orders": int(row[3] or 0),
    }


async def _global_stats(db: aiosqlite.Connection) -> dict[str, Any]:
    cur = await db.execute(
        """
        SELECT
            COUNT(*) total_orders,
            SUM(CASE WHEN status='new' THEN 1 ELSE 0 END) new_orders,
            SUM(CASE WHEN status IN ('taken', 'at_shop', 'on_delivery', 'at_client') THEN 1 ELSE 0 END) active_orders,
            SUM(CASE WHEN status IN ('delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return') THEN 1 ELSE 0 END) completed_orders,
            SUM(CASE WHEN status='cancelled' THEN 1 ELSE 0 END) cancelled_orders
        FROM orders
        """
    )
    row = await cur.fetchone()
    await cur.close()
    return {
        "total_orders": int(row[0] or 0),
        "new_orders": int(row[1] or 0),
        "active_orders": int(row[2] or 0),
        "completed_orders": int(row[3] or 0),
        "cancelled_orders": int(row[4] or 0),
    }
