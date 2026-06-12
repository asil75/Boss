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

# ================== CONFIG ==================
DB_PATH = "db.sqlite3"

# ===== OWNER =====
OWNER_ID = 1309289031  # <-- твой Telegram ID


def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

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

# ========== Настройки ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("delivery")

BOT_TOKEN = "8555882487:AAFyl9juLHiZ33FIjcretFe0U2yIDau1pYs"  # Токен из твоего файла

# FSM states
(
    ADDRESS_FROM,
    CONTACT_SHOP,
    ADDRESS_CLIENT,    
    CLIENT_PHONE,      
    CLIENT_NAME,       
    DELIVERY_PRICE,    
) = range(6)

# Payment Statuses (Paid to courier field)
PAYMENT_STATUS_UNPAID = 0          # Не оплачено магазином
PAYMENT_STATUS_MARKED_PAID = 1     # Оплачено магазином (ждет подтверждения)
PAYMENT_STATUS_CONFIRMED = 2       # Подтверждено курьером

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

            # ===== users =====
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER UNIQUE,
                    role TEXT,
                    phone TEXT
                );
            """)

            # --- миграции users (БЕЗОПАСНЫЕ) ---
            try:
                await db.execute("ALTER TABLE users ADD COLUMN phone TEXT")
            except Exception:
                pass  # уже есть

            try:
                await db.execute(
                    "ALTER TABLE users ADD COLUMN is_blocked INTEGER DEFAULT 0"
                )
            except Exception:
                pass  # уже есть

            # ===== orders =====
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

            # ===== courier_messages =====
            await db.execute("""
                CREATE TABLE IF NOT EXISTS courier_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, order_id INTEGER,
                    courier_tg_id INTEGER, message_id INTEGER, created_at TEXT
                );
            """)

            # ===== payments =====
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

# user helpers
async def set_role(tg_id: int, role: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        if role:
            # Обновляем роль, не трогая телефон
            await db.execute("""
                INSERT INTO users (tg_id, role)
                VALUES (?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role;
            """, (tg_id, role))
        else:
            # remove role
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
    if not tg_id:
        return "—"
    try:
        user = await app.bot.get_chat(tg_id)
        name = getattr(user, "full_name", None)
        if name:
            return name
        if getattr(user, "username", None):
            return f"@{user.username}"
        return f"ID {tg_id}"
    except Exception:
        return f"ID {tg_id}"


# order helpers
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
            data["shop_tg_id"],
            data.get("courier_tg_id"),
            data["from_address"],
            data["shop_contact"],
            data["to_address"],
            data.get("to_apt", ""),
            data.get("client_name", ""),
            data.get("client_phone", ""),
            data["price"],
            initial_status,
            log_text,
            created_at,
            data.get("return_for_id"),
            PAYMENT_STATUS_UNPAID,
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
            data["from_address"],
            data["shop_contact"],
            data["to_address"],
            data.get("to_apt", ""),
            data.get("client_name", ""),
            data.get("client_phone", ""),
            data["price"],
            log_entry,
            order_id
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
    if not row:
        return None
    return {
        "id": row[0],
        "shop_tg_id": row[1],
        "courier_tg_id": row[2],
        "from_address": row[3],
        "shop_contact": row[4],
        "to_address": row[5],
        "to_apt": row[6],
        "client_name": row[7],
        "client_phone": row[8],
        "price": row[9],
        "status": row[10],
        "log": row[11],
        "created_at": row[12],
        "return_for": row[13],
        "paid_to_courier": row[14],
        "paid_at": row[15],
    }


async def update_order(order_id: int, status: Optional[str] = None, courier: Optional[int] = None, log_add: Optional[str] = None, paid: Optional[int] = None):
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        if log_add:
            await db.execute("UPDATE orders SET log = log || ? WHERE id=?", (f"[{timestamp}] {log_add}\n", order_id))
        updates = []
        params = []
        if status is not None:
            updates.append("status=?")
            params.append(status)
        if courier is not None:
            if courier == 0:
                updates.append("courier_tg_id=NULL")
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

        if not updates:
            return
        params.append(order_id)
        await db.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id=?", tuple(params))
        await db.commit()


# courier messages store
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
        cur = await db.execute("""
            SELECT courier_tg_id, message_id FROM courier_messages
            WHERE order_id=?
        """, (order_id,))
        rows = await cur.fetchall()
        await cur.close()
    return [(r[0], r[1]) for r in rows]


async def delete_courier_message_records(order_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM courier_messages WHERE order_id=?", (order_id,))
        await db.commit()


async def delete_specific_courier_message_record(order_id: int, courier_tg_id: int, message_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM courier_messages
            WHERE order_id=? AND courier_tg_id=? AND message_id=?
        """, (order_id, courier_tg_id, message_id))
        await db.commit()


