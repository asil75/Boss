# ================== IMPORTS ==================
import time
import logging
import urllib.parse
import aiosqlite
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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

# Импортируем настройки
from config import BOT_TOKEN, OWNER_ID, DB_PATH, is_owner

# ========== Логирование ==========
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("delivery")

# ================== GLOBAL BLOCK GUARD ==================
async def global_block_guard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Глобальный защитник. Проверяет блокировку до выполнения любых команд."""
    user = update.effective_user
    if not user or user.id == OWNER_ID:
        return

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_blocked FROM users WHERE tg_id=?", (user.id,))
        row = await cur.fetchone()
        await cur.close()

    if row and row[0] == 1:
        if update.message:
            await update.message.reply_text("⛔ Вы заблокированы администратором.")
        elif update.callback_query:
            await update.callback_query.answer("⛔ Вы заблокированы администратором.", show_alert=True)
        # Прерываем выполнение всех остальных хендлеров
        raise ApplicationHandlerStop

# ================== КОНСТАНТЫ И СОСТОЯНИЯ ==================
(ADDRESS_FROM, CONTACT_SHOP, ADDRESS_CLIENT, CLIENT_PHONE, CLIENT_NAME, DELIVERY_PRICE) = range(6)

PAYMENT_STATUS_UNPAID = 0
PAYMENT_STATUS_MARKED_PAID = 1
PAYMENT_STATUS_CONFIRMED = 2

SHOP_COMMANDS = [
    BotCommand("new_order", "Создать заказ"), BotCommand("myorders", "Активные заказы"),
    BotCommand("unpaid", "Неоплаченные"), BotCommand("completed", "История"),
    BotCommand("finance", "Финансы"), BotCommand("stats", "Статистика"),
    BotCommand("whoami", "Роль"), BotCommand("cancel", "Отмена")
]

COURIER_COMMANDS = [
    BotCommand("myorders", "Доставки"), BotCommand("unpaid", "Оплаты"),
    BotCommand("completed", "История"), BotCommand("payouts", "Выплаты"),
    BotCommand("stats", "Статистика"), BotCommand("whoami", "Роль"),
    BotCommand("cancel", "Отмена")
]

# ================== БД И ХЕЛПЕРЫ ==================
async def init_db(app):
    async with aiosqlite.connect(DB_PATH) as db:
        # Таблица пользователей
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tg_id INTEGER UNIQUE, role TEXT, phone TEXT, is_blocked INTEGER DEFAULT 0
            );
        """)
        # Таблица заказов
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, shop_tg_id INTEGER, courier_tg_id INTEGER,
                from_address TEXT, shop_contact TEXT, to_address TEXT, to_apt TEXT,
                client_name TEXT, client_phone TEXT, price REAL, status TEXT, log TEXT, 
                created_at TEXT, return_for INTEGER DEFAULT NULL,
                paid_to_courier INTEGER DEFAULT 0, paid_at TEXT DEFAULT NULL
            );
        """)
        # Таблица сообщений курьерам
        await db.execute("""
            CREATE TABLE IF NOT EXISTS courier_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
                courier_tg_id INTEGER, message_id INTEGER, created_at TEXT
            );
        """)
        # Таблица выплат
        await db.execute("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
                shop_tg_id INTEGER, courier_tg_id INTEGER, amount REAL, paid_at TEXT
            );
        """)
        # Миграции
        try: await db.execute("ALTER TABLE users ADD COLUMN phone TEXT")
        except: pass
        try: await db.execute("ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0")
        except: pass
        await db.commit()
    logger.info("Database initialized successfully.")

