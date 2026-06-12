from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import aiosqlite

from app.db import utc_now
from app.schemas import UserOut

ACTIVE_STATUSES = ("new", "taken", "at_shop", "on_delivery", "at_client", "failed_delivery")


def _row_to_user(row: aiosqlite.Row | dict[str, Any]) -> UserOut:
    return UserOut(
        tg_id=int(row["tg_id"]),
        role=row["role"],
        phone=row["phone"],
        first_name=row.get("first_name") if hasattr(row, "get") else row["first_name"],
        last_name=row.get("last_name") if hasattr(row, "get") else row["last_name"],
        username=row.get("username") if hasattr(row, "get") else row["username"],
        language_code=row.get("language_code") if hasattr(row, "get") else row["language_code"],
        is_blocked=bool(row["is_blocked"]),
    )


async def upsert_user(
    db: aiosqlite.Connection,
    tg_id: int,
    *,
    first_name: str | None = None,
    last_name: str | None = None,
    username: str | None = None,
    language_code: str | None = None,
) -> UserOut:
    await db.execute(
        """
        INSERT INTO users (tg_id, first_name, last_name, username, language_code)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET
            first_name=excluded.first_name,
            last_name=excluded.last_name,
            username=excluded.username,
            language_code=excluded.language_code,
            updated_at=?
        """,
        (tg_id, first_name, last_name, username, language_code, utc_now()),
    )
    await db.commit()
    row = await _get_user_row(db, tg_id)
    return _row_to_user(row)


async def get_user(db: aiosqlite.Connection, tg_id: int) -> UserOut | None:
    row = await _get_user_row(db, tg_id)
    return _row_to_user(row) if row else None


async def _get_user_row(db: aiosqlite.Connection, tg_id: int) -> aiosqlite.Row | None:
    cur = await db.execute("SELECT * FROM users WHERE tg_id=?", (tg_id,))
    row = await cur.fetchone()
    await cur.close()
    return row


async def set_role(db: aiosqlite.Connection, tg_id: int, role: str) -> UserOut:
    active_count = await _count_active_orders(db, tg_id)
    if active_count:
        raise PermissionError("Фаол заказ бор бўлса, роль ўзгартириб бўлмайди.")

    await db.execute(
        """
        INSERT INTO users (tg_id, role)
        VALUES (?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role, updated_at=?
        """,
        (tg_id, role, utc_now()),
    )
    await db.commit()
    row = await _get_user_row(db, tg_id)
    return _row_to_user(row)


async def set_phone(db: aiosqlite.Connection, tg_id: int, phone: str) -> UserOut:
    await db.execute(
        """
        INSERT INTO users (tg_id, phone)
        VALUES (?, ?)
        ON CONFLICT(tg_id) DO UPDATE SET phone=excluded.phone, updated_at=?
        """,
        (tg_id, phone, utc_now()),
    )
    await db.commit()
    row = await _get_user_row(db, tg_id)
    return _row_to_user(row)


async def set_blocked(db: aiosqlite.Connection, tg_id: int, is_blocked: bool) -> UserOut:
    await db.execute(
        "UPDATE users SET is_blocked=?, updated_at=? WHERE tg_id=?",
        (1 if is_blocked else 0, utc_now(), tg_id),
    )
    await db.commit()
    row = await _get_user_row(db, tg_id)
    if not row:
        raise LookupError("Фойдаланувчи топилмади.")
    return _row_to_user(row)


async def list_users(
    db: aiosqlite.Connection,
    *,
    role: str | None = None,
    is_blocked: bool | None = None,
    limit: int = 50,
    offset: int = 0,
) -> list[UserOut]:
    where = []
    params: list[Any] = []
    if role:
        where.append("role=?")
        params.append(role)
    if is_blocked is not None:
        where.append("is_blocked=?")
        params.append(1 if is_blocked else 0)
    sql = "SELECT * FROM users"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    cur = await db.execute(sql, params)
    rows = await cur.fetchall()
    await cur.close()
    return [_row_to_user(row) for row in rows]


async def _count_active_orders(db: aiosqlite.Connection, tg_id: int) -> int:
    placeholders = ", ".join("?" for _ in ACTIVE_STATUSES)
    cur = await db.execute(
        f"""
        SELECT COUNT(*) FROM orders
        WHERE (courier_tg_id=? OR shop_tg_id=?) AND status IN ({placeholders})
        """,
        (tg_id, tg_id, *ACTIVE_STATUSES),
    )
    count = int((await cur.fetchone())[0])
    await cur.close()
    return count