# safe delete/edit message
async def deactivate_or_delete_message(app, chat_id: int, message_id: int, text_override: Optional[str] = None):
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception:
        try:
            if text_override:
                await app.bot.edit_message_text(text_override, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            else:
                await app.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            return True
        except Exception as e:
            logger.info(f"Не удалось деактивировать/удалить сообщение {message_id} в чате {chat_id}: {e}")
            return False

# ========= ФУНКЦИИ ТОТАЛЬНОЙ ОЧИСТКИ ЧАТА ОТ СООБЩЕНИЙ БОТА =========

async def purge_chat_history(app, chat_id: int, ctx: ContextTypes.DEFAULT_TYPE):
    if "all_bot_messages" not in ctx.user_data:
        ctx.user_data["all_bot_messages"] = []
        return
    messages_to_delete = ctx.user_data["all_bot_messages"]
    for mid in messages_to_delete:
        if mid:
            try:
                await app.bot.delete_message(chat_id=chat_id, message_id=mid)
            except Exception as e:
                logger.debug(f"Не удалось удалить старое сообщение {mid}: {e}")
    ctx.user_data["all_bot_messages"] = []


async def register_bot_message(ctx: ContextTypes.DEFAULT_TYPE, message_id: int):
    if "all_bot_messages" not in ctx.user_data:
        ctx.user_data["all_bot_messages"] = []
    if message_id not in ctx.user_data["all_bot_messages"]:
        ctx.user_data["all_bot_messages"].append(message_id)


async def delete_message_after_command(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.message:
        try:
            await ctx.application.bot.delete_message(chat_id=update.effective_chat.id, message_id=update.message.message_id)
        except Exception as e:
            logger.info(f"Не удалось удалить сообщение команды: {e}")


# HTML report builder
def html_report(order_id: int, o: dict, courier_name: Optional[str], include_log: bool = True):
    apt = o.get("to_apt") or "—"
    price_display = int(o.get("price") or 0)
    log_content = o.get("log") or "Лог пуст."
    def clickable(addr):
        if not addr:
            return "Н/Д"
        enc = urllib.parse.quote_plus(addr)
        return f'<a href="https://yandex.ru/maps/?text={enc}">{addr}</a>'

    header = f"<b>ЗАКАЗ #{order_id}</b>"
    if o.get("return_for"):
        header = f"↩️ <b>ВОЗВРАТ #{o['return_for']}.{order_id}</b> (к заказу #{o['return_for']})"

    report = (
        f"{header}\n\n"
        f"<b>Статус:</b> {o['status'].upper()}\n"
        f"<b>Цена:</b> {price_display} ₽\n"
        f"<b>Курьер:</b> {courier_name or '—'}\n\n"

        f"<b>ОТПРАВИТЕЛЬ</b>\n"
        f"Адрес: {clickable(o['from_address'])}\n"
        f"Контакт: {o['shop_contact']}\n\n"

        f"<b>ПОЛУЧАТЕЛЬ</b>\n"
        f"Адрес: {clickable(o['to_address'])}\n" 
        f"Имя: {o['client_name']}\n"
        f"Телефон: {o['client_phone']}\n"
    )
    if include_log:
        report += f"\n<b>ЛОГ:</b>\n<pre>{log_content}</pre>"
    return report


# send to couriers
async def get_couriers():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE role='courier'")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def send_order_to_couriers(order_id: int, app):
    o = await get_order(order_id)
    if not o or o["status"] != "new":
        return
    txt = html_report(order_id, o, courier_name="—", include_log=False)
    kb = [[InlineKeyboardButton(f"🚀 Взять заказ #{order_id}", callback_data=f"take_{order_id}")]]
    markup = InlineKeyboardMarkup(kb)
    existing = await get_courier_message_records(order_id)
    if existing:
        for cid, mid in existing:
            try:
                await deactivate_or_delete_message(app, cid, mid, text_override=f"❌ <b>Заказ #{order_id} неактуален (обновлён)</b>")
            except:
                pass
        await delete_courier_message_records(order_id)
    couriers = await get_couriers()
    for cid in couriers:
        try:
            sent_msg = await app.bot.send_message(cid, txt, parse_mode="HTML", reply_markup=markup)
            if getattr(sent_msg, "message_id", None):
                await save_courier_message_record(order_id, cid, sent_msg.message_id)
        except Exception as e:
            logger.info(f"Не удалось отправить заказ {order_id} курьеру {cid}: {e}")
            continue


# ========== Role commands (set/unset) ==========
async def set_role_commands(app, tg_id: int, role: Optional[str]):
    try:
        if role == "shop":
            await app.bot.set_my_commands(SHOP_COMMANDS, scope=BotCommandScopeChat(tg_id))
        elif role == "courier":
            await app.bot.set_my_commands(COURIER_COMMANDS, scope=BotCommandScopeChat(tg_id))
        else:
            await app.bot.delete_my_commands(scope=BotCommandScopeChat(tg_id))
    except Exception as e:
        logger.warning(f"Не удалось установить команды для {tg_id}: {e}")


# ========== CONTACT HANDLER (PHONE) ==========
async def handle_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Сохраняет телефон и пускает в бота."""
    user = update.effective_user
    contact = update.message.contact
    
    # Проверка, что контакт принадлежит отправителю
    if contact.user_id != user.id:
        await update.message.reply_text("⛔ Пожалуйста, отправьте свой контакт, нажав на кнопку ниже.")
        return

    # Сохраняем в БД
    await save_phone(user.id, contact.phone_number)
    
    # Убираем клавиатуру с кнопкой контакта
    await update.message.reply_text("✅ Номер успешно подтвержден!", reply_markup=ReplyKeyboardRemove())
    
    # Запускаем основное меню
    await show_main_menu(update, ctx)


# ========== START LOGIC ==========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Точка входа. Проверяет регистрацию телефона."""

    # 🔒 ШАГ 3.2 — проверка блокировки
    if await global_block_guard(update, ctx):
        return

    tg = update.effective_user.id

    # 🧹 Очистка чата от старых сообщений
    if update.message:
        await purge_chat_history(ctx.application, tg, ctx)
        await delete_message_after_command(update, ctx)

    # ⬇️ дальше ИДЁТ ТВОЙ СТАРЫЙ КОД
    # Проверка телефона
    if not await check_phone_exists(tg):
        # Если телефона нет - просим контакт
        kb = [[KeyboardButton("📱 Поделиться номером", request_contact=True)]]
        markup = ReplyKeyboardMarkup(kb, one_time_keyboard=True, resize_keyboard=True)
        
        await update.message.reply_text(
            "👋 Привет! Для работы с ботом необходимо подтвердить номер телефона.\n"
            "Нажмите кнопку ниже 👇",
            reply_markup=markup
        )
        return

    # Если телефон есть — показываем меню
    await show_main_menu(update, ctx)


async def show_main_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Показывает выбор роли или текущее состояние."""
    tg = update.effective_user.id
    
    # If user has active taken orders as courier, don't allow role switching until finished
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM orders WHERE courier_tg_id=? AND status='taken'", (tg,))
        row = await cur.fetchone()
        await cur.close()
    active_taken = row[0] if row else 0

    if active_taken:
        role = await get_role(tg)
        await set_role_commands(ctx.application, tg, role)
        reply_msg = await ctx.application.bot.send_message(
            tg,
            f"У вас есть активные доставки — сначала завершите их. Текущая роль: <b>{role or 'не выбрана'}</b>",
            parse_mode="HTML"
        )
        await register_bot_message(ctx, reply_msg.message_id)
        return

    # Выбор роли
    kb = [
        [InlineKeyboardButton("🏪 Магазин", callback_data="role_shop")],
        [InlineKeyboardButton("🛵 Курьер", callback_data="role_courier")],
    ]
    reply_msg = await ctx.application.bot.send_message(
        tg, 
        "Выберите вашу роль:", 
        reply_markup=InlineKeyboardMarkup(kb)
    )
    await register_bot_message(ctx, reply_msg.message_id)


async def role_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = q.from_user.id
    
    try:
        await q.edit_message_reply_markup(reply_markup=None)
        if q.message.message_id in ctx.user_data.get("all_bot_messages", []):
            ctx.user_data["all_bot_messages"].remove(q.message.message_id)
    except:
        pass
        
    if q.data == "role_shop":
        await set_role(tg, "shop")
        await set_role_commands(ctx.application, tg, "shop")
        reply_msg = await q.message.reply_text("Ты теперь Магазин. Меню обновлено 👍")
    else:
        await set_role(tg, "courier")
        await set_role_commands(ctx.application, tg, "courier")
        reply_msg = await q.message.reply_text("Ты теперь Курьер. Меню обновлено 👍")

    await register_bot_message(ctx, reply_msg.message_id)


# ========== WHOAMI / STATS ==========
async def whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    await purge_chat_history(ctx.application, tg, ctx)
    await delete_message_after_command(update, ctx)
    
    role = await get_role(tg)
    reply_msg = await update.message.reply_html(f"Твоя роль: <b>{role or 'не выбрана'}</b>")
    await register_bot_message(ctx, reply_msg.message_id)


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    await purge_chat_history(ctx.application, tg, ctx)
    await delete_message_after_command(update, ctx)
    
    role = await get_role(tg)
    if not role:
        reply_msg = await update.message.reply_text("Роль не выбрана.")
        await register_bot_message(ctx, reply_msg.message_id)
        return
        
    async with aiosqlite.connect(DB_PATH) as db:
        if role == "shop":
            cur = await db.execute("""
                SELECT status, COUNT(*) FROM orders
                WHERE shop_tg_id=?
                GROUP BY status
            """, (tg,))
        else:
            cur = await db.execute("""
                SELECT status, COUNT(*) FROM orders
                WHERE courier_tg_id=?
                GROUP BY status
            """, (tg,))
        rows = await cur.fetchall()
        
    msg = f"<b>📊 Статистика ({role.upper()})</b>\n"
    total = 0
    for s, c in rows:
        msg += f"{s}: {c}\n"
        total += c
    msg += f"\n<b>Всего: {total}</b>"
    
    reply_msg = await update.message.reply_html(msg)
    await register_bot_message(ctx, reply_msg.message_id)


# ========== NEW / EDIT ORDER (FSM) ==========
async def new_order_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    if await get_role(tg) != "shop":
        await update.message.reply_html("Только магазин может создавать заказы")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["shop_tg_id"] = tg
    ctx.user_data["order_id"] = None
    ctx.user_data["return_for_id"] = None
    ctx.user_data["courier_tg_id"] = None
    
    reply_msg = await update.message.reply_html("📝 <b>Шаг 1/6 — Адрес магазина</b>\nВведите адрес отправки:")
    await register_bot_message(ctx, reply_msg.message_id)
    return ADDRESS_FROM


async def shop_set_return_order_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    
    parts = q.data.split("_")
    try:
        order_id = int(parts[4]) 
    except (IndexError, ValueError) as e:
        await q.edit_message_text(f"Неверные данные в запросе. Операция отменена.")
        return ConversationHandler.END

    if await get_role(q.from_user.id) != "shop":
        await q.edit_message_text("Только магазин может создавать заказы")
        return ConversationHandler.END
    
    original_order = await get_order(order_id)
    if not original_order or original_order["status"] != "failed_delivery":
         await q.edit_message_text(f"⛔ Оригинальный заказ #{order_id} не находится в статусе Failed Delivery.")
         return ConversationHandler.END

    if not original_order["courier_tg_id"]:
        await q.edit_message_text(f"⛔ Для заказа #{order_id} не назначен курьер.")
        return ConversationHandler.END
        
    async with aiosqlite.connect(DB_PATH) as db:
        cur_ret = await db.execute(
            "SELECT COUNT(*) FROM orders WHERE return_for=? AND status != 'delivered'", 
            (order_id,)
        )
        count_returns = (await cur_ret.fetchone())[0]
        await cur_ret.close()
    
    if count_returns > 0:
        await q.edit_message_text(f"⛔ Возвратный заказ уже в работе.")
        return ConversationHandler.END

    ctx.user_data.clear()
    ctx.user_data["shop_tg_id"] = q.from_user.id
    ctx.user_data["order_id"] = None
    ctx.user_data["return_for_id"] = order_id
    ctx.user_data["courier_tg_id"] = original_order["courier_tg_id"]

    ctx.user_data["from_address"] = original_order["to_address"] 
    ctx.user_data["shop_contact"] = original_order["shop_contact"] 
    ctx.user_data["to_address"] = original_order["from_address"] 
    ctx.user_data["to_apt"] = ""
    ctx.user_data["client_name"] = original_order.get("client_name", original_order["shop_contact"]) 
    ctx.user_data["client_phone"] = original_order.get("client_phone", original_order["shop_contact"]) 
    
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except:
        pass
        
    reply_msg = await ctx.application.bot.send_message(
        q.from_user.id,
        f"📝 <b>Шаг 1/1 (Цена) — Создание Возвратного Заказа от #{order_id}</b>\n"
        "Адреса заполнены автоматически (обратный маршрут: от клиента к магазину).\n"
        "Введите цену, которую вы платите курьеру за возврат товара:",
        parse_mode="HTML"
    )
    await register_bot_message(ctx, reply_msg.message_id)
    
    await update_order(
        order_id, 
        status="completed_with_return", 
        log_add=f"Магазин создал возвратный заказ #{order_id}. Исходный заказ закрыт."
    )
    return DELIVERY_PRICE


async def edit_order_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Загружаю заказ для редактирования...")
    tg = q.from_user.id
    order_id = int(q.data.split("_")[1])
    
    if await get_role(tg) != "shop":
        await q.edit_message_text("⛔ Только магазин может редактировать заказы")
        return ConversationHandler.END
    
    order = await get_order(order_id)
    if not order:
        await q.edit_message_text("⚠️ Заказ не найден")
        return ConversationHandler.END
        
    if order["status"] in ("delivered", "cancelled", "cancelled_70_percent", "at_client", "failed_delivery", "completed_with_return") or order["return_for"] is not None:
        await q.edit_message_text("⛔ Этот заказ нельзя редактировать.")
        return ConversationHandler.END
        
    ctx.user_data.clear()
    ctx.user_data.update(order)
    ctx.user_data["order_id"] = order_id
    ctx.user_data["return_for_id"] = order["return_for"] 
    
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except:
        pass
        
    reply_msg = await ctx.application.bot.send_message(
        tg,
        f"✍️ <b>Редактирование заказа #{order_id}</b>\n"
        f"Текущий адрес отправки: <b>{order['from_address']}</b>\n"
        "Введите новый адрес или повторите старый:",
        parse_mode="HTML"
    )
    await register_bot_message(ctx, reply_msg.message_id)
    return ADDRESS_FROM


async def step_from(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("return_for_id") is not None:
        return DELIVERY_PRICE 
    ctx.user_data["from_address"] = update.message.text.strip()
    current_contact = ctx.user_data.get("shop_contact", "—")
    reply_msg = await update.message.reply_html(
        f"📝 <b>Шаг 2/6 — Контакт магазина</b>\n"
        f"Текущий контакт: <b>{current_contact}</b>\n"
        "Введите новый контакт или повторите старый:"
    )
    await register_bot_message(ctx, reply_msg.message_id)
    return CONTACT_SHOP


async def step_shop_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("return_for_id") is not None:
         return DELIVERY_PRICE
    ctx.user_data["shop_contact"] = update.message.text.strip()
    current_address = ctx.user_data.get("to_address", "—")
    reply_msg = await update.message.reply_html(
        f"📝 <b>Шаг 3/6 — Адрес получателя</b>\n"
        f"Текущий адрес: <b>{current_address}</b>\n"
        "Введите адрес доставки:"
    )
    await register_bot_message(ctx, reply_msg.message_id)
    return ADDRESS_CLIENT


async def step_client_address(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("return_for_id") is not None:
         return DELIVERY_PRICE
    text = update.message.text.strip()
    ctx.user_data["to_address"] = text
    ctx.user_data["to_apt"] = ""
    current_phone = ctx.user_data.get("client_phone", "—")
    reply_msg = await update.message.reply_html(
        f"📝 <b>Шаг 4/6 — Телефон клиента</b>\n"
        f"Текущий телефон: <b>{current_phone}</b>\n"
        "Введите номер телефона клиента:"
    )
    await register_bot_message(ctx, reply_msg.message_id)
    return CLIENT_PHONE


async def step_client_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("return_for_id") is not None:
         return DELIVERY_PRICE
    ctx.user_data["client_phone"] = update.message.text.strip()
    current_name = ctx.user_data.get("client_name", "—")
    reply_msg = await update.message.reply_html(
        f"📝 <b>Шаг 5/6 — Имя клиента</b>\n"
        f"Текущее имя: <b>{current_name}</b>\n"
        "Введите имя получателя:"
    )
    await register_bot_message(ctx, reply_msg.message_id)
    return CLIENT_NAME


async def step_client_name(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("return_for_id") is not None:
         return DELIVERY_PRICE
    ctx.user_data["client_name"] = update.message.text.strip()
    current_price = ctx.user_data.get("price", "—")
    reply_msg = await update.message.reply_html(
        f"📝 <b>Шаг 6/6 — Цена доставки (Курьеру)</b>\n"
        f"Текущая цена: <b>{current_price}</b>\nВведите новую цену (только число):"
    )
    await register_bot_message(ctx, reply_msg.message_id)
    return DELIVERY_PRICE


async def step_price_final(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    txt = update.message.text.strip().replace(",", ".")
    try:
        price = float(txt)
    except:
        await update.message.reply_html("⚠️ Введите корректное число")
        return DELIVERY_PRICE
        
    ctx.user_data["price"] = price
    order_id = ctx.user_data.get("order_id")
    courier_id = ctx.user_data.get("courier_tg_id")
    is_return = ctx.user_data.get("return_for_id") is not None
    
    if order_id is None:
        oid = await save_order(ctx.user_data)
        if is_return:
             courier_name = await get_username(courier_id, ctx.application)
             o = await get_order(oid)
             report = html_report(oid, o, courier_name, include_log=True)
             try:
                kb = InlineKeyboardMarkup([
                     [InlineKeyboardButton(f"✅ Возвращен в магазин", callback_data=f"finish_return_{oid}")]
                ])
                sent_msg = await ctx.application.bot.send_message(
                    courier_id, 
                    f"↩️ <b>СОЗДАН ВОЗВРАТ #{o['return_for']}.{oid}</b>\nТовар необходимо вернуть в магазин. \n\n{report}", 
                    parse_mode="HTML", 
                    reply_markup=kb
                )
                if getattr(sent_msg, "message_id", None):
                    await save_courier_message_record(oid, courier_id, sent_msg.message_id)
             except Exception as e:
                logger.warning(f"Ошибка уведомления курьера о возврате: {e}")
             
             reply_msg = await update.message.reply_html(f"✅ <b>Возвратный заказ #{oid} (к #{ctx.user_data['return_for_id']}) создан и назначен курьеру {courier_name}</b>")
             await register_bot_message(ctx, reply_msg.message_id)
        else:
            await send_order_to_couriers(oid, ctx.application)
            reply_msg = await update.message.reply_html(
                f"✅ <b>Заказ #{oid} создан</b>\nРазослан всем курьерам."
            )
            await register_bot_message(ctx, reply_msg.message_id)

    else:
        oid = await update_order_details(order_id, ctx.user_data)
        new_order_data = await get_order(oid)
        courier_id_updated = new_order_data.get("courier_tg_id")
        courier_name = await get_username(courier_id_updated, ctx.application)
        
        if is_return:
             reply_msg = await update.message.reply_html(f"✏️ <b>Возвратный заказ #{order_id} (к #{new_order_data['return_for']}) обновлён</b>")
        else:
             reply_msg = await update.message.reply_html(f"✏️ <b>Заказ #{oid} обновлён</b>")
        await register_bot_message(ctx, reply_msg.message_id)
             
        if courier_id_updated:
            report = html_report(oid, new_order_data, courier_name, include_log=True)
            status = new_order_data['status']
            kb = []
            
            if new_order_data["return_for"] is None:
                if status == 'taken':
                    kb = [[InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={new_order_data['shop_tg_id']}")],
                          [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{oid}")],
                          [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{oid}")]]
                elif status == 'at_shop':
                     kb = [[InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={new_order_data['shop_tg_id']}")],
                           [InlineKeyboardButton("✅ Забрал", callback_data=f"picked_up_{oid}")],
                           [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{oid}")]]
                elif status == 'on_delivery':
                     kb = [[InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{oid}")]]
                elif status == 'at_client':
                     kb = [[InlineKeyboardButton("✅ Доставлен", callback_data=f"finish_{oid}")],
                           [InlineKeyboardButton("🚫 Клиент отсутствует", callback_data=f"client_not_home_{oid}")]]
            else:
                if status != 'delivered':
                    kb = [[InlineKeyboardButton(f"✅ Возвращен в магазин", callback_data=f"finish_return_{oid}")] ]

            try:
                sent_msg = await ctx.application.bot.send_message(
                    courier_id_updated,
                    f"❗ <b>ЗАКАЗ #{oid} ОБНОВЛЕН!</b>\n\n{report}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup(kb)
                )
                if getattr(sent_msg, "message_id", None):
                    existing = await get_courier_message_records(oid)
                    for cid, mid in existing:
                        try:
                            await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"❌ <b>Заказ #{oid} неактуален (обновлён)</b>")
                        except:
                            pass
                    await delete_courier_message_records(oid)
                    await save_courier_message_record(oid, courier_id_updated, sent_msg.message_id)
            except Exception as e:
                logger.warning(f"Не удалось отправить обновление курьеру: {e}")
        elif new_order_data["status"] == 'new':
            await send_order_to_couriers(oid, ctx.application)
            reply_msg = await update.message.reply_html(f"✏️ <b>Заказ #{oid} обновлён</b>\nСтарые кнопки деактивированы. Новая версия разослана всем курьерам.")
            await register_bot_message(ctx, reply_msg.message_id)
        
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    reply_msg = await update.message.reply_html("Операция отменена.")
    await register_bot_message(ctx, reply_msg.message_id)
    return ConversationHandler.END


# ========== MYORDERS ==========
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text("⛔ Доступ запрещён")
        return

    keyboard = [
        ["🧾 Заказы"],
        ["👥 Пользователи"],
        ["🏪 Магазины"],
        ["🛵 Курьеры"],
        ["📢 Рассылка"],
        ["⚙️ Настройки"],
        ["📜 Логи"],
    ]

    reply_markup = ReplyKeyboardMarkup(
        keyboard,
        resize_keyboard=True
    )

    await update.message.reply_text(
        "👑 Админ-панель",
        reply_markup=reply_markup
    )

async def myorders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    await purge_chat_history(ctx.application, tg, ctx)
    await delete_message_after_command(update, ctx)
    
    role = await get_role(tg)
    if not role:
        reply_msg = await update.message.reply_text("Вы не выбрали роль. Нажмите /start")
        await register_bot_message(ctx, reply_msg.message_id)
        return
        
    ACTIVE_STATUSES = ('new', 'taken', 'at_shop', 'on_delivery', 'at_client')
    
    async with aiosqlite.connect(DB_PATH) as db:
        if role == "courier":
            query = "SELECT * FROM orders WHERE courier_tg_id=? AND status IN (?, ?, ?, ?, ?) ORDER BY created_at DESC"
            params = (tg,) + ACTIVE_STATUSES
        else:
            query = "SELECT * FROM orders WHERE shop_tg_id=? AND status IN (?, ?, ?, ?, ?) ORDER BY created_at DESC"
            params = (tg,) + ACTIVE_STATUSES
        
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
        
    if not rows:
        reply_msg = await update.message.reply_html("<b>Нет активных заказов</b>")
        await register_bot_message(ctx, reply_msg.message_id)
        return
        
    for row in rows:
        (
            order_id, shop_tg_id, courier_tg_id,
            from_address, shop_contact,
            to_address, to_apt,
            client_name, client_phone, price,
            status, log, created_at, return_for, paid_to_courier, paid_at
        ) = row
        o = {
            "id": order_id, "shop_tg_id": shop_tg_id, "courier_tg_id": courier_tg_id,
            "from_address": from_address, "shop_contact": shop_contact, "to_address": to_address,
            "to_apt": to_apt, "client_name": client_name, "client_phone": client_phone,
            "price": price, "status": status, "log": log, "return_for": return_for,
            "paid_to_courier": paid_to_courier, "paid_at": paid_at
        }
        courier_name = await get_username(courier_tg_id, ctx.application)
        txt = html_report(order_id, o, courier_name, include_log=False)
        final_kb = []
        is_return_order = return_for is not None
        
        if role == "shop":
            kb = []
            if status in ('new', 'taken'):
                 kb.append(InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_{order_id}"))
            if status not in ('failed_delivery', 'cancelled_70_percent', 'completed_with_return'):
                 kb.append(InlineKeyboardButton("❌ Отменить", callback_data=f"shop_cancel_{order_id}"))
            if courier_tg_id:
                kb.append(InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_tg_id}"))
            final_kb = [[b] for b in kb]
        
        else:
            if is_return_order:
                if shop_tg_id:
                    final_kb.append([InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_tg_id}")])
                if status != 'delivered':
                    final_kb.append([InlineKeyboardButton(f"✅ Возвращен в магазин", callback_data=f"finish_return_{order_id}")] )
            
            elif status == "taken":
                if shop_tg_id:
                    final_kb.append([InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_tg_id}")]) 
                final_kb += [
                    [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{order_id}")],
                    [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")]
                ]
            elif status == "at_shop":
                if shop_tg_id:
                    final_kb.append([InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_tg_id}")])
                final_kb += [
                    [InlineKeyboardButton("✅ Забрал", callback_data=f"picked_up_{order_id}")],
                    [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")]
                ]
            elif status == "on_delivery":
                final_kb += [
                    [InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{order_id}")]
                ]
            elif status == "at_client":
                if shop_tg_id:
                    final_kb.append([InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_tg_id}")])
                final_kb += [
                    [InlineKeyboardButton("✅ Доставлен", callback_data=f"finish_{order_id}")],
                    [InlineKeyboardButton("🚫 Клиент отсутствует", callback_data=f"client_not_home_{order_id}")]
                ]

        if final_kb:
            msg = await update.effective_chat.send_message(txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(final_kb))
        else:
            msg = await update.effective_chat.send_message(txt, parse_mode="HTML")
        await register_bot_message(ctx, msg.message_id)


# ========== UNPAID_ORDERS ==========
async def unpaid_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    await purge_chat_history(ctx.application, tg, ctx)
    await delete_message_after_command(update, ctx)
    
    role = await get_role(tg)
    if not role:
        reply_msg = await update.message.reply_text("Вы не выбрали роль. Нажмите /start")
        await register_bot_message(ctx, reply_msg.message_id)
        return
        
    PAYABLE_STATUSES = ('delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return')
    
    async with aiosqlite.connect(DB_PATH) as db:
        if role == "shop":
            query = f"SELECT * FROM orders WHERE shop_tg_id=? AND status IN ({', '.join('?'*len(PAYABLE_STATUSES))}) AND paid_to_courier < ?"
            params = (tg,) + PAYABLE_STATUSES + (PAYMENT_STATUS_CONFIRMED,)
        else:
            query = f"SELECT * FROM orders WHERE courier_tg_id=? AND status IN ({', '.join('?'*len(PAYABLE_STATUSES))}) AND paid_to_courier < ?"
            params = (tg,) + PAYABLE_STATUSES + (PAYMENT_STATUS_CONFIRMED,)
        
        cur = await db.execute(query, params)
        rows = await cur.fetchall()

    if not rows:
        reply_msg = await update.message.reply_html("<b>Нет неоплаченных заказов.</b>")
        await register_bot_message(ctx, reply_msg.message_id)
        return

    for row in rows:
        (
            order_id, shop_tg_id, courier_tg_id,
            from_address, shop_contact,
            to_address, to_apt,
            client_name, client_phone, price,
            status, log, created_at, return_for, paid_to_courier, paid_at
        ) = row
        o = {
            "id": order_id, "shop_tg_id": shop_tg_id, "courier_tg_id": courier_tg_id,
            "from_address": from_address, "shop_contact": shop_contact, "to_address": to_address,
            "to_apt": to_apt, "client_name": client_name, "client_phone": client_phone,
            "price": price, "status": status, "log": log, "return_for": return_for,
            "paid_to_courier": paid_to_courier, "paid_at": paid_at
        }
        
        amount = float(price or 0)
        if status == 'cancelled_70_percent':
             amount = round(amount * 0.70, 2)
             
        report = (
            f"<b>ЗАКАЗ #{order_id}</b> ({'↩️ ВОЗВРАТ' if return_for else 'Прямой'})\n"
            f"От: {o['from_address']} / К: {o['to_address']}\n"
            f"Сумма доставки: {int(amount)} ₽\n"
            f"Статус оплаты: "
        )

        kb = []
        if role == "shop":
            if paid_to_courier == PAYMENT_STATUS_UNPAID:
                report += "Не оплачено"
                kb.append([InlineKeyboardButton("💸 Оплачено", callback_data=f"shop_mark_paid_{order_id}")])
            elif paid_to_courier == PAYMENT_STATUS_MARKED_PAID:
                report += "Ожидание подтверждения"
                kb.append([InlineKeyboardButton("⏳ Курьер подтверждает", callback_data=f"info_confirm_wait")])
            elif paid_to_courier == PAYMENT_STATUS_CONFIRMED:
                report += "УСПЕШНО"
        
        elif role == "courier":
            if paid_to_courier == PAYMENT_STATUS_UNPAID:
                report += "Не оплачено"
                kb.append([InlineKeyboardButton("🔴 Жду оплаты", callback_data=f"info_wait")])
            elif paid_to_courier == PAYMENT_STATUS_MARKED_PAID:
                report += "Оплачено магазином"
                kb.append([InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"courier_confirm_payment_{order_id}")])
            elif paid_to_courier == PAYMENT_STATUS_CONFIRMED:
                report += "УСПЕШНО"

        msg = await update.effective_chat.send_message(report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        await register_bot_message(ctx, msg.message_id)


# ========== PAYMENT HANDLERS ==========
async def shop_mark_paid_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Запрос на подтверждение отправлен курьеру.")
    order_id = int(q.data.split("_")[3])
    o = await get_order(order_id)
    shop_id = q.from_user.id
    
    if o["shop_tg_id"] != shop_id or o["paid_to_courier"] != PAYMENT_STATUS_UNPAID:
         await q.edit_message_text("❌ Ошибка: Заказ не принадлежит вам или уже оплачен.")
         return

    courier_id = o["courier_tg_id"]
    await update_order(order_id, paid=PAYMENT_STATUS_MARKED_PAID, 
                       log_add=f"Магазин отметил оплату. Требуется подтверждение курьером.")

    report = f"<b>ЗАКАЗ #{order_id}</b>\nСумма: {int(o['price'])} ₽\nСтатус оплаты: Ожидание подтверждения"
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⏳ Курьер подтверждает", callback_data=f"info_confirm_wait")]])
    try:
        await q.edit_message_text(report, parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.warning(f"Ошибка редактирования сообщения магазина: {e}")

    kb_courier = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"courier_confirm_payment_{order_id}")]])
    try:
        await ctx.application.bot.send_message(
            courier_id,
            f"🔔 <b>ПОСТУПИЛА ОПЛАТА!</b>\nМагазин #{shop_id} отметил заказ #{order_id} как оплаченный. Подтвердите получение.",
            parse_mode="HTML",
            reply_markup=kb_courier
        )
    except:
        pass


async def courier_confirm_payment_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer("Оплата подтверждена.")
    order_id = int(q.data.split("_")[3])
    o = await get_order(order_id)
    courier_id = q.from_user.id
    shop_id = o["shop_tg_id"]
    courier_name = await get_username(courier_id, ctx.application)
    
    if o["courier_tg_id"] != courier_id or o["paid_to_courier"] != PAYMENT_STATUS_MARKED_PAID:
         await q.edit_message_text("❌ Ошибка: Вы не можете подтвердить этот платеж.")
         return
         
    amount = float(o['price'] or 0)
    if o['status'] == 'cancelled_70_percent':
         amount = round(amount * 0.70, 2)
         
    await update_order(order_id, paid=PAYMENT_STATUS_CONFIRMED, 
                       log_add=f"Курьер подтвердил получение {int(amount)} ₽. Оплата успешно завершена.")
                       
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO payments (order_id, shop_tg_id, courier_tg_id, amount, paid_at) VALUES (?, ?, ?, ?, ?)",
                         (order_id, shop_id, courier_id, amount, timestamp))
        await db.commit()

    report_courier = f"<b>ЗАКАЗ #{order_id}</b>\nСумма: {int(amount)} ₽\nСтатус оплаты: УСПЕШНО"
    kb_success = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Успешно", callback_data=f"info_success")]])
    try:
        await q.edit_message_text(report_courier, parse_mode="HTML", reply_markup=kb_success)
    except Exception as e:
        logger.warning(f"Ошибка редактирования сообщения курьера: {e}")

    report_shop = f"✅ <b>КУРЬЕР ПОДТВЕРДИЛ!</b>\nКурьер {courier_name} подтвердил получение оплаты за заказ #{order_id} ({int(amount)} ₽). Статус: УСПЕШНО."
    try:
        await ctx.application.bot.send_message(shop_id, report_shop, parse_mode="HTML")
    except:
        pass


# ========== COMPLETED_ORDERS ==========
async def completed_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    await purge_chat_history(ctx.application, tg, ctx)
    await delete_message_after_command(update, ctx)
    role = await get_role(tg)
    if not role:
        reply_msg = await update.message.reply_text("Вы не выбрали роль. Нажмите /start")
        await register_bot_message(ctx, reply_msg.message_id)
        return
    await send_completed_for_role(tg, role, ctx)


async def send_completed_for_role(tg: int, role: str, ctx: ContextTypes.DEFAULT_TYPE):
    COMPLETED_STATUSES = ('delivered', 'cancelled', 'cancelled_70_percent', 'failed_delivery', 'completed_with_return')
    
    if role == "courier":
        async with aiosqlite.connect(DB_PATH) as db:
            query = "SELECT id, price, status, return_for, paid_to_courier, shop_tg_id FROM orders WHERE courier_tg_id=? AND status IN (?, ?, ?, ?, ?) ORDER BY created_at DESC"
            params = (tg,) + COMPLETED_STATUSES
            cur = await db.execute(query, params)
            rows = await cur.fetchall()
        
        if not rows:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 История как магазин", callback_data="hist_switch_shop")]])
            reply_msg = await ctx.application.bot.send_message(tg, "📘 <b>История как курьер</b>\n\nНет завершенных/отмененных записей.", parse_mode="HTML", reply_markup=kb)
            await register_bot_message(ctx, reply_msg.message_id)
            return
            
        lines = ["📘 <b>История как курьер (Завершенные)</b>\n"]
        total_earned_calc = 0.0
        
        for r in rows:
            oid, price, status, return_for, paid_to_courier, shop_tg_id = r
            price_display = int(price or 0)
            if status == 'cancelled_70_percent':
                 price_to_count = round(float(price or 0) * 0.70, 2)
                 lines.append(f"#{oid} — Отмена (70%): {int(price_to_count)} ₽ | Статус: {status}")
                 total_earned_calc += price_to_count
            else:
                lines.append(f"#{oid} — Цена: {price_display} ₽ | Статус: {status}")
                if status in ('delivered', 'failed_delivery', 'cancelled', 'completed_with_return'): 
                    total_earned_calc += float(price or 0)
                 
        footer = f"\n\n――――――――――\n💰 <b>Итого заработал курьер: {int(total_earned_calc)} ₽</b>"
        text = "\n".join(lines) + footer
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 История как магазин", callback_data="hist_switch_shop")]])
        reply_msg = await ctx.application.bot.send_message(tg, text, parse_mode="HTML", reply_markup=kb)
        await register_bot_message(ctx, reply_msg.message_id)
        
    else: 
        async with aiosqlite.connect(DB_PATH) as db:
            query = "SELECT id, price, status, return_for, paid_to_courier FROM orders WHERE shop_tg_id=? AND status IN (?, ?, ?, ?, ?) ORDER BY created_at DESC"
            params = (tg,) + COMPLETED_STATUSES
            cur = await db.execute(query, params)
            rows = await cur.fetchall()
            
        if not rows:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛵 История как курьер", callback_data="hist_switch_courier")]])
            reply_msg = await ctx.application.bot.send_message(tg, "📗 <b>История как магазин</b>\n\nНет завершенных/отмененных записей.", parse_mode="HTML", reply_markup=kb)
            await register_bot_message(ctx, reply_msg.message_id)
            return
            
        lines = ["📗 <b>История как магазин (Завершенные)</b>\n"]
        for r in rows:
            oid, price, status, return_for, paid_to_courier = r
            pay_status = "УСПЕШНО" if paid_to_courier == PAYMENT_STATUS_CONFIRMED else ("ОПЛАЧЕНО (ожидание)" if paid_to_courier == PAYMENT_STATUS_MARKED_PAID else "НЕ ОПЛАЧЕНО")
            price_display = int(price or 0)
            
            if status == 'cancelled_70_percent':
                 price_display = int(round(float(price or 0) * 0.70, 0))
                 lines.append(f"#{oid} — Отмена (70%): {price_display} ₽ | Статус: {status} | {pay_status}")
            else:
                 lines.append(f"#{oid} — Сумма курьеру: {price_display} ₽ | Статус: {status} | {pay_status}")

        async with aiosqlite.connect(DB_PATH) as db:
            cur2 = await db.execute("SELECT SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END) FROM orders WHERE shop_tg_id=? AND paid_to_courier=2", (tg,))
            row = await cur2.fetchone()
        paid_sum = int(row[0] or 0) if row and row[0] else 0
        async with aiosqlite.connect(DB_PATH) as db:
            cur3 = await db.execute("SELECT SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END) FROM orders WHERE shop_tg_id=? AND paid_to_courier < 2 AND status IN (?, ?, ?, ?, ?)", (tg,) + COMPLETED_STATUSES)
            row2 = await cur3.fetchone()
        unpaid_sum = int(row2[0] or 0) if row2 and row2[0] else 0
        
        footer = f"\n\n――――――――――\n💰 <b>Оплачено курьерам: {paid_sum} ₽</b>\n💸 <b>Не оплачено (завершенные): {unpaid_sum} ₽</b>"
        text = "\n".join(lines) + footer
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛵 История как курьер", callback_data="hist_switch_courier")]])
        reply_msg = await ctx.application.bot.send_message(tg, text, parse_mode="HTML", reply_markup=kb)
        await register_bot_message(ctx, reply_msg.message_id)


async def history_switch_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
         await q.edit_message_reply_markup(reply_markup=None)
    except:
         pass
    if q.data == "hist_switch_shop":
        await send_completed_for_role(q.from_user.id, "shop", ctx)
    else:
        await send_completed_for_role(q.from_user.id, "courier", ctx)


# ========== COURIER ACTIONS ==========
async def picked_up(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    order_id = int(q.data.split("_")[2])
    courier_id = q.from_user.id
    o = await get_order(order_id)

    if not o or o["courier_tg_id"] != courier_id or o["status"] != 'at_shop':
        await q.answer("⛔ Заказ неактивен.")
        return

    courier_name = await get_username(courier_id, ctx.application)
    shop_id = o["shop_tg_id"]

    await update_order(order_id, status="on_delivery", log_add=f"Курьер {courier_name} забрал товар.")
    o = await get_order(order_id)
    report = html_report(order_id, o, courier_name, include_log=True)
    kb = [[InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{order_id}")]]
    
    try:
        await q.edit_message_text(report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    except:
        sent_msg = await ctx.application.bot.send_message(courier_id, report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        await register_bot_message(ctx, sent_msg.message_id)
    
    try:
        msg = f"✅ Курьер <b>{courier_name}</b> забрал товар для заказа #{order_id}."
        await ctx.application.bot.send_message(shop_id, msg, parse_mode="HTML")
    except:
        pass
        
async def take_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    courier_id = q.from_user.id
    order_id = int(q.data.split("_")[1])
    o = await get_order(order_id)
    if not o or o["status"] != "new":
        o_status = o["status"] if o else "удален"
        try:
            await q.edit_message_text(f"⛔ <b>Заказ #{order_id} недоступен.</b>\nСтатус: {o_status.upper()}.", parse_mode="HTML", reply_markup=None)
        except:
            pass
        return
    
    courier_name = await get_username(courier_id, ctx.application)
    await update_order(order_id, status="taken", courier=courier_id, log_add=f"Курьер {courier_name} взял заказ")
    o = await get_order(order_id)
    txt = html_report(order_id, o, courier_name, include_log=True)
    
    kb = [
        [InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={o['shop_tg_id']}")],
        [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{order_id}")],
        [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")]
    ]
    try:
        await q.edit_message_text(f"🚀 Вы взяли заказ #{order_id}. Направляйтесь к магазину.", reply_markup=None)
    except:
        pass
        
    try:
        sent_msg = await ctx.application.bot.send_message(courier_id, txt, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        await save_courier_message_record(order_id, courier_id, sent_msg.message_id)
        await register_bot_message(ctx, sent_msg.message_id)
    except:
        pass

    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            if cid != courier_id:
                try:
                    await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"⛔ <b>Заказ #{order_id} уже взят</b>")
                except:
                    pass
                try:
                    await delete_specific_courier_message_record(order_id, cid, mid)
                except:
                    pass
    except:
        pass
        
    try:
        shop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_id}")]])
        await ctx.application.bot.send_message(
            o["shop_tg_id"],
            f"🚀 Курьер {courier_name} взял заказ #{order_id}",
            parse_mode="HTML",
            reply_markup=shop_kb
        )
    except:
        pass


async def arrived(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    if len(parts) < 3: return
    where = parts[1]
    order_id = int(parts[2])
    courier_id = q.from_user.id
    o = await get_order(order_id)
    
    status_map = {"shop": ("at_shop", "в магазин"), "client": ("at_client", "получателю")}
    
    if where == "shop" and o["status"] != 'taken':
        await q.answer("⛔ Рано или поздно.")
        return
    elif where == "client" and o["status"] != 'on_delivery':
        await q.answer("⛔ Рано или поздно.")
        return

    if not o or o["courier_tg_id"] != courier_id:
        await q.answer("⛔ Заказ неактивен.")
        return

    new_status, location_name = status_map[where]
    courier_name = await get_username(courier_id, ctx.application)
    shop_id = o["shop_tg_id"]

    await update_order(order_id, status=new_status, log_add=f"Курьер прибыл {location_name}")
    o = await get_order(order_id)
    report = html_report(order_id, o, courier_name, include_log=True)
    
    kb = []
    if where == "shop":
        kb = [
            [InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_id}")],
            [InlineKeyboardButton("✅ Забрал", callback_data=f"picked_up_{order_id}")],
            [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")]
        ]
    elif where == "client":
        kb = [
            [InlineKeyboardButton("✅ Доставлен", callback_data=f"finish_{order_id}")],
            [InlineKeyboardButton("🚫 Клиент отсутствует", callback_data=f"client_not_home_{order_id}")]
        ]

    try:
        await q.edit_message_text(report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
    except:
        sent_msg = await ctx.application.bot.send_message(courier_id, report, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))
        await register_bot_message(ctx, sent_msg.message_id) 
    
    try:
        msg = f"📍 Курьер <b>{courier_name}</b> прибыл {location_name} (заказ #{order_id})"
        await ctx.application.bot.send_message(shop_id, msg, parse_mode="HTML")
    except:
        pass


async def cancel_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    order_id = int(q.data.split("_")[1])
    courier_id = q.from_user.id
    o = await get_order(order_id)
    if not o or o["courier_tg_id"] != courier_id:
        await q.edit_message_text("⛔ Этот заказ не ваш")
        return
    if o.get("return_for") is not None:
        await q.edit_message_text("⛔ Возвратный заказ не может быть отменен курьером.")
        return

    courier_name = await get_username(courier_id, ctx.application)
    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            try:
                await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"❌ <b>Заказ #{order_id} отменен курьером</b>")
            except:
                pass
        await delete_courier_message_records(order_id)
    except:
        pass

    await update_order(order_id, status="new", courier=0, log_add=f"Курьер {courier_name} отменил заказ")
    o_updated = await get_order(order_id)
    try:
        await ctx.application.bot.send_message(o["shop_tg_id"], f"❌ Курьер {courier_name} отменил заказ #{order_id}. Заказ возвращен в пул.", parse_mode="HTML")
    except:
        pass
    await send_order_to_couriers(order_id, ctx.application)
    try:
        report = html_report(order_id, o_updated, courier_name, include_log=True)
        await q.edit_message_text(report, parse_mode="HTML", reply_markup=None)
    except:
        pass


async def finish_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data.split("_")[1] == 'return': return
    order_id = int(q.data.split("_")[1])
    courier_id = q.from_user.id
    o = await get_order(order_id)
    if not o or o["courier_tg_id"] != courier_id or o["status"] != 'at_client':
        await q.answer("⛔ Заказ неактивен.")
        return
        
    courier_name = await get_username(courier_id, ctx.application)
    shop_id = o["shop_tg_id"]
    
    await update_order(order_id, status="delivered", log_add=f"Курьер {courier_name} завершил заказ")
    o = await get_order(order_id)
    report = html_report(order_id, o, courier_name, include_log=True)
    try:
        await q.edit_message_text(report, parse_mode="HTML", reply_markup=None)
    except:
        pass
    try:
        shop_kb = None
        if courier_id:
            shop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_id}")]])
        await ctx.application.bot.send_message(shop_id, report, parse_mode="HTML", reply_markup=shop_kb)
    except:
        pass
    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            if cid == courier_id:
                await deactivate_or_delete_message(ctx.application, cid, mid, text_override=report)
                continue
            try:
                await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"ℹ️ <b>Заказ #{order_id} завершён</b>")
            except:
                pass
            try:
                await delete_specific_courier_message_record(order_id, cid, mid)
            except:
                pass
    except:
        pass


async def finish_return_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        order_id = int(q.data.split("_")[2]) 
    except:
        return
    courier_id = q.from_user.id
    o = await get_order(order_id)

    if not o or o["courier_tg_id"] != courier_id:
        await q.answer("⛔ Заказ неактивен.")
        return
    if o["return_for"] is None:
        return

    courier_name = await get_username(courier_id, ctx.application)
    await update_order(order_id, status="delivered", log_add=f"Курьер {courier_name} завершил возврат товара.")
    o = await get_order(order_id)
    report = html_report(order_id, o, courier_name, include_log=True)
    try:
        await q.edit_message_text(report, parse_mode="HTML", reply_markup=None)
    except:
        pass
    try:
        shop_id = o["shop_tg_id"]
        shop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_id}")]])
        await ctx.application.bot.send_message(shop_id, f"✅ <b>Возвратный заказ #{o['return_for']}.{order_id} завершен!</b>\n\n{report}", parse_mode="HTML", reply_markup=shop_kb)
    except:
        pass
    await delete_courier_message_records(order_id)


async def client_not_home_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    courier_id = q.from_user.id
    parts = q.data.split("_")
    try:
        order_id = int(parts[-1]) 
    except:
        return
    orig = await get_order(order_id)
    if not orig or orig["courier_tg_id"] != courier_id or orig["status"] != "at_client":
        return
    
    courier_name = await get_username(courier_id, ctx.application)
    await update_order(order_id, status="failed_delivery", log_add=f"Клиент отсутствует. Требуется возврат.")
    orig = await get_order(order_id) 
    report = html_report(order_id, orig, courier_name, include_log=True)

    try:
        await q.edit_message_text(report, parse_mode="HTML", reply_markup=None)
        await delete_courier_message_records(order_id)
    except:
        pass
    try:
        shop_id = orig["shop_tg_id"]
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("Создать Возвратный Заказ", callback_data=f"create_return_from_fail_{order_id}")]
        ])
        await ctx.application.bot.send_message(
            shop_id, 
            f"⚠️ <b>КЛИЕНТ ОТСУТСТВУЕТ</b> (Заказ #{order_id}).\n\nКурьер {courier_name} ожидает решения.\nСоздайте новый заказ для возврата товара.",
            parse_mode="HTML",
            reply_markup=kb
        )
    except:
        pass


async def shop_cancel_from_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        order_id = int(q.data.split("_")[2])
    except:
        return
    shop_id = q.from_user.id
    o = await get_order(order_id)
    if not o or o["shop_tg_id"] != shop_id:
        return
    courier_id = o["courier_tg_id"]
    courier_name = await get_username(courier_id, ctx.application)
    
    is_on_delivery = o["status"] in ("on_delivery", "at_client")
    price = o["price"]
    log_message = "Магазин отменил заказ"
    courier_param = None
    status = "cancelled"
    paid_status = PAYMENT_STATUS_UNPAID

    if is_on_delivery and courier_id and o["paid_to_courier"] == PAYMENT_STATUS_UNPAID:
        amount = await pay_courier_fixed_amount(order_id, courier_id, price, "Компенсация 70% за отмену в пути")
        log_message = f"Магазин отменил заказ. Компенсация 70%."
        status = "cancelled_70_percent"
        courier_param = courier_id
        paid_status = PAYMENT_STATUS_UNPAID
    elif o["status"] == "taken" or o["status"] == "at_shop":
        status = "new"
        courier_param = 0
        paid_status = PAYMENT_STATUS_UNPAID
    elif o["status"] == "new":
        status = "cancelled"
        courier_param = 0
        paid_status = PAYMENT_STATUS_UNPAID
    else:
        status = "cancelled"
        courier_param = courier_id if courier_id else 0
        paid_status = o["paid_to_courier"]

    await update_order(order_id, status=status, courier=courier_param, log_add=log_message, paid=paid_status)
    o_updated = await get_order(order_id)
    report = html_report(order_id, o_updated, courier_name, include_log=True)
    await q.edit_message_text(report, parse_mode="HTML", reply_markup=None)
    
    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            try:
                await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"❌ <b>Заказ #{order_id} отменён магазином</b>")
            except:
                pass
        await delete_courier_message_records(order_id)
    except:
        pass
        
    if courier_id:
        try:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_id}")]])
            await ctx.application.bot.send_message(courier_id, f"❌ <b>Заказ #{order_id} отменён магазином</b>\n\n{report}", parse_mode="HTML", reply_markup=kb)
        except:
            pass
    if status == 'new':
        await send_order_to_couriers(order_id, ctx.application)


# ========== FINANCE ==========
async def finance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    await purge_chat_history(ctx.application, tg, ctx)
    await delete_message_after_command(update, ctx)
    role = await get_role(tg)
    if role != "shop": return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Неоплаченные доставки", callback_data="finance_unpaid")],
        [InlineKeyboardButton("💵 Оплаченные доставки", callback_data="finance_paid")],
        [InlineKeyboardButton("📊 Сводка", callback_data="finance_summary")]
    ])
    reply_msg = await update.message.reply_html("Финансы магазина:", reply_markup=kb)
    await register_bot_message(ctx, reply_msg.message_id)


async def finance_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = q.from_user.id
    parts = q.data.split("_")
    action = parts[1] if len(parts) > 1 else None
    
    if action == "finance":
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Неоплаченные доставки", callback_data="finance_unpaid")],
            [InlineKeyboardButton("💵 Оплаченные доставки", callback_data="finance_paid")],
            [InlineKeyboardButton("📊 Сводка", callback_data="finance_summary")]
        ])
        await q.edit_message_text("Финансы магазина:", reply_markup=kb)
        return
        
    if action == "unpaid":
        await q.edit_message_text("Используйте команду /unpaid для просмотра и управления неоплаченными заказами.")
    elif action == "paid":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, price, paid_at, status FROM orders WHERE shop_tg_id=? AND paid_to_courier=? ORDER BY paid_at DESC", (tg, PAYMENT_STATUS_CONFIRMED))
            rows = await cur.fetchall()
        if not rows:
            await q.edit_message_text("Нет оплаченных доставок.")
            return
        total = 0.0
        text_lines = ["💵 <b>Оплаченные доставки</b>\n"]
        for r in rows:
            oid, price, paid_at, status = r
            current_amount = float(price or 0)
            if status == 'cancelled_70_percent':
                 price_to_pay = round(current_amount * 0.70, 2)
                 total += price_to_pay
                 text_lines.append(f"#{oid} — {int(price_to_pay)} ₽ | Оплачено: {paid_at or '—'} (70%)")
            else:
                 total += current_amount
                 text_lines.append(f"#{oid} — {int(price or 0)} ₽ | Оплачено: {paid_at or '—'}")
        text_lines.append(f"\nИтого оплачено: {int(total)} ₽")
        kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="finance")]])
        await q.edit_message_text("\n".join(text_lines), parse_mode="HTML", reply_markup=kb_back)
    elif action == "summary":
        async with aiosqlite.connect(DB_PATH) as db:
            cur_paid = await db.execute("SELECT SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END) FROM orders WHERE shop_tg_id=? AND paid_to_courier=?", (tg, PAYMENT_STATUS_CONFIRMED))
            row_paid = await cur_paid.fetchone()
            cur_unpaid = await db.execute("SELECT SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END) FROM orders WHERE shop_tg_id=? AND paid_to_courier < ? AND status IN ('delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return')", (tg, PAYMENT_STATUS_CONFIRMED))
            row_unpaid = await cur_unpaid.fetchone()
        paid_sum = int(row_paid[0] or 0) if row_paid and row_paid[0] else 0
        unpaid_sum = int(row_unpaid[0] or 0) if row_unpaid and row_unpaid[0] else 0
        text = f"📊 <b>Сводка по оплатам</b>\n\nОплачено: {paid_sum} ₽\nНе оплачено (завершенные): {unpaid_sum} ₽"
        kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="finance")]])
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_back)


async def finance_pay_all_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    shop_id = q.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT id, courier_tg_id, price, status FROM orders 
            WHERE shop_tg_id=? AND paid_to_courier < ?
            AND status IN ('delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return')
        """, (shop_id, PAYMENT_STATUS_CONFIRMED))
        rows = await cur.fetchall()
    if not rows:
        await q.edit_message_text("Нет неоплаченных заказов.")
        return
        
    await q.edit_message_text("⚠️ В целях безопасности, массовая оплата должна быть подтверждена каждым курьером. Мы инициируем процесс.")
    count = 0
    for r in rows:
        oid, courier_id, price, status = r
        if r[3] == PAYMENT_STATUS_UNPAID:
            await update_order(oid, paid=PAYMENT_STATUS_MARKED_PAID, log_add="Магазин инициировал массовую оплату.")
            count += 1
            if courier_id:
                try:
                    kb_courier = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Подтвердить оплату", callback_data=f"courier_confirm_payment_{oid}")]])
                    await ctx.application.bot.send_message(courier_id, f"🔔 <b>ПОСТУПИЛА МАССОВАЯ ОПЛАТА!</b>\nМагазин #{shop_id} отметил заказ #{oid} как оплаченный.", parse_mode="HTML", reply_markup=kb_courier)
                except:
                    pass
    await ctx.application.bot.send_message(shop_id, f"Инициирована оплата для {count} заказов.", parse_mode="HTML")
    

async def payouts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    await purge_chat_history(ctx.application, tg, ctx)
    await delete_message_after_command(update, ctx)
    role = await get_role(tg)
    if role != "courier": return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Полученные выплаты", callback_data="payouts_received")],
        [InlineKeyboardButton("💸 Должны выплатить", callback_data="payouts_unpaid")],
        [InlineKeyboardButton("📊 Сводка", callback_data="payouts_summary")]
    ])
    reply_msg = await update.message.reply_html("Выплаты:", reply_markup=kb)
    await register_bot_message(ctx, reply_msg.message_id)


