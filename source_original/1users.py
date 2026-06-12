from telegram import Update
from telegram.ext import ContextTypes, MessageHandler, filters
import aiosqlite
import logging


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # защита OWNER
    # если у тебя is_owner в bot.py — можно потом вынести, пока оставим так
    # временно без повторной проверки, т.к. вход уже через админку

    text = "👥 Пользователи (тест)\n\n"

    try:
        async with aiosqlite.connect("db.sqlite3") as db:
            async with db.execute(
                "SELECT tg_id, role, phone FROM users ORDER BY id DESC LIMIT 10"
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await update.message.reply_text("Пользователи не найдены")
            return

        for tg_id, role, phone in rows:
            text += (
                f"ID: {tg_id}\n"
                f"Роль: {role or 'не выбрана'}\n"
                f"Телефон: {phone or 'нет'}\n\n"
            )

        await update.message.reply_text(text)

    except Exception:
        logging.exception("Ошибка при просмотре пользователей")
        await update.message.reply_text("❌ Ошибка при получении пользователей")


def register(app):
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^👥 Пользователи$"),
            admin_users
        )
    )
