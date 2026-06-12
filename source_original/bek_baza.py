import aiosqlite
import asyncio

async def check_db():
    async with aiosqlite.connect("db.sqlite3") as db:
        # Проверим структуру таблицы users
        cursor = await db.execute("PRAGMA table_info(users)")
        columns = await cursor.fetchall()
        print("Структура таблицы users:")
        for col in columns:
            print(f"  {col[1]} - {col[2]}")
        
        # Проверим есть ли поле is_blocked
        has_is_blocked = any("is_blocked" in col for col in columns)
        print(f"\nПоле is_blocked существует: {has_is_blocked}")
        
        # Посмотрим всех пользователей
        cursor = await db.execute("SELECT tg_id, role, phone, is_blocked FROM users")
        users = await cursor.fetchall()
        print(f"\nВсего пользователей: {len(users)}")
        for user in users:
            print(f"ID: {user[0]}, Роль: {user[1]}, Телефон: {user[2]}, Блокировка: {user[3]}")

asyncio.run(check_db())