async def payouts_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = q.from_user.id
    parts = q.data.split("_")
    action = parts[1] if len(parts) > 1 else None
    
    if action == "payouts": 
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💰 Полученные выплаты", callback_data="payouts_received")],
            [InlineKeyboardButton("💸 Должны выплатить", callback_data="payouts_unpaid")],
            [InlineKeyboardButton("📊 Сводка", callback_data="payouts_summary")]
        ])
        await q.edit_message_text("Выплаты:", reply_markup=kb)
        return
    if action == "received":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, price, paid_at, status FROM orders WHERE courier_tg_id=? AND paid_to_courier=? ORDER BY paid_at DESC", (tg, PAYMENT_STATUS_CONFIRMED))
            rows = await cur.fetchall()
        if not rows:
            await q.edit_message_text("Нет полученных выплат.")
            return
        total = 0.0
        text_lines = ["💰 <b>Полученные выплаты</b>\n"]
        for r in rows:
            oid, price, paid_at, status = r
            current_amount = float(price or 0)
            if status == 'cancelled_70_percent':
                 price_paid = round(current_amount * 0.70, 2)
                 total += price_paid
                 text_lines.append(f"#{oid} — {int(price_paid)} ₽ | Оплачено: {paid_at or '—'} (70%)")
            else:
                 total += current_amount
                 text_lines.append(f"#{oid} — {int(current_amount)} ₽ | Оплачено: {paid_at or '—'}")
        text_lines.append(f"\nИтого получено: {int(total)} ₽")
        kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="payouts")]])
        await q.edit_message_text("\n".join(text_lines), parse_mode="HTML", reply_markup=kb_back)
    elif action == "unpaid":
        await q.edit_message_text("Используйте команду /unpaid для просмотра и управления неоплаченными заказами.")
    elif action == "summary":
        async with aiosqlite.connect(DB_PATH) as db:
            cur1 = await db.execute("SELECT SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END) FROM orders WHERE courier_tg_id=? AND status IN ('delivered', 'failed_delivery', 'cancelled_70_percent', 'completed_with_return')", (tg,))
            row1 = await cur1.fetchone()
            cur2 = await db.execute("SELECT SUM(CASE WHEN status='cancelled_70_percent' THEN price * 0.70 ELSE price END) FROM orders WHERE courier_tg_id=? AND paid_to_courier=?", (tg, PAYMENT_STATUS_CONFIRMED))
            row2 = await cur2.fetchone()
        total_expected = round(float(row1[0] or 0), 2)
        total_paid = round(float(row2[0] or 0), 2)
        total_unpaid = total_expected - total_paid
        text = f"📊 <b>Ваша финансовая сводка</b>\n\nЗаработано (заверш.): {int(total_expected)} ₽\nПолучено: {int(total_paid)} ₽\nДолжны выплатить: {int(total_unpaid)} ₽"
        kb_back = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data="payouts")]])
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_back)


