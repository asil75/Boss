# ================== IMPORTS ==================
import time
import logging
import urllib.parse
import aiosqlite
from typing import Optional
from datetime import datetime

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    WebAppInfo, # Добавлено для Mini App
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    ConversationHandler,
    filters,
    TypeHandler,
    ApplicationHandlerStop
)

# ================== CONFIG ==================
from config import DB_PATH, is_owner

# ========== Настройки ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("delivery")

BOT_TOKEN = "8555882487:AAFyl9juLHiZ33FIjcretFe0U2yIDau1pYs"

# !!! ЗАМЕНИТЕ ЭТУ ССЫЛКУ НА ВАШУ ИЗ GITHUB PAGES !!!
MINI_APP_URL = "https://ВАШ-ЛОГИН.github.io/ВАШ-РЕПОЗИТОРИЙ/"

# FSM states
(
    ADDRESS_FROM,
    CONTACT_SHOP,
    ADDRESS_CLIENT,    
    CLIENT_PHONE,      
    CLIENT_NAME,       
    DELIVERY_PRICE,    
) = range(6)

# Payment Statuses
PAYMENT_STATUS_UNPAID = 0
PAYMENT_STATUS_MARKED_PAID = 1
PAYMENT_STATUS_CONFIRMED = 2

# Commands for roles
SHOP_COMMANDS = [
    BotCommand("new_order", "Создать заказ"),
    BotCommand("myorders", "Активные заказы"),
    BotCommand("unpaid", "Неоплаченные заказы"),
    BotCommand("completed", "История заказов"),
    BotCommand("finance", "Финансы магазина"),
    BotCommand("stats", "Статистика"),
    BotCommand("whoami", "Моя роль"),
    BotCommand("cancel", "Отменить действие"),
]

COURIER_COMMANDS = [
    BotCommand("myorders", "Мои доставки"),
    BotCommand("unpaid", "Неоплаченные заказы"),
    BotCommand("completed", "История доставок"),
    BotCommand("payouts", "Выплаты (получено/должны)"),
    BotCommand("stats", "Статистика"),
    BotCommand("whoami", "Моя роль"),
    BotCommand("cancel", "Отменить действие"),
]

# ========== DB init / helpers ==========
async def init_db(app):
    logger.info("Инициализация базы данных...")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER UNIQUE,
                    role TEXT,
                    phone TEXT
                );
            """)
            try:
                await db.execute("ALTER TABLE users ADD COLUMN phone TEXT")
            except Exception: pass
            try:
                await db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
            except Exception: pass

            await db.execute("""
                CREATE TABLE IF NOT EXISTS ratings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    courier_tg_id INTEGER,
                    shop_tg_id INTEGER,
                    order_id INTEGER,
                    rating INTEGER CHECK(rating >= 1 AND rating <= 5),
                    comment TEXT,
                    created_at TEXT
                );
            """)

            await db.execute("""
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
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS courier_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
                    courier_tg_id INTEGER, message_id INTEGER, created_at TEXT
                );
            """)

            await db.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
                    shop_tg_id INTEGER, courier_tg_id INTEGER, amount REAL, paid_at TEXT
                );
            """)
            await db.commit()
            logger.info("DB ready")
    except Exception as e:
        logger.exception("Ошибка инициализации БД")

