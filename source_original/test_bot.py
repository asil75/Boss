import time
import logging
import urllib.parse
import aiosqlite
from datetime import datetime
from typing import Optional

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand,
    BotCommandScopeChat, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove
)
from telegram.ext import (
    ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, ConversationHandler, filters, TypeHandler, ApplicationHandlerStop
)

# Импорт наших исправленных модулей
import config
from users import register_users_handlers
from admin import register_admin_handlers

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("delivery_bot")

# FSM states
ADDRESS_FROM, CONTACT_SHOP, ADDRESS_CLIENT, CLIENT_PHONE, CLIENT_NAME, DELIVERY_PRICE = range(6)

# Statuses
PAYMENT_STATUS_UNPAID = 0
PAYMENT_STATUS_MARKED_PAID = 1
PAYMENT_STATUS_CONFIRMED = 2

# === DATABASE ===
async def init_db(app):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER UNIQUE,
            role TEXT, phone TEXT, is_blocked INTEGER DEFAULT 0)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shop_tg_id INTEGER, courier_tg_id INTEGER,
            from_address TEXT, shop_contact TEXT, to_address TEXT, to_apt TEXT,
            client_name TEXT, client_phone TEXT, price REAL, status TEXT, log TEXT,
            created_at TEXT, return_for INTEGER, paid_to_courier INTEGER DEFAULT 0, paid_at TEXT)""")
        await db.execute("""CREATE TABLE IF NOT EXISTS courier_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER, 
            courier_tg_id INTEGER, message_id INTEGER, created_at TEXT)""")
        await db.commit()
    logger.info("Database initialized.")

# === HELPERS ===
async def get_role(tg_id: int) -> Optional[str]:
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def set_role(tg_id: int, role: str):
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("INSERT INTO users (tg_id, role) VALUES (?, ?) ON CONFLICT(tg_id) DO UPDATE SET role=?", (tg_id, role, role))
        await db.commit()

# === HANDLERS ===
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    async with aiosqlite.connect(config.DB_PATH) as db:
        cur = await db.execute("SELECT phone FROM users WHERE tg_id=?", (user.id,))
        row = await cur.fetchone()
    
    if not row or not row[0]:
        kb = [[KeyboardButton("📱 Поделиться номером", request_contact=True)]]
        await update.message.reply_text("Привет! Подтвердите номер телефона для начала:", 
                                       reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
        return

    role = await get_role(user.id)
    if not role:
        kb = [[InlineKeyboardButton("🏪 Магазин", callback_data="role_shop"),
               InlineKeyboardButton("🛵 Курьер", callback_data="role_courier")]]
        await update.message.reply_text("Выберите вашу роль:", reply_markup=InlineKeyboardMarkup(kb))
    else:
        await show_main_menu(update, context)

async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    contact = update.message.contact
    if contact.user_id != update.effective_user.id:
        await update.message.reply_text("Ошибка: это не ваш номер.")
        return
    async with aiosqlite.connect(config.DB_PATH) as db:
        await db.execute("INSERT INTO users (tg_id, phone) VALUES (?, ?) ON CONFLICT(tg_id) DO UPDATE SET phone=?", 
                         (update.effective_user.id, contact.phone_number, contact.phone_number))
        await db.commit()
    await start(update, context)

async def set_role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    role = query.data.split("_")[1]
    await set_role(query.from_user.id, role)
    await query.answer(f"Вы выбрали роль: {role}")
    await show_main_menu(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tg_id = update.effective_user.id
    role = await get_role(tg_id)
    
    if role == "shop":
        kb = [["➕ Новый заказ", "📦 Активные заказы"], ["📊 Статистика", "⚙️ Профиль"]]
    else:
        kb = [["📦 Взять заказ", "🗺️ Мои доставки"], ["📊 Статистика", "⚙️ Профиль"]]
    
    text = f"🏠 Главное меню ({'Магазин' if role == 'shop' else 'Курьер'})"
    if update.callback_query:
        await update.callback_query.message.reply_text(text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))
    else:
        await update.message.reply_text(text, reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

# === BOT LAUNCH ===
def main():
    app = ApplicationBuilder().token(config.BOT_TOKEN).build()
    app.post_init = init_db

    # Register handlers from plugins
    register_users_handlers(app)
    register_admin_handlers(app)

    # Base handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(CallbackQueryHandler(set_role_callback, pattern="^role_"))
    app.add_handler(MessageHandler(filters.Regex("^🏠 Главное меню$"), start))

    # Placeholder for new order (add logic from your old bot here)
    # ...

    logger.info("Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()