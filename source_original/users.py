from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, CallbackQueryHandler, filters
import aiosqlite
import logging


DB_PATH = "db.sqlite3"


async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = "👥 Пользователи\n\n"

    try:
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT id, tg_id, role, phone, is_blocked FROM users ORDER BY id DESC LIMIT 10"
            ) as cursor:
                rows = await cursor.fetchall()

        if not rows:
            await update.message.reply_text("Пользователи не найдены")
            return

        for uid, tg_id, role, phone, is_blocked in rows:
            status = "⛔ Заблокирован" if is_blocked else "✅ Активен"

            keyboard = [[
                InlineKeyboardButton(
                    "🚫 Заблокировать" if not is_blocked else "✅ Разблокировать",
                    callback_data=f"toggle_block:{tg_id}"
                )
            ]]

            text_user = (
                f"ID: {tg_id}\n"
                f"Роль: {role or 'не выбрана'}\n"
                f"Телефон: {phone or 'нет'}\n"
                f"Статус: {status}"
            )

            await update.message.reply_text(
                text_user,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )

    except Exception:
        logging.exception("Ошибка при просмотре пользователей")
        await update.message.reply_text("❌ Ошибка при получении пользователей")


async def toggle_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    tg_id = int(query.data.split(":")[1])

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "SELECT is_blocked FROM users WHERE tg_id=?",
            (tg_id,)
        )
        row = await cur.fetchone()

        if not row:
            await query.message.reply_text("❌ Пользователь не найден")
            return

        new_status = 0 if row[0] else 1

        await db.execute(
            "UPDATE users SET is_blocked=? WHERE tg_id=?",
            (new_status, tg_id)
        )
        await db.commit()

    await query.message.reply_text(
        "✅ Статус пользователя обновлён"
    )


def register(app):
    app.add_handler(
        MessageHandler(
            filters.TEXT & filters.Regex("^👥 Пользователи$"),
            admin_users
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            toggle_block,
            pattern="^toggle_block:"
        )
    )