async def error_handler(update, ctx):
    logger.error("Ошибка:", exc_info=ctx.error)


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.post_init = init_db
    app.add_error_handler(error_handler)

    # 1. Глобальная проверка блокировки (Группа -1 выполняется самой первой)
    app.add_handler(TypeHandler(Update, global_block_guard), group=-1)

    # ====== Conversation: создание заказа ======
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new_order", new_order_start),
            CallbackQueryHandler(edit_order_start, pattern="^edit_"),
            CallbackQueryHandler(
                shop_set_return_order_entry,
                pattern="^create_return_from_fail_"
            ),
        ],
        states={
            ADDRESS_FROM: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_from)
            ],
            CONTACT_SHOP: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_shop_contact)
            ],
            ADDRESS_CLIENT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_address)
            ],
            CLIENT_PHONE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_phone)
            ],
            CLIENT_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_client_name)
            ],
            DELIVERY_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, step_price_final)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=False,
    )

    app.add_handler(conv)

    # ====== CallbackQuery handlers ======
    app.add_handler(CallbackQueryHandler(role_choice, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(take_order, pattern="^take_"))
    app.add_handler(CallbackQueryHandler(picked_up, pattern="^picked_up_"))
    app.add_handler(CallbackQueryHandler(finish_return_order, pattern="^finish_return_"))
    app.add_handler(CallbackQueryHandler(finish_order, pattern="^finish_"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_"))
    app.add_handler(CallbackQueryHandler(arrived, pattern="^arrived_"))
    app.add_handler(CallbackQueryHandler(shop_cancel_from_button, pattern="^shop_cancel_"))
    app.add_handler(CallbackQueryHandler(history_switch_cb, pattern="^hist_switch_"))
    app.add_handler(CallbackQueryHandler(client_not_home_entry, pattern="^client_not_home_"))
    app.add_handler(CallbackQueryHandler(shop_mark_paid_cb, pattern="^shop_mark_paid_"))
    app.add_handler(
        CallbackQueryHandler(
            courier_confirm_payment_cb,
            pattern="^courier_confirm_payment_"
        )
    )
    app.add_handler(
        CallbackQueryHandler(
            finance_pay_all_cb,
            pattern="^finance_pay_all$"
        )
    )
    app.add_handler(CallbackQueryHandler(finance_cb, pattern="^finance"))
    app.add_handler(CallbackQueryHandler(payouts_cb, pattern="^payouts"))

    app.add_handler(
        CallbackQueryHandler(
            lambda q, c: q.answer("Заказ в работе."),
            pattern="^info_"
        )
    )

    # ====== Контакт (ВАЖНО: до команд) ======
    app.add_handler(MessageHandler(filters.CONTACT, handle_contact))

    # ====== Команды ======
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("myorders", myorders))
    app.add_handler(CommandHandler("unpaid", unpaid_orders))
    app.add_handler(CommandHandler("completed", completed_orders))
    app.add_handler(CommandHandler("finance", finance))
    app.add_handler(CommandHandler("payouts", payouts))

    # ====== Plugins (ОПЦИОНАЛЬНО) ======
    try:
        from plugins.users import register as register_users
        register_users(app)
        logger.info("Users plugin loaded")
    except Exception as e:
        logger.warning(f"Users plugin NOT loaded: {e}")

    logger.info("BOT STARTED")
    app.run_polling()


if __name__ == "__main__":
    main()