# --- Хелперы пользователей ---
async def set_role(tg_id: int, role: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (tg_id, role) VALUES (?, ?) ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role", (tg_id, role))
        await db.commit()

async def get_role(tg_id: int) -> Optional[str]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return row[0] if row else None

async def save_phone(tg_id: int, phone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO users (tg_id, phone) VALUES (?, ?) ON CONFLICT(tg_id) DO UPDATE SET phone=excluded.phone", (tg_id, phone))
        await db.commit()

async def check_phone_exists(tg_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT phone FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        return bool(row and row[0])

async def get_username(tg_id: int, app):
    if not tg_id: return "—"
    try:
        u = await app.bot.get_chat(tg_id)
        return u.full_name or f"@{u.username}" or f"ID {tg_id}"
    except: return f"ID {tg_id}"

# --- Хелперы заказов ---
async def save_order(data: dict):
    ts = str(int(time.time()))
    log = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Заказ создан."
    status = 'taken' if data.get("return_for_id") else 'new'
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders(shop_tg_id, courier_tg_id, from_address, shop_contact, to_address, to_apt, 
            client_name, client_phone, price, status, log, created_at, return_for, paid_to_courier)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (data["shop_tg_id"], data.get("courier_tg_id"), data["from_address"], data["shop_contact"],
              data["to_address"], data.get("to_apt", ""), data.get("client_name", ""), data.get("client_phone", ""),
              data["price"], status, log, ts, data.get("return_for_id"), PAYMENT_STATUS_UNPAID))
        await db.commit()
        return cur.lastrowid

async def update_order(order_id: int, **kwargs):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        if 'log_add' in kwargs:
            await db.execute("UPDATE orders SET log = log || ? WHERE id=?", (f"\n[{ts}] {kwargs.pop('log_add')}", order_id))
        
        updates = []
        params = []
        for k, v in kwargs.items():
            if k == 'courier':
                if v == 0: updates.append("courier_tg_id=NULL")
                else: 
                    updates.append("courier_tg_id=?")
                    params.append(v)
            elif k == 'paid':
                updates.append("paid_to_courier=?")
                params.append(v)
                if v == PAYMENT_STATUS_CONFIRMED:
                    updates.append("paid_at=?")
                    params.append(ts)
            else:
                updates.append(f"{k}=?")
                params.append(v)
        
        if updates:
            params.append(order_id)
            await db.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id=?", tuple(params))
        await db.commit()

async def get_order(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM orders WHERE id=?", (order_id,))
        row = await cur.fetchone()
        return dict(row) if row else None

# --- Работа с сообщениями ---
async def save_courier_message_record(oid, cid, mid):
    ts = str(int(time.time()))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO courier_messages (order_id, courier_tg_id, message_id, created_at) VALUES (?,?,?,?)", (oid, cid, mid, ts))
        await db.commit()

async def deactivate_or_delete_message(app, chat_id, mid, text_override=None):
    try: await app.bot.delete_message(chat_id, mid)
    except:
        try:
            if text_override: await app.bot.edit_message_text(text_override, chat_id, mid, parse_mode="HTML")
            else: await app.bot.edit_message_reply_markup(chat_id, mid, reply_markup=None)
        except: pass

async def purge_chat_history(app, chat_id, ctx):
    if "bot_msgs" not in ctx.user_data: ctx.user_data["bot_msgs"] = []
    for mid in ctx.user_data["bot_msgs"]:
        try: await app.bot.delete_message(chat_id, mid)
        except: pass
    ctx.user_data["bot_msgs"] = []

async def register_bot_message(ctx, mid):
    if "bot_msgs" not in ctx.user_data: ctx.user_data["bot_msgs"] = []
    if mid not in ctx.user_data["bot_msgs"]: ctx.user_data["bot_msgs"].append(mid)

def html_report(order_id: int, o: dict, courier_name: str, include_log: bool = True):
    price = int(o.get("price") or 0)
    def link(addr): return f'<a href="https://yandex.ru/maps/?text={urllib.parse.quote_plus(addr)}">{addr}</a>' if addr else "—"
    header = f"<b>ЗАКАЗ #{order_id}</b>" if not o.get("return_for") else f"↩️ <b>ВОЗВРАТ #{o['return_for']}.{order_id}</b>"
    res = f"{header}\n\n<b>Статус:</b> {o['status'].upper()}\n<b>Цена:</b> {price} ₽\n<b>Курьер:</b> {courier_name}\n\n" \
          f"<b>ОТПРАВИТЕЛЬ</b>\nАдрес: {link(o['from_address'])}\nКонтакт: {o['shop_contact']}\n\n" \
          f"<b>ПОЛУЧАТЕЛЬ</b>\nАдрес: {link(o['to_address'])}\nИмя: {o['client_name']}\nТел: {o['client_phone']}"
    if include_log: res += f"\n\n<b>ЛОГ:</b>\n<pre>{o['log']}</pre>"
    return res

async def send_order_to_couriers(order_id: int, app):
    o = await get_order(order_id)
    if not o or o["status"] != "new": return
    txt = html_report(order_id, o, "—", False)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"🚀 Взять #{order_id}", callback_data=f"take_{order_id}")]])
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE role='courier'")
        couriers = [r[0] for r in await cur.fetchall()]
        # Очистка старых кнопок
        cur_msg = await db.execute("SELECT courier_tg_id, message_id FROM courier_messages WHERE order_id=?", (order_id,))
        for cid, mid in await cur_msg.fetchall():
            await deactivate_or_delete_message(app, cid, mid, "❌ Заказ обновлен")
        await db.execute("DELETE FROM courier_messages WHERE order_id=?", (order_id,))
        await db.commit()

    for cid in couriers:
        try:
            m = await app.bot.send_message(cid, txt, parse_mode="HTML", reply_markup=kb)
            await save_courier_message_record(order_id, cid, m.message_id)
        except: pass