async def set_role(tg_id: int, role: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        if role:
            await db.execute("""
                INSERT INTO users (tg_id, role)
                VALUES (?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role;
            """, (tg_id, role))
        else:
            await db.execute("""
                INSERT INTO users (tg_id, role)
                VALUES (?, NULL)
                ON CONFLICT(tg_id) DO UPDATE SET role=NULL;
            """, (tg_id,))
        await db.commit()

async def get_role(tg_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

async def save_phone(tg_id: int, phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (tg_id, phone, role)
            VALUES (?, ?, NULL)
            ON CONFLICT(tg_id) DO UPDATE SET phone=excluded.phone;
        """, (tg_id, phone))
        await db.commit()

async def check_phone_exists(tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT phone FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        await cur.close()
    return bool(row and row[0])

async def get_username(tg_id: int, app):
    if not tg_id: return "—"
    try:
        user = await app.bot.get_chat(tg_id)
        name = getattr(user, "full_name", None)
        if name: return name
        if getattr(user, "username", None): return f"@{user.username}"
        return f"ID {tg_id}"
    except Exception: return f"ID {tg_id}"

async def save_order(data: dict):
    created_at = str(int(time.time()))
    log_text = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Заказ создан."
    initial_status = 'taken' if data.get("return_for_id") else 'new'
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders(
                shop_tg_id, courier_tg_id,
                from_address, shop_contact,
                to_address, to_apt, client_name, client_phone,
                price, status, log, created_at, return_for, paid_to_courier, paid_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
        """, (
            data["shop_tg_id"], data.get("courier_tg_id"),
            data["from_address"], data["shop_contact"],
            data["to_address"], data.get("to_apt", ""),
            data.get("client_name", ""), data.get("client_phone", ""),
            data["price"], initial_status, log_text, created_at,
            data.get("return_for_id"), PAYMENT_STATUS_UNPAID,
        ))
        await db.commit()
        return cur.lastrowid

async def pay_courier_fixed_amount(order_id: int, courier_id: int, price: float, log_message: str):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    amount = round(price * 0.70, 2)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET log=log || ? WHERE id=?", 
                         (f"[{timestamp}] {log_message}. Сумма: {amount} ₽.\n", order_id))
        shop_tg_id_lookup = (await get_order(order_id))["shop_tg_id"]
        await db.execute("INSERT INTO payments (order_id, shop_tg_id, courier_tg_id, amount, paid_at) VALUES (?, ?, ?, ?, ?)",
                         (order_id, shop_tg_id_lookup, courier_id, amount, timestamp))
        await db.commit()
    return amount

async def update_order_details(order_id: int, data: dict):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log_entry = f"\n[{timestamp}] Заказ отредактирован магазином."
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE orders SET
                from_address=?, shop_contact=?,
                to_address=?, to_apt=?, client_name=?,
                client_phone=?, price=?, log=log || ?
            WHERE id=?
        """, (
            data["from_address"], data["shop_contact"],
            data["to_address"], data.get("to_apt", ""),
            data.get("client_name", ""), data.get("client_phone", ""),
            data["price"], log_entry, order_id
        ))
        await db.commit()
        return order_id

async def get_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, shop_tg_id, courier_tg_id, from_address, shop_contact,
                   to_address, to_apt, client_name, client_phone, price,
                   status, log, created_at, return_for, paid_to_courier, paid_at
            FROM orders WHERE id=?
        """, (order_id,))
        row = await cur.fetchone()
        await cur.close()
    if not row: return None
    return {
        "id": row[0], "shop_tg_id": row[1], "courier_tg_id": row[2], "from_address": row[3],
        "shop_contact": row[4], "to_address": row[5], "to_apt": row[6], "client_name": row[7],
        "client_phone": row[8], "price": row[9], "status": row[10], "log": row[11],
        "created_at": row[12], "return_for": row[13], "paid_to_courier": row[14], "paid_at": row[15],
    }

async def update_order(order_id: int, status: Optional[str] = None, courier: Optional[int] = None, log_add: Optional[str] = None, paid: Optional[int] = None):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        if log_add:
            await db.execute("UPDATE orders SET log = log || ? WHERE id=?", (f"[{timestamp}] {log_add}\n", order_id))
        updates, params = [], []
        if status is not None:
            updates.append("status=?")
            params.append(status)
        if courier is not None:
            if courier == 0: updates.append("courier_tg_id=NULL")
            else:
                updates.append("courier_tg_id=?")
                params.append(courier)
        if paid is not None:
            updates.append("paid_to_courier=?")
            params.append(paid)
            if paid == PAYMENT_STATUS_CONFIRMED:
                 updates.append("paid_at=?")
                 params.append(timestamp)
            elif paid == PAYMENT_STATUS_UNPAID:
                 updates.append("paid_at=NULL")
        if not updates: return
        params.append(order_id)
        await db.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id=?", tuple(params))
        await db.commit()

async def save_courier_message_record(order_id: int, courier_tg_id: int, message_id: int):
    created_at = str(int(time.time()))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO courier_messages (order_id, courier_tg_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (order_id, courier_tg_id, message_id, created_at))
        await db.commit()

