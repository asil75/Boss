import aiosqlite
import asyncio

async def test_block():
    async with aiosqlite.connect("db.sqlite3") as db:
        # Добавим тестового пользователя
        await db.execute("""
            INSERT OR IGNORE INTO users (tg_id, role, phone, is_blocked)
            VALUES (?, ?, ?, ?)
        """, (123456789, 'shop', '+79991112233', 0))
        await db.commit()
        
        # Проверим
        cursor = await db.execute("SELECT tg_id, is_blocked FROM users WHERE tg_id=?", (123456789,))
        user = await cursor.fetchone()
        print(f"До блокировки: ID={user[0]}, Блокировка={user[1]}")
        
        # Заблокируем
        await db.execute("UPDATE users SET is_blocked=1 WHERE tg_id=?", (123456789,))
        await db.commit()
        
        cursor = await db.execute("SELECT tg_id, is_blocked FROM users WHERE tg_id=?", (123456789,))
        user = await cursor.fetchone()
        print(f"После блокировки: ID={user[0]}, Блокировка={user[1]}")
        
        # Разблокируем
        await db.execute("UPDATE users SET is_blocked=0 WHERE tg_id=?", (123456789,))
        await db.commit()

asyncio.run(test_block())