# ================== ОБРАБОТЧИКИ КОМАНД ==================

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    if update.message:
        await purge_chat_history(ctx.application, tg, ctx)
        try: await update.message.delete()
        except: pass

    if not await check_phone_exists(tg):
        kb = [[KeyboardButton("📱 Поделиться номером", request_contact=True)]]
        await update.effective_chat.send_message("👋 Привет! Для работы с ботом подтвердите номер телефона:", 
                                                 reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True, one_time_keyboard=True))
        return
    await show_main_menu(update, ctx)

async def handle_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if update.message.contact.user_id != user.id:
        await update.message.reply_text("⛔ Пожалуйста, отправьте свой контакт.")
        return
    await save_phone(user.id, update.message.contact.phone_number)
    await update.message.reply_text("✅ Номер подтвержден!", reply_markup=ReplyKeyboardRemove())
    await show_main_menu(update, ctx)

async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    # Проверка на активные заказы перед сменой роли
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM orders WHERE courier_tg_id=? AND status='taken'", (tg,))
        active = (await cur.fetchone())[0]
    
    if active > 0:
        role = await get_role(tg)
        m = await ctx.application.bot.send_message(tg, f"У вас есть активные доставки ({active}). Сначала завершите их. Текущая роль: <b>{role}</b>", parse_mode="HTML")
        await register_bot_message(ctx, m.message_id)
        return

    kb = [[InlineKeyboardButton("🏪 Магазин", callback_data="role_shop")], [InlineKeyboardButton("🛵 Курьер", callback_data="role_courier")]]
    m = await ctx.application.bot.send_message(tg, "Выберите вашу роль:", reply_markup=InlineKeyboardMarkup(kb))
    await register_bot_message(ctx, m.message_id)

async def role_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    role = "shop" if q.data == "role_shop" else "courier"
    await set_role(q.from_user.id, role)
    
    cmds = SHOP_COMMANDS if role == "shop" else COURIER_COMMANDS
    await ctx.application.bot.set_my_commands(cmds, scope=BotCommandScopeChat(q.from_user.id))
    m = await q.edit_message_text(f"✅ Вы выбрали роль: {'Магазин' if role=='shop' else 'Курьер'}. Меню обновлено.")
    await register_bot_message(ctx, m.message_id)

