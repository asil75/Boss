import sqlite3
from datetime import datetime, timezone
from typing import AsyncIterator

import aiosqlite

from app.config import DB_PATH


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def get_db() -> AsyncIterator[aiosqlite.Connection]:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        await db.execute("PRAGMA foreign_keys=ON")
        yield db


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = sqlite3.Row
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE NOT NULL,
                role TEXT,
                phone TEXT,
                first_name TEXT,
                last_name TEXT,
                username TEXT,
                language_code TEXT,
                is_blocked INTEGER DEFAULT 0,
                created_at TEXT,
                updated_at TEXT
            );

            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shop_tg_id INTEGER,
                courier_tg_id INTEGER,
                from_address TEXT,
                shop_contact TEXT,
                to_address TEXT,
                to_apt TEXT,
                client_name TEXT,
                client_phone TEXT,
                price REAL,
                status TEXT,
                log TEXT,
                created_at TEXT,
                return_for INTEGER DEFAULT NULL,
                paid_to_courier INTEGER DEFAULT 0,
                paid_at TEXT DEFAULT NULL
            );

            CREATE TABLE IF NOT EXISTS courier_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                courier_tg_id INTEGER,
                message_id INTEGER,
                created_at TEXT
            );

            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                shop_tg_id INTEGER,
                courier_tg_id INTEGER,
                amount REAL,
                paid_at TEXT
            );
            """
        )

        for column_sql in (
            "ALTER TABLE users ADD COLUMN phone TEXT",
            "ALTER TABLE users ADD COLUMN first_name TEXT",
            "ALTER TABLE users ADD COLUMN last_name TEXT",
            "ALTER TABLE users ADD COLUMN username TEXT",
            "ALTER TABLE users ADD COLUMN language_code TEXT",
            "ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN created_at TEXT",
            "ALTER TABLE users ADD COLUMN updated_at TEXT",
            "ALTER TABLE orders ADD COLUMN return_for INTEGER DEFAULT NULL",
            "ALTER TABLE orders ADD COLUMN paid_to_courier INTEGER DEFAULT 0",
            "ALTER TABLE orders ADD COLUMN paid_at TEXT DEFAULT NULL",
        ):
            try:
                await db.execute(column_sql)
            except aiosqlite.OperationalError:
                pass

        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_shop_status ON orders(shop_tg_id, status)"
        )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_orders_courier_status ON orders(courier_tg_id, status)"
        )
        await db.execute("CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status)")
        await db.commit()