async def get_courier_message_records(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT courier_tg_id, message_id FROM courier_messages WHERE order_id=?", (order_id,))
        rows = await cur.fetchall()
        await cur.close()
    return [(r[0], r[1]) for r in rows]

async def delete_courier_message_records(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM courier_messages WHERE order_id=?", (order_id,))
        await db.commit()

async def delete_specific_courier_message_record(order_id: int, courier_tg_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM courier_messages WHERE order_id=? AND courier_tg_id=? AND message_id=?", (order_id, courier_tg_id, message_id))
        await db.commit()

async def deactivate_or_delete_message(app, chat_id: int, message_id: int, text_override: Optional[str] = None):
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception:
        try:
            if text_override: await app.bot.edit_message_text(text_override, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            else: await app.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            return True
        except Exception as e:
            logger.info(f"Не удалось деактивировать сообщение {message_id}: {e}")
            return False

async def purge_chat_history(app, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    if "all_bot_messages" not in ctx.user_data:
        ctx.user_data["all_bot_messages"] = []
        return
    for mid in ctx.user_data["all_bot_messages"]:
        if mid:
            try: await app.bot.delete_message(chat_id=chat_id, message_id=mid)
            except: pass
    ctx.user_data["all_bot_messages"] = []

async def register_bot_message(ctx: ContextTypes.DEFAULT_TYPE, message_id: int):
    if "all_bot_messages" not in ctx.user_data: ctx.user_data["all_bot_messages"] = []
    if message_id not in ctx.user_data["all_bot_messages"]: ctx.user_data["all_bot_messages"].append(message_id)

async def delete_message_after_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message:
        try: await ctx.application.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
        except: pass

def html_report(order_id: int, o: dict, courier_name: Optional[str], include_log: bool = True):
    price_display = int(o.get("price") or 0)
    log_content = o.get("log") or "Лог пуст."
    def clickable(addr):
        if not addr: return "Н/Д"
        enc = urllib.parse.quote_plus(addr)
        return f'<a href="https://yandex.ru/maps/?text={enc}">{addr}</a>'
    header = f"<b>ЗАКАЗ #{order_id}</b>"
    if o.get("return_for"): header = f"↩️ <b>ВОЗВРАТ #{o['return_for']}.{order_id}</b>"
    report = (
        f"{header}\n\n<b>Статус:</b> {o['status'].upper()}\n<b>Цена:</b> {price_display}₽\n<b>Курьер:</b> {courier_name or '—'}\n\n"
        f"<b>ОТПРАВИТЕЛЬ</b>\nАдрес: {clickable(o['from_address'])}\nКонтакт: {o['shop_contact']}\n\n"
        f"<b>ПОЛУЧАТЕЛЬ</b>\nАдрес: {clickable(o['to_address'])}\nИмя: {o['client_name']}\nТелефон: {o['client_phone']}\n"
    )
    if include_log: report += f"\n<b>ЛОГ:</b>\n<pre>{log_content}</pre>"
    return report

async def get_couriers():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE role='courier'")
        rows = await cur.fetchall()
    return [r[0] for r in rows]

async def send_order_to_couriers(order_id: int, app):
    o = await get_order(order_id)
    if not o or o["status"] != "new": return
    txt = html_report(order_id, o, courier_name="—", include_log=False)
    kb = [[InlineKeyboardButton(f"🚀 Взять заказ #{order_id}", callback_data=f"take_{order_id}")]]
    markup = InlineKeyboardMarkup(kb)
    existing = await get_courier_message_records(order_id)
    for cid, mid in existing:
        try: await deactivate_or_delete_message(app, cid, mid, text_override=f"❌ <b>Заказ #{order_id} обновлён</b>")
        except: pass
    await delete_courier_message_records(order_id)
    couriers = await get_couriers()
    for cid in couriers:
        try:
            sent_msg = await app.bot.send_message(cid, txt, parse_mode="HTML", reply_markup=markup)
            if getattr(sent_msg, "message_id", None): await save_courier_message_record(order_id, cid, sent_msg.message_id)
        except: continue

async def set_role_commands(app, tg_id: int, role: Optional[str]):
    try:
        if role == "shop": await app.bot.set_my_commands(SHOP_COMMANDS, scope=BotCommandScopeChat(tg_id))
        elif role == "courier": await app.bot.set_my_commands(COURIER_COMMANDS, scope=BotCommandScopeChat(tg_id))
        else: await app.bot.delete_my_commands(scope=BotCommandScopeChat(tg_id))
    except: pass

# ========== CONTACT HANDLER (PHONE) ==========
async def handle_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    contact = update.message.contact
    if contact.user_id != user.id:
        await update.message.reply_text("⛔ Отправьте свой контакт через кнопку.")
        return
    await save_phone(user.id, contact.phone_number)
    await update.message.reply_text("✅ Номер подтвержден!", reply_markup=ReplyKeyboardRemove())
    await show_main_menu(update, ctx)

# ========== START LOGIC (ИНТЕГРАЦИЯ MINI APP) ==========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await global_block_guard(update, ctx): return
    tg = update.effective_user.id
    if update.message:
        await purge_chat_history(ctx.application, tg, ctx)
        await delete_message_after_command(update, ctx)
    if not await check_phone_exists(tg):
        kb = [[KeyboardButton("📱 Поделиться номером", request_contact=True)]]
        await update.message.reply_text("👋 Привет! Подтвердите номер телефона:", reply_markup=ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True))
        return
    role = await get_role(tg)
    if role in ["shop", "courier"]: await show_role_menu(update, ctx)
    else: await show_main_menu(update, ctx)

async def show_role_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await purge_chat_history(context.application, user_id, context)
    role = await get_role(user_id)
    
    # Кнопка Mini App
    wa_btn = KeyboardButton("🚀 Открыть Delivery App", web_app=WebAppInfo(url=MINI_APP_URL))

    if role == "shop":
        keyboard = [
            [wa_btn],
            ["➕ Новый заказ", "📦 Активные заказы"],
            ["💰 Финансы", "📊 Статистика"],
            ["📋 История заказов", "❌ Отмена"]
        ]
        text = "🏪 <b>Меню магазина</b>"
    elif role == "courier":
        keyboard = [
            [wa_btn],
            ["📦 Взять заказ", "🗺️ Мои доставки"],
            ["💰 Выплаты", "📊 Статистика"],
            ["⭐ Мой рейтинг", "❌ Отмена"]
        ]
        text = "🛵 <b>Меню курьера</b>"
    else:
        await show_main_menu(update, context)
        return
    
    reply_msg = await context.application.bot.send_message(user_id, text, parse_mode="HTML", reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True))
    await register_bot_message(context, reply_msg.message_id)

async def handle_menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    user_id = update.effective_user.id
    role = await get_role(user_id)
    await purge_chat_history(context.application, user_id, context)
    if text == "❌ Отмена": await show_role_menu(update, context)
    elif role == "shop":
        if text == "📦 Активные заказы": await myorders(update, context)
        elif text == "💰 Финансы": await finance(update, context)
        elif text == "📊 Статистика": await stats(update, context)
        elif text == "📋 История заказов": await completed_orders(update, context)
    elif role == "courier":
        if text == "📦 Взять заказ": await show_available_orders(update, context)
        elif text == "🗺️ Мои доставки": await myorders(update, context)
        elif text == "💰 Выплаты": await payouts(update, context)
        elif text == "📊 Статистика": await stats(update, context)
        elif text == "⭐ Мой рейтинг": await show_rating(update, context)

async def show_available_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT id, price, from_address, to_address FROM orders WHERE status='new' LIMIT 5")
        orders = await cursor.fetchall()
    if not orders:
        await update.message.reply_text("📭 Нет новых заказов.")
        return
    text = "📦 <b>Доступные заказы:</b>\n\n"
    for o in orders:
        text += f"<b>#{o[0]}</b> | 💰 {o[1]}₽\n📍 {o[2][:20]}... ➔ {o[3][:20]}...\n\n"
    kb = [[InlineKeyboardButton("🔄 Обновить", callback_data="refresh_orders")]]
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

async def show_rating(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("SELECT COUNT(*), SUM(CASE WHEN status='delivered' THEN 1 ELSE 0 END) FROM orders WHERE courier_tg_id=?", (user_id,))
        stats = await cursor.fetchone()
    total, completed = stats
    text = f"⭐ <b>Ваш рейтинг</b>\nЗаказов: {total}\nУспешно: {completed}"
    await update.message.reply_text(text, parse_mode="HTML")

async def set_role_callback(update: Update, context: ContextTypes.DEFAULT_TYPE, role: str):
    user_id = update.effective_user.id
    await set_role(user_id, role)
    await set_role_commands(context.application, user_id, role)
    await show_role_menu(update, context)

async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    kb = [
        [InlineKeyboardButton("🏪 Магазин", callback_data="role_shop")],
        [InlineKeyboardButton("🛵 Курьер", callback_data="role_courier")],
    ]
    reply_msg = await ctx.application.bot.send_message(tg, "Выберите роль:", reply_markup=InlineKeyboardMarkup(kb))
    await register_bot_message(ctx, reply_msg.message_id)

async def role_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "role_shop": await set_role_callback(update, ctx, "shop")
    else: await set_role_callback(update, ctx, "courier")

async def whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    await update.message.reply_html(f"Роль: <b>{role or 'не выбрана'}</b>")

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    async with aiosqlite.connect(DB_PATH) as db:
        field = "shop_tg_id" if role == "shop" else "courier_tg_id"
        cur = await db.execute(f"SELECT status, COUNT(*) FROM orders WHERE {field}=? GROUP BY status", (tg,))
        rows = await cur.fetchall()
    msg = f"📊 <b>Стат ({role})</b>\n"
    for s, c in rows: msg += f"{s}: {c}\n"
    await update.message.reply_html(msg)

# ========== FSM (NEW ORDER) ==========
async def new_order_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    if await get_role(tg) != "shop": return ConversationHandler.END
    ctx.user_data.update({"shop_tg_id": tg, "order_id": None, "return_for_id": None})
    await purge_chat_history(ctx.application, tg, ctx)
    msg = await ctx.application.bot.send_message(tg, "📝 <b>Шаг 1/6 — Адрес магазина</b>", parse_mode="HTML")
    await register_bot_message(ctx, msg.message_id)
    return ADDRESS_FROM

async def step_from(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["from_address"] = update.message.text.strip()
    await update.message.reply_html("📝 <b>Шаг 2/6 — Контакт</b>")
    return CONTACT_SHOP

async def step_shop_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["shop_contact"] = update.message.text.strip()
    await update.message.reply_html("📝 <b>Шаг 3/6 — Куда?</b>")
    return ADDRESS_CLIENT

async def step_client_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["to_address"] = update.message.text.strip()
    await update.message.reply_html("📝 <b>Шаг 4/6 — Тел. клиента</b>")
    return CLIENT_PHONE

async def step_client_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["client_phone"] = update.message.text.strip()
    await update.message.reply_html("📝 <b>Шаг 5/6 — Имя клиента</b>")
    return CLIENT_NAME

async def step_client_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["client_name"] = update.message.text.strip()
    await update.message.reply_html("📝 <b>Шаг 6/6 — Цена</b>")
    return DELIVERY_PRICE

async def step_price_final(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: ctx.user_data["price"] = float(update.message.text.replace(",", "."))
    except: return DELIVERY_PRICE
    oid = await save_order(ctx.user_data)
    await send_order_to_couriers(oid, ctx.application)
    await update.message.reply_html(f"✅ <b>Заказ #{oid} создан!</b>")
    ctx.user_data.clear()
    return ConversationHandler.END

async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_html("Отмена.")
    return ConversationHandler.END

# ========== MYORDERS / FINANCE ==========
async def myorders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    async with aiosqlite.connect(DB_PATH) as db:
        field = "courier_tg_id" if role == "courier" else "shop_tg_id"
        cur = await db.execute(f"SELECT * FROM orders WHERE {field}=? AND status NOT IN ('delivered', 'cancelled') ORDER BY created_at DESC", (tg,))
        rows = await cur.fetchall()
    if not rows: await update.message.reply_html("Нет активных заказов"); return
    for r in rows:
        o = await get_order(r[0])
        txt = html_report(r[0], o, "Вы", include_log=False)
        await update.effective_chat.send_message(txt, parse_mode="HTML")

async def finance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("💰 Раздел финансов магазина.")

async def payouts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_html("💰 Раздел выплат курьера.")

async def global_block_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or is_owner(user.id): return
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_blocked FROM users WHERE tg_id=?", (user.id,))
        row = await cur.fetchone()
    if row and row[0] == 1: raise ApplicationHandlerStop

async def error_handler(update, ctx): logger.error("Ошибка:", exc_info=ctx.error)

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = init_db
    app.add_error_handler(error_handler)
    app.add_handler(TypeHandler(Update, global_block_guard), group=-1)

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new_order", new_order_start),
            MessageHandler(filters.Regex("^➕ Новый заказ$"), new_order_start),
        ],
        states={
            ADDRESS_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_from)],
            CONTACT_SHOP: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_shop_contact)],
            ADDRESS_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_address)],
            CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_phone)],
            CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_name)],
            DELIVERY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_price_final)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )
    app.add_handler(conv)

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(role_choice, pattern="^role_"))
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    
    # Menu filters
    app.add_handler(MessageHandler(filters.Regex("^📦 Активные заказы$|^🗺️ Мои доставки$"), myorders))
    app.add_handler(MessageHandler(filters.Regex("^💰 Финансы$"), finance))
    app.add_handler(MessageHandler(filters.Regex("^💰 Выплаты$"), payouts))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), stats))
    app.add_handler(MessageHandler(filters.Regex("^⭐ Мой рейтинг$"), show_rating))
    app.add_handler(MessageHandler(filters.Regex("^❌ Отмена$"), show_role_menu))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_button))

    logger.info("BOT STARTED")
    app.run_polling()

if __name__ == "__main__":
    main()