# --- Управление заказами (FSM) ---
async def new_order_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await get_role(update.effective_user.id) != "shop":
        await update.message.reply_text("Только магазин может создавать заказы.")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["shop_tg_id"] = update.effective_user.id
    m = await update.message.reply_html("📝 <b>Шаг 1/6 — Адрес магазина</b>\nВведите адрес отправки:")
    await register_bot_message(ctx, m.message_id)
    return ADDRESS_FROM

async def step_from(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["from_address"] = update.message.text.strip()
    m = await update.message.reply_html("📝 <b>Шаг 2/6 — Контакт магазина</b>\nВведите телефон или никнейм отправителя:")
    await register_bot_message(ctx, m.message_id)
    return CONTACT_SHOP

async def step_shop_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["shop_contact"] = update.message.text.strip()
    m = await update.message.reply_html("📝 <b>Шаг 3/6 — Адрес получателя</b>\nВведите адрес доставки:")
    await register_bot_message(ctx, m.message_id)
    return ADDRESS_CLIENT

async def step_client_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["to_address"] = update.message.text.strip()
    m = await update.message.reply_html("📝 <b>Шаг 4/6 — Телефон клиента</b>\nВведите номер телефона получателя:")
    await register_bot_message(ctx, m.message_id)
    return CLIENT_PHONE

async def step_client_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["client_phone"] = update.message.text.strip()
    m = await update.message.reply_html("📝 <b>Шаг 5/6 — Имя клиента</b>\nВведите имя получателя:")
    await register_bot_message(ctx, m.message_id)
    return CLIENT_NAME

async def step_client_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["client_name"] = update.message.text.strip()
    m = await update.message.reply_html("📝 <b>Шаг 6/6 — Цена доставки</b>\nВведите цену для курьера (только цифры):")
    await register_bot_message(ctx, m.message_id)
    return DELIVERY_PRICE

async def step_price_final(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try: price = float(update.message.text.strip().replace(",", "."))
    except: 
        await update.message.reply_text("Введите число.")
        return DELIVERY_PRICE
    
    ctx.user_data["price"] = price
    oid = await save_order(ctx.user_data)
    await send_order_to_couriers(oid, ctx.application)
    m = await update.message.reply_html(f"✅ <b>Заказ #{oid} создан и разослан курьерам!</b>")
    await register_bot_message(ctx, m.message_id)
    return ConversationHandler.END

# --- Действия курьера ---
async def take_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    oid = int(q.data.split("_")[1])
    cid = q.from_user.id
    o = await get_order(oid)
    if not o or o["status"] != "new":
        await q.answer("❌ Заказ уже недоступен", show_alert=True)
        return
    
    name = await get_username(cid, ctx.application)
    await update_order(oid, status="taken", courier=cid, log_add=f"Курьер {name} взял заказ")
    await q.edit_message_text(f"🚀 Вы взяли заказ #{oid}. Едьте к магазину.")
    
    o = await get_order(oid)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✉️ Магазин", url=f"tg://user?id={o['shop_tg_id']}")],
        [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{oid}")],
        [InlineKeyboardButton("❌ Отмена", callback_data=f"cancel_{oid}")]
    ])
    m = await ctx.application.bot.send_message(cid, html_report(oid, o, name, True), parse_mode="HTML", reply_markup=kb)
    await register_bot_message(ctx, m.message_id)
    await ctx.application.bot.send_message(o["shop_tg_id"], f"🚀 Курьер {name} взял ваш заказ #{oid}")

async def arrived(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    _, where, oid = q.data.split("_")
    oid = int(oid)
    o = await get_order(oid)
    if not o or o["courier_tg_id"] != q.from_user.id: return
    
    if where == "shop":
        await update_order(oid, status="at_shop", log_add="Прибыл к магазину")
        kb = [[InlineKeyboardButton("✅ Забрал товар", callback_data=f"picked_up_{oid}")]]
    else:
        await update_order(oid, status="at_client", log_add="Прибыл к клиенту")
        kb = [[InlineKeyboardButton("✅ Доставил", callback_data=f"finish_{oid}"), 
               InlineKeyboardButton("🚫 Нет дома", callback_data=f"client_not_home_{oid}")]]
    
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
    await ctx.application.bot.send_message(o["shop_tg_id"], f"📍 Курьер на месте ({'магазин' if where=='shop' else 'клиент'}) по заказу #{oid}")

async def picked_up(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    oid = int(q.data.split("_")[2])
    await update_order(oid, status="on_delivery", log_add="Товар забран, в пути")
    kb = [[InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{oid}")]]
    await q.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup(kb))
    o = await get_order(oid)
    await ctx.application.bot.send_message(o["shop_tg_id"], f"🚚 Товар по заказу #{oid} забран курьером и едет к клиенту.")

async def finish_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    oid = int(q.data.split("_")[1])
    await update_order(oid, status="delivered", log_add="Заказ успешно доставлен")
    await q.edit_message_text(f"✅ Заказ #{oid} завершен!")
    o = await get_order(oid)
    await ctx.application.bot.send_message(o["shop_tg_id"], f"🏁 Заказ #{oid} доставлен! Не забудьте оплатить курьеру.")

# --- Финансы и Статистика ---
async def finance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role = await get_role(update.effective_user.id)
    if role != "shop": return
    kb = [[InlineKeyboardButton("💸 Неоплаченные", callback_data="finance_unpaid")], 
          [InlineKeyboardButton("📊 Сводка", callback_data="finance_summary")]]
    await update.message.reply_text("Финансы магазина:", reply_markup=InlineKeyboardMarkup(kb))

async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    async with aiosqlite.connect(DB_PATH) as db:
        field = "shop_tg_id" if role == "shop" else "courier_tg_id"
        cur = await db.execute(f"SELECT status, COUNT(*) FROM orders WHERE {field}=? GROUP BY status", (tg,))
        rows = await cur.fetchall()
    
    txt = f"📊 <b>Статистика ({role})</b>\n\n"
    for s, c in rows: txt += f"{s}: {c}\n"
    await update.message.reply_html(txt)

async def whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role = await get_role(update.effective_user.id)
    await update.message.reply_html(f"Твоя роль: <b>{role or 'не выбрана'}</b>")

async def admin_panel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id): return
    kb = [["👥 Пользователи"], ["📢 Рассылка"], ["📜 Логи"]]
    await update.message.reply_text("👑 Админ-панель", reply_markup=ReplyKeyboardMarkup(kb, resize_keyboard=True))

async def cancel_fsm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_text("Операция отменена.")
    return ConversationHandler.END

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = init_db

    # 1. Глобальная проверка блокировки (Группа -1 выполняется самой первой)
    app.add_handler(TypeHandler(Update, global_block_guard), group=-1)

    # 2. FSM для создания заказа
    conv = ConversationHandler(
        entry_points=[CommandHandler("new_order", new_order_start)],
        states={
            ADDRESS_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_from)],
            CONTACT_SHOP: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_shop_contact)],
            ADDRESS_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_address)],
            CLIENT_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_phone)],
            CLIENT_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_name)],
            DELIVERY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_price_final)],
        },
        fallbacks=[CommandHandler("cancel", cancel_fsm)],
    )
    app.add_handler(conv)

    # 3. Базовые хендлеры
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("finance", finance))

    # 4. Callback Query
    app.add_handler(CallbackQueryHandler(role_choice, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(take_order, pattern="^take_"))
    app.add_handler(CallbackQueryHandler(arrived, pattern="^arrived_"))
    app.add_handler(CallbackQueryHandler(picked_up, pattern="^picked_up_"))
    app.add_handler(CallbackQueryHandler(finish_order, pattern="^finish_"))

    # 5. Загрузка плагина пользователей
    try:
        from plugins.users import register as reg_users
        reg_users(app)
        logger.info("Plugin 'users' loaded.")
    except Exception as e:
        logger.warning(f"Plugin 'users' not loaded: {e}")

    logger.info("BOT STARTED")
    app.run_polling()

if __name__ == "__main__":
    main()


