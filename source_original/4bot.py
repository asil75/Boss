 # bot.py — полный файл
import time
import aiosqlite
import logging
from typing import Optional
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    BotCommand,
    BotCommandScopeChat,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
    ConversationHandler,
)
import urllib.parse

# ========== Настройки ==========
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("delivery")

BOT_TOKEN = "8555882487:AAFyl9juLHiZ33FIjcretFe0U2yIDau1pYs"  # <- ПОМЕНИ на свой токен
DB_PATH = "db.sqlite3"

# FSM states
(
    ADDRESS_FROM,
    CONTACT_SHOP,
    CONTACT_CLIENT,
    DELIVERY_PRICE,
) = range(4)

RETURN_COURIER_PRICE = 10
RETURN_SHOP_PRICE = 11

# Commands for roles
SHOP_COMMANDS = [
    BotCommand("new_order", "Создать заказ"),
    BotCommand("myorders", "Активные заказы"),
    BotCommand("history", "История заказов"),
    BotCommand("finance", "Финансы магазина"),
    BotCommand("stats", "Статистика"),
    BotCommand("whoami", "Моя роль"),
    BotCommand("cancel", "Отменить действие"),
]

COURIER_COMMANDS = [
    BotCommand("myorders", "Мои доставки"),
    BotCommand("history", "История доставок"),
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
            # users: tg_id unique, role current role
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER UNIQUE,
                    role TEXT
                );
            """)
            # orders: main table
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
            # courier_messages: track sent message ids to manage deactivation
            await db.execute("""
                CREATE TABLE IF NOT EXISTS courier_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    courier_tg_id INTEGER,
                    message_id INTEGER,
                    created_at TEXT
                );
            """)
            # payments history (optional extra)
            await db.execute("""
                CREATE TABLE IF NOT EXISTS payments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    shop_tg_id INTEGER,
                    courier_tg_id INTEGER,
                    amount REAL,
                    paid_at TEXT
                );
            """)
            await db.commit()
        logger.info("DB ready")
    except Exception as e:
        logger.exception("Ошибка DB INIT: %s", e)


# user helpers
async def set_role(tg_id: int, role: Optional[str]):
    async with aiosqlite.connect(DB_PATH) as db:
        if role:
            await db.execute("""
                INSERT INTO users (tg_id, role)
                VALUES (?, ?)
                ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role;
            """, (tg_id, role))
        else:
            # remove role (set to NULL)
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


async def get_username(tg_id: int, app):
    if not tg_id:
        return "—"
    try:
        user = await app.bot.get_chat(tg_id)
        # prefer first+last name if available
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
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders(
                shop_tg_id, courier_tg_id,
                from_address, shop_contact,
                to_address, to_apt, client_name, client_phone,
                price, status, log, created_at, return_for, paid_to_courier, paid_at
            )
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, NULL, 0, NULL)
        """, (
            data["shop_tg_id"],
            data["from_address"],
            data["shop_contact"],
            data["to_address"],
            data.get("to_apt", ""),
            data.get("client_name", ""),
            data.get("client_phone", ""),
            data["price"],
            log_text,
            created_at,
        ))
        await db.commit()
        return cur.lastrowid


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


async def update_order(order_id: int, status: Optional[str] = None, courier: Optional[int] = None, log_add: Optional[str] = None, paid: Optional[bool] = None):
    """
    Update order fields.
    courier == 0 -> set courier NULL
    paid True -> set paid_to_courier=1 and paid_at
    """
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
            if paid:
                updates.append("paid_to_courier=1")
                updates.append("paid_at=?")
                params.append(timestamp)
            else:
                updates.append("paid_to_courier=0")
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

    report = (
        f"<b>ЗАКАЗ #{order_id}</b>\n\n"
        f"<b>Статус:</b> {o['status'].upper()}\n"
        f"<b>Цена:</b> {price_display} ₽\n"
        f"<b>Курьер:</b> {courier_name or '—'}\n\n"

        f"<b>ОТПРАВИТЕЛЬ</b>\n"
        f"Адрес: {clickable(o['from_address'])}\n"
        f"Контакт: {o['shop_contact']}\n\n"

        f"<b>ПОЛУЧАТЕЛЬ</b>\n"
        f"Адрес: {clickable(o['to_address'])}, кв/офис: {apt}\n"
        f"Имя: {o['client_name']}\n"
        f"Телефон: {o['client_phone']}\n"
    )
    if include_log:
        report += f"\n<b>ЛОГ:</b>\n<pre>{log_content}</pre>"
    return report


# send to couriers (mass push)
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


# create return order helper
async def create_return_order(original_order_id: int, price: float, courier_tg_id: int, app):
    orig = await get_order(original_order_id)
    if not orig:
        return None
    data = {
        "shop_tg_id": orig["shop_tg_id"],
        "from_address": orig["to_address"],       # клиент -> магазин
        "shop_contact": orig["shop_contact"],
        "to_address": orig["from_address"],
        "to_apt": orig.get("to_apt", ""),
        "client_name": orig.get("shop_contact", "Магазин"),
        "client_phone": orig.get("shop_contact", ""),
        "price": price,
    }
    new_oid = await save_order(data)
    # link to original
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET return_for=? WHERE id=?", (original_order_id, new_oid))
        await db.commit()
    # assign courier & set status taken
    await update_order(new_oid, status="taken", courier=courier_tg_id, log_add=f"Автоматически создан возврат от заказа #{original_order_id}")
    return new_oid


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


# ========== START + ROLE ==========
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id

    # If user has active taken orders as courier, don't allow role switching until finished
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT COUNT(*) FROM orders WHERE courier_tg_id=? AND status='taken'", (tg,))
        row = await cur.fetchone()
        await cur.close()
    active_taken = row[0] if row else 0

    # Always offer role selection unless user has active order as courier
    if active_taken:
        # keep current role but inform user they cannot switch
        role = await get_role(tg)
        await set_role_commands(ctx.application, tg, role)
        await update.message.reply_html(
            f"У вас есть активные доставки — сначала завершите их. Текущая роль: <b>{role or 'не выбрана'}</b>"
        )
        return

    # show selection always
    kb = [
        [InlineKeyboardButton("🏪 Магазин", callback_data="role_shop")],
        [InlineKeyboardButton("🛵 Курьер", callback_data="role_courier")],
    ]
    await update.message.reply_text("Кто вы?", reply_markup=InlineKeyboardMarkup(kb))


async def role_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = q.from_user.id
    if q.data == "role_shop":
        await set_role(tg, "shop")
        await set_role_commands(ctx.application, tg, "shop")
        await q.edit_message_text("Ты теперь Магазин. Меню обновлено 👍")
    else:
        await set_role(tg, "courier")
        await set_role_commands(ctx.application, tg, "courier")
        await q.edit_message_text("Ты теперь Курьер. Меню обновлено 👍")


# ========== WHOAMI / STATS ==========
async def whoami(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    role = await get_role(update.effective_user.id)
    await update.message.reply_html(f"Твоя роль: <b>{role or 'не выбрана'}</b>")


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    if not role:
        await update.message.reply_text("Роль не выбрана.")
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
    await update.message.reply_html(msg)


# ========== NEW / EDIT ORDER (FSM) ==========
async def new_order_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    if await get_role(tg) != "shop":
        await update.message.reply_html("Только магазин может создавать заказы")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data["shop_tg_id"] = tg
    ctx.user_data["order_id"] = None
    await update.message.reply_html("📝 <b>Шаг 1/4 — Адрес магазина</b>\nВведите адрес отправки:")
    return ADDRESS_FROM


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
    if order["status"] in ("delivered", "cancelled"):
        await q.edit_message_text("⛔ Этот заказ нельзя редактировать")
        return ConversationHandler.END
    ctx.user_data.clear()
    ctx.user_data.update(order)
    ctx.user_data["order_id"] = order_id
    try:
        await q.edit_message_reply_markup(reply_markup=None)
    except:
        pass
    await ctx.application.bot.send_message(
        tg,
        f"✍️ <b>Редактирование заказа #{order_id}</b>\n"
        f"Текущий адрес отправки: <b>{order['from_address']}</b>\n"
        "Введите новый адрес или повторите старый:",
        parse_mode="HTML"
    )
    return ADDRESS_FROM


async def step_from(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["from_address"] = update.message.text.strip()
    current_contact = ctx.user_data.get("shop_contact", "—")
    await update.message.reply_html(
        f"📝 <b>Шаг 2/4 — Контакт магазина</b>\n"
        f"Текущий контакт: <b>{current_contact}</b>\n"
        "Введите новый контакт или повторите старый:"
    )
    return CONTACT_SHOP


async def step_shop_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data["shop_contact"] = update.message.text.strip()
    d = ctx.user_data
    await update.message.reply_html(
        "📝 <b>Шаг 3/4 — Данные получателя</b>\n"
        f"Текущее значение: <b>{d.get('to_address', '')}, {d.get('to_apt','')}, "
        f"{d.get('client_name','')}, {d.get('client_phone','')}</b>\n\n"
        "Введите данные через запятую:\n"
        "<code>Адрес доставки, Квартира/Офис, Имя клиента, Телефон</code>"
    )
    return CONTACT_CLIENT


async def step_client(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    parts = [p.strip() for p in update.message.text.split(",")]
    if len(parts) < 4:
        await update.message.reply_html("⚠️ Нужно 4 значения через запятую")
        return CONTACT_CLIENT
    ctx.user_data["to_address"] = parts[0]
    ctx.user_data["to_apt"] = parts[1]
    ctx.user_data["client_name"] = parts[2]
    ctx.user_data["client_phone"] = parts[3]
    current_price = ctx.user_data.get("price", "—")
    await update.message.reply_html(
        f"📝 <b>Шаг 4/4 — Цена доставки</b>\n"
        f"Текущая цена: <b>{current_price}</b>\nВведите новую цену (только число):"
    )
    return DELIVERY_PRICE


async def step_price_final(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # validate price
    txt = update.message.text.strip().replace(",", ".")
    try:
        price = float(txt)
    except:
        await update.message.reply_html("⚠️ Введите корректное число")
        return DELIVERY_PRICE
    ctx.user_data["price"] = price
    order_id = ctx.user_data.get("order_id")
    courier_id = ctx.user_data.get("courier_tg_id")
    if order_id is None:
        # create
        oid = await save_order(ctx.user_data)
        await send_order_to_couriers(oid, ctx.application)
        await update.message.reply_html(
            f"✅ <b>Заказ #{oid} создан</b>\nРазослан всем курьерам."
        )
    else:
        # update existing
        oid = await update_order_details(order_id, ctx.user_data)
        await update.message.reply_html(f"✏️ <b>Заказ #{oid} обновлён</b>")
        new_order_data = await get_order(oid)
        courier_name = await get_username(courier_id, ctx.application)
        if courier_id:
            report = html_report(oid, new_order_data, courier_name, include_log=True)
            try:
                sent_msg = await ctx.application.bot.send_message(
                    courier_id,
                    f"❗ <b>ЗАКАЗ #{oid} ОБНОВЛЕН!</b>\n\n{report}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{oid}")],
                        [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{oid}")],
                        [InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{oid}")],
                        [InlineKeyboardButton("🚫 Клиент отсутствует", callback_data=f"client_not_home_{oid}")],
                        [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{oid}")],
                        [InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={new_order_data['shop_tg_id']}")]
                    ])
                )
                if getattr(sent_msg, "message_id", None):
                    existing = await get_courier_message_records(oid)
                    for cid, mid in existing:
                        try:
                            await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"❌ <b>Заказ #{oid} неактуален (обновлён)</b>")
                        except:
                            pass
                    await delete_courier_message_records(oid)
                    await save_courier_message_record(oid, courier_id, sent_msg.message_id)
            except Exception as e:
                logger.warning(f"Не удалось отправить обновление курьеру: {e}")
        else:
            await send_order_to_couriers(oid, ctx.application)
            await update.message.reply_html(f"✏️ <b>Заказ #{oid} обновлён</b>\nСтарые кнопки деактивированы. Новая версия разослана всем курьерам.")
    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_html("Операция отменена.")
    return ConversationHandler.END


# ========== MYORDERS (shop/courier view) ==========
async def myorders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    if not role:
        await update.message.reply_text("Вы не выбрали роль. Нажмите /start")
        return
    async with aiosqlite.connect(DB_PATH) as db:
        if role == "courier":
            query = "SELECT * FROM orders WHERE courier_tg_id=? ORDER BY created_at DESC"
            params = (tg,)
        else:
            query = "SELECT * FROM orders WHERE shop_tg_id=? ORDER BY created_at DESC"
            params = (tg,)
        cur = await db.execute(query, params)
        rows = await cur.fetchall()
    if not rows:
        await update.message.reply_html("<b>Нет заказов</b>")
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
            "shop_tg_id": shop_tg_id,
            "courier_tg_id": courier_tg_id,
            "from_address": from_address,
            "shop_contact": shop_contact,
            "to_address": to_address,
            "to_apt": to_apt,
            "client_name": client_name,
            "client_phone": client_phone,
            "price": price,
            "status": status,
            "log": log,
            "return_for": return_for,
            "paid_to_courier": paid_to_courier,
            "paid_at": paid_at
        }
        courier_name = await get_username(courier_tg_id, ctx.application)
        txt = html_report(order_id, o, courier_name, include_log=False)
        final_kb = []
        if role == "shop":
            # shop buttons
            kb = []
            if status in ("new", "taken"):
                kb.append(InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_{order_id}"))
            kb.append(InlineKeyboardButton("❌ Отменить", callback_data=f"shop_cancel_{order_id}"))
            if status == "taken" and courier_tg_id:
                kb.append(InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_tg_id}"))
            # finance button (mark paid/unpaid)
            if status in ("delivered", "taken", "failed_delivery") and not paid_to_courier:
                kb.append(InlineKeyboardButton("💸 Отметить как оплачено", callback_data=f"mark_paid_{order_id}"))
            if paid_to_courier:
                kb.append(InlineKeyboardButton("✅ Оплачено", callback_data=f"view_paid_{order_id}"))
            final_kb = [[b] for b in kb]
        else:
            # courier buttons
            if status == "taken" and shop_tg_id:
                final_kb.append([InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_tg_id}")])
            final_kb += [
                [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{order_id}")],
                [InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{order_id}")],
                [InlineKeyboardButton("🚫 Клиент отсутствует", callback_data=f"client_not_home_{order_id}")],
                [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")],
                [InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{order_id}")]
            ]
        if final_kb:
            await update.message.reply_html(txt, reply_markup=InlineKeyboardMarkup(final_kb))
        else:
            await update.message.reply_html(txt)


# ========== COURIER ACTIONS ==========
async def take_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    courier_id = q.from_user.id
    order_id = int(q.data.split("_")[1])
    o = await get_order(order_id)
    if not o or o["status"] != "new":
        o_status = o["status"] if o else "удален"
        try:
            await q.edit_message_text(
                f"⛔ <b>Заказ #{order_id} недоступен.</b>\n"
                f"Статус: {o_status.upper()}. Сообщение деактивировано.",
                parse_mode="HTML",
                reply_markup=None
            )
        except Exception as e:
            logger.warning(f"Ошибка при деактивации старого сообщения: {e}")
        return
    courier_name = await get_username(courier_id, ctx.application)
    await update_order(
        order_id,
        status="taken",
        courier=courier_id,
        log_add=f"Курьер {courier_name} взял заказ"
    )
    o = await get_order(order_id)
    txt = html_report(order_id, o, courier_name)
    kb = [
        [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{order_id}")],
        [InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{order_id}")],
        [InlineKeyboardButton("🚫 Клиент отсутствует", callback_data=f"client_not_home_{order_id}")],
        [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")],
        [InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{order_id}")]
    ]
    if o.get("shop_tg_id"):
        kb.append([InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={o['shop_tg_id']}")])
    try:
        sent_msg = await ctx.application.bot.send_message(
            courier_id,
            txt,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(kb)
        )
        if getattr(sent_msg, "message_id", None):
            await save_courier_message_record(order_id, courier_id, sent_msg.message_id)
    except Exception as e:
        logger.warning(f"Не удалось отправить сообщение курьеру: {e}")
    try:
        await q.edit_message_text("Вы взяли заказ", reply_markup=None)
    except:
        pass
    # deactivate other mails
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
    except Exception as e:
        logger.warning(f"Ошибка при деактивации старых рассылок: {e}")
    # notify shop
    try:
        shop_kb = None
        if courier_id:
            shop_kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_id}")]])
        await ctx.application.bot.send_message(
            o["shop_tg_id"],
            f"🚀 Курьер {courier_name} взял заказ #{order_id}",
            parse_mode="HTML",
            reply_markup=shop_kb
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить магазин о взятии заказа: {e}")


async def arrived(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    # expected arrived_shop_{order} or arrived_client_{order}
    if len(parts) < 3:
        return
    where = parts[1]
    order_id = int(parts[2])
    courier_id = q.from_user.id
    o = await get_order(order_id)
    if not o or o["courier_tg_id"] != courier_id or o["status"] != 'taken':
        await q.answer("⛔ Заказ неактивен или отменен магазином.")
        if o and o["status"] == 'cancelled':
            await q.edit_message_text(f"❌ Заказ #{order_id} был отменен магазином.", reply_markup=None)
        return
    courier_name = await get_username(courier_id, ctx.application)
    shop_id = o["shop_tg_id"]
    if where == "shop":
        msg = f"📍 Курьер <b>{courier_name}</b> прибыл в магазин (заказ #{order_id})"
        log_msg = "Курьер прибыл в магазин"
    else:
        msg = f"📍 Курьер <b>{courier_name}</b> прибыл к клиенту (заказ #{order_id})"
        log_msg = "Курьер прибыл к клиенту"
    await update_order(order_id, status=o["status"], log_add=log_msg)
    try:
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
    courier_name = await get_username(courier_id, ctx.application)
    # deactivate old messages
    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            try:
                await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"❌ <b>Заказ #{order_id} неактуален (пересоздаётся)</b>")
            except:
                pass
        await delete_courier_message_records(order_id)
    except Exception as e:
        logger.warning(f"Ошибка при удалении старых сообщений: {e}")
    await update_order(order_id, status="new", courier=0, log_add=f"Курьер {courier_name} отменил заказ")
    try:
        await ctx.application.bot.send_message(o["shop_tg_id"], f"❌ Курьер {courier_name} отменил заказ #{order_id}", parse_mode="HTML")
    except:
        pass
    await send_order_to_couriers(order_id, ctx.application)
    try:
        await q.edit_message_text(f"Заказ #{order_id} отменён и снова доступен курьерам", reply_markup=None)
    except:
        pass


async def finish_order(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    order_id = int(q.data.split("_")[1])
    courier_id = q.from_user.id
    o = await get_order(order_id)
    if not o or o["courier_tg_id"] != courier_id or o["status"] != 'taken':
        await q.answer("⛔ Заказ неактивен или отменен магазином.")
        return
    courier_name = await get_username(courier_id, ctx.application)
    shop_id = o["shop_tg_id"]
    # update to delivered and add log
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
    except Exception as e:
        logger.warning(f"Не удалось отправить отчет магазину: {e}")
    # deactivate other courier messages but keep courier's message for record
    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            if cid == courier_id:
                continue
            try:
                await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"ℹ️ <b>Заказ #{order_id} завершён</b>")
            except:
                pass
            try:
                await delete_specific_courier_message_record(order_id, cid, mid)
            except:
                pass
    except Exception as e:
        logger.warning(f"Ошибка при очистке сообщений после завершения: {e}")


# ========== SHOP CANCEL ==========
async def shop_cancel_from_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    try:
        order_id = int(q.data.split("_")[2])
    except:
        await q.edit_message_text("Неверные данные")
        return
    shop_id = q.from_user.id
    o = await get_order(order_id)
    if not o or o["shop_tg_id"] != shop_id:
        await q.answer("⛔ Нельзя отменить")
        return
    courier_id = o["courier_tg_id"]
    courier_name = await get_username(courier_id, ctx.application)
    await update_order(order_id, status="cancelled", log_add="Магазин отменил заказ")
    await q.edit_message_text(f"❌ Заказ #{order_id} отменён", reply_markup=None)
    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            try:
                await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"❌ <b>Заказ #{order_id} отменён магазином</b>")
            except:
                pass
        await delete_courier_message_records(order_id)
    except Exception as e:
        logger.warning(f"Ошибка при деактивации сообщений при отмене магазином: {e}")
    if courier_id:
        try:
            report = html_report(order_id, o, courier_name)
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_id}")]])
            await ctx.application.bot.send_message(courier_id, f"❌ <b>Заказ #{order_id} отменён магазином</b>\n\n{report}", parse_mode="HTML", reply_markup=kb)
        except:
            pass


# ========== HISTORY (role-based view) ==========
# support callbacks for switching history mode
async def history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    if not role:
        await update.message.reply_text("Вы не выбрали роль. Нажмите /start")
        return
    # default show current role history
    await send_history_for_role(tg, role, ctx)


async def send_history_for_role(tg: int, role: str, ctx: ContextTypes.DEFAULT_TYPE):
    # role is "courier" or "shop"
    if role == "courier":
        # show courier deliveries (delivered and failed deliveries etc.)
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT id, price, status, return_for, paid_to_courier FROM orders
                WHERE courier_tg_id=? ORDER BY created_at DESC
            """, (tg,))
            rows = await cur.fetchall()
        if not rows:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 История как магазин", callback_data="hist_switch_shop")]])
            await ctx.application.bot.send_message(tg, "📘 <b>История как курьер</b>\n\nНет записей.", parse_mode="HTML", reply_markup=kb)
            return
        total_earned = 0.0
        lines = ["📘 <b>История как курьер</b>\n"]
        for r in rows:
            oid, price, status, return_for, paid_to_courier = r
            # calculate forward/return split by checking if this is return (return_for not null) or forward
            if return_for:
                # this is return order
                lines.append(f"#{oid} — Обратка: {int(price)} ₽ | Статус: {status}")
                total_earned += float(price or 0)
            else:
                lines.append(f"#{oid} — Прямая: {int(price)} ₽ | Статус: {status}")
                total_earned += float(price or 0)
        footer = f"\n\n――――――――――\n💰 <b>Итого заработал курьер: {int(total_earned)} ₽</b>"
        text = "\n".join(lines) + footer
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("📦 История как магазин", callback_data="hist_switch_shop")]])
        await ctx.application.bot.send_message(tg, text, parse_mode="HTML", reply_markup=kb)
    else:
        # shop history: show orders created by shop
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("""
                SELECT id, price, status, return_for, paid_to_courier FROM orders
                WHERE shop_tg_id=? ORDER BY created_at DESC
            """, (tg,))
            rows = await cur.fetchall()
        if not rows:
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛵 История как курьер", callback_data="hist_switch_courier")]])
            await ctx.application.bot.send_message(tg, "📗 <b>История как магазин</b>\n\nНет записей.", parse_mode="HTML", reply_markup=kb)
            return
        total_spent = 0.0
        lines = ["📗 <b>История как магазин</b>\n"]
        for r in rows:
            oid, price, status, return_for, paid_to_courier = r
            # for shop show what paid_to_courier is
            pay_status = "Оплачено" if paid_to_courier else "Не оплачено"
            lines.append(f"#{oid} — Сумма курьеру: {int(price)} ₽ | Статус: {status} | {pay_status}")
            if paid_to_courier:
                total_spent += float(price or 0)
            else:
                total_spent += 0  # only count paid if you want; but as requested show expenses — we'll show both below
        # additionally compute totals separately
        async with aiosqlite.connect(DB_PATH) as db:
            cur2 = await db.execute("SELECT SUM(price) FROM orders WHERE shop_tg_id=? AND paid_to_courier=1", (tg,))
            row = await cur2.fetchone()
        paid_sum = int(row[0] or 0) if row and row[0] else 0
        async with aiosqlite.connect(DB_PATH) as db:
            cur3 = await db.execute("SELECT SUM(price) FROM orders WHERE shop_tg_id=? AND paid_to_courier=0", (tg,))
            row2 = await cur3.fetchone()
        unpaid_sum = int(row2[0] or 0) if row2 and row2[0] else 0
        footer = f"\n\n――――――――――\n💰 <b>Оплачено курьерам: {paid_sum} ₽</b>\n💸 <b>Не оплачено: {unpaid_sum} ₽</b>"
        text = "\n".join(lines) + footer
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛵 История как курьер", callback_data="hist_switch_courier")]])
        await ctx.application.bot.send_message(tg, text, parse_mode="HTML", reply_markup=kb)


async def history_switch_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.data == "hist_switch_shop":
        await send_history_for_role(q.from_user.id, "shop", ctx)
    else:
        await send_history_for_role(q.from_user.id, "courier", ctx)


# ========== RETURN / NEGOTIATION FLOW ==========
# courier presses client_not_home_{order_id}
async def client_not_home_entry(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    courier_id = q.from_user.id
    parts = q.data.split("_")
    # handle patterns: client_not_home_{order_id}
    try:
        order_id = int(parts[2])
    except:
        await q.edit_message_text("Неверные данные")
        return ConversationHandler.END
    o = await get_order(order_id)
    if not o or o["courier_tg_id"] != courier_id or o["status"] != "taken":
        await q.edit_message_text("⛔ Этот заказ не ваш или не активен.")
        return ConversationHandler.END
    # store in user_data
    ctx.user_data["return_flow"] = {"order_id": order_id, "stage": "awaiting_courier_price", "courier_id": courier_id}
    await q.edit_message_text(f"Введите предлагаемую цену возврата (только число в рублях) для заказа #{order_id}:")
    return RETURN_COURIER_PRICE


async def courier_price_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    if role != "courier" or "return_flow" not in ctx.user_data:
        await update.message.reply_html("Нет активного запроса на возврат.")
        return ConversationHandler.END
    rf = ctx.user_data["return_flow"]
    if rf.get("stage") != "awaiting_courier_price":
        await update.message.reply_html("Нет активного запроса на возврат.")
        ctx.user_data.pop("return_flow", None)
        return ConversationHandler.END
    txt = update.message.text.strip().replace(",", ".")
    try:
        price = float(txt)
    except:
        await update.message.reply_html("⚠️ Введите корректное число цены в рублях.")
        return RETURN_COURIER_PRICE
    order_id = rf["order_id"]
    courier_id = rf["courier_id"]
    rf["proposed_price"] = price
    rf["stage"] = "proposed_to_shop"
    # notify shop with accept / set own price / cancel
    o = await get_order(order_id)
    shop_id = o["shop_tg_id"]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✔️ Принять {int(price)} ₽", callback_data=f"accept_return_{order_id}_{int(price)}")],
        [InlineKeyboardButton("✏️ Назначить свою цену", callback_data=f"set_return_price_{order_id}_{int(price)}")],
        [InlineKeyboardButton("❌ Отменить заказ", callback_data=f"shop_cancel_{order_id}")]
    ])
    try:
        await ctx.application.bot.send_message(
            shop_id,
            f"Курьер предлагает возврат заказа #{order_id} за <b>{int(price)} ₽</b>.\nВыберите действие:",
            parse_mode="HTML",
            reply_markup=kb
        )
    except Exception as e:
        logger.warning(f"Не удалось отправить предложение магазину: {e}")
    await update.message.reply_html("✅ Предложение цены отправлено магазину. Ждите ответа.")
    return ConversationHandler.END


# shop accepts proposed price
async def shop_accept_return_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    # accept_return_{order}_{price}
    try:
        _, _, order_id_str, price_str = parts
        order_id = int(order_id_str)
        price = float(price_str)
    except:
        await q.edit_message_text("Неверные данные в запросе.")
        return
    orig = await get_order(order_id)
    if not orig:
        await q.edit_message_text("Заказ не найден.")
        return
    courier_id = orig.get("courier_tg_id")
    if not courier_id:
        await q.edit_message_text("Курьер не найден.")
        return
    # mark original order as failed_delivery (but courier still earns forward)
    await update_order(order_id, status="failed_delivery", log_add=f"Магазин принял возврат за {int(price)} ₽")
    # create return order and assign courier
    new_oid = await create_return_order(order_id, price, courier_id, ctx.application)
    if not new_oid:
        await q.edit_message_text("Ошибка при создании возвратного заказа.")
        return
    try:
        await q.edit_message_text(f"Возврат создан как заказ #{new_oid}. Назначен курьеру {await get_username(courier_id, ctx.application)}")
    except:
        pass
    try:
        courier_name = await get_username(courier_id, ctx.application)
        o_new = await get_order(new_oid)
        report = html_report(new_oid, o_new, courier_name, include_log=True)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 У клиента (начало возврата)", callback_data=f"arrived_client_{new_oid}")],
            [InlineKeyboardButton("📍 У магазина (возврат завершён)", callback_data=f"arrived_shop_{new_oid}")],
            [InlineKeyboardButton("❌ Отменить возврат", callback_data=f"cancel_{new_oid}")],
            [InlineKeyboardButton("✅ Завершить возврат", callback_data=f"finish_{new_oid}")]
        ])
        await ctx.application.bot.send_message(courier_id, f"↩️ <b>Создан возврат #{new_oid}</b>\n\n{report}", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.warning(f"Не удалось уведомить курьера о возврате: {e}")


# shop sets own price -> we start FSM for shop
async def shop_set_price_entry_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    try:
        _, _, order_id_str, proposed_str = parts
        order_id = int(order_id_str)
    except:
        await q.edit_message_text("Неверные данные.")
        return ConversationHandler.END
    # keep state for shop
    ctx.user_data["return_flow_shop"] = {"order_id": order_id, "stage": "awaiting_shop_price", "proposed_price": float(proposed_str)}
    await q.edit_message_text(f"Введите вашу цену возврата для заказа #{order_id} (только число):")
    return RETURN_SHOP_PRICE


async def shop_price_received(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    if role != "shop" or "return_flow_shop" not in ctx.user_data:
        await update.message.reply_html("Нет активного запроса на ввод цены.")
        return ConversationHandler.END
    rf = ctx.user_data["return_flow_shop"]
    if rf.get("stage") != "awaiting_shop_price":
        await update.message.reply_html("Нет активного запроса.")
        ctx.user_data.pop("return_flow_shop", None)
        return ConversationHandler.END
    txt = update.message.text.strip().replace(",", ".")
    try:
        price = float(txt)
    except:
        await update.message.reply_html("⚠️ Введите корректное число цены в рублях.")
        return RETURN_SHOP_PRICE
    order_id = rf["order_id"]
    orig = await get_order(order_id)
    if not orig:
        await update.message.reply_html("Заказ не найден.")
        ctx.user_data.pop("return_flow_shop", None)
        return ConversationHandler.END
    courier_id = orig.get("courier_tg_id")
    if not courier_id:
        await update.message.reply_html("Курьер не найден, отмена.")
        ctx.user_data.pop("return_flow_shop", None)
        return ConversationHandler.END
    # send to courier for acceptance
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✔️ Принять {int(price)} ₽", callback_data=f"accept_return_by_courier_{order_id}_{int(price)}")],
        [InlineKeyboardButton("❌ Отказать", callback_data=f"decline_return_by_courier_{order_id}_{int(price)}")]
    ])
    try:
        await ctx.application.bot.send_message(courier_id, f"Магазин назначил цену возврата для заказа #{order_id}: <b>{int(price)} ₽</b>. Примите или откажитесь.", parse_mode="HTML", reply_markup=kb)
    except Exception as e:
        logger.warning(f"Не удалось отправить предложение курьеру: {e}")
    await update.message.reply_html("✅ Цена отправлена курьеру на подтверждение.")
    ctx.user_data.pop("return_flow_shop", None)
    return ConversationHandler.END


# courier accepts shop price
async def courier_accept_shop_price_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    # accept_return_by_courier_{order}_{price}
    try:
        # parts likely: ['accept','return','by','courier','{order}','{price}'] or combined pattern
        # but we used f"accept_return_by_courier_{order}_{int(price)}"
        # so splitting by '_' => ['accept','return','by','courier','{order}','{price}']
        order_id = int(parts[4])
        price = float(parts[5])
    except Exception:
        # fallback pattern used earlier maybe different
        try:
            _, _, _, order_id_str, price_str = parts
            order_id = int(order_id_str)
            price = float(price_str)
        except:
            await q.edit_message_text("Неверные данные.")
            return
    orig = await get_order(order_id)
    if not orig:
        await q.edit_message_text("Заказ не найден.")
        return
    courier_id = q.from_user.id
    await update_order(order_id, status="failed_delivery", log_add=f"Магазин и курьер согласовали возврат за {int(price)} ₽")
    new_oid = await create_return_order(order_id, price, courier_id, ctx.application)
    if not new_oid:
        await q.edit_message_text("Ошибка при создании возвратного заказа.")
        return
    try:
        await q.edit_message_text(f"Вы согласовали возврат. Создан возвратный заказ #{new_oid}.")
    except:
        pass
    try:
        shop_id = orig["shop_tg_id"]
        await ctx.application.bot.send_message(shop_id, f"↩️ Создан возвратный заказ #{new_oid}. Курьер: {await get_username(courier_id, ctx.application)}")
    except:
        pass
    try:
        courier_name = await get_username(courier_id, ctx.application)
        o_new = await get_order(new_oid)
        report = html_report(new_oid, o_new, courier_name, include_log=True)
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📍 У клиента (начало возврата)", callback_data=f"arrived_client_{new_oid}")],
            [InlineKeyboardButton("📍 У магазина (возврат завершён)", callback_data=f"arrived_shop_{new_oid}")],
            [InlineKeyboardButton("❌ Отменить возврат", callback_data=f"cancel_{new_oid}")],
            [InlineKeyboardButton("✅ Завершить возврат", callback_data=f"finish_{new_oid}")]
        ])
        await ctx.application.bot.send_message(courier_id, f"↩️ <b>Создан возврат #{new_oid}</b>\n\n{report}", parse_mode="HTML", reply_markup=kb)
    except:
        pass


async def courier_decline_shop_price_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    # decline_return_by_courier_{order}_{price}
    try:
        order_id = int(parts[4])
        price = float(parts[5])
    except:
        await q.edit_message_text("Неверные данные.")
        return
    orig = await get_order(order_id)
    if not orig:
        await q.edit_message_text("Заказ не найден.")
        return
    courier_id = q.from_user.id
    shop_id = orig["shop_tg_id"]
    try:
        await ctx.application.bot.send_message(shop_id, f"⚠️ Курьер {await get_username(courier_id, ctx.application)} отклонил предложенную цену {int(price)} ₽ для возврата заказа #{order_id}.")
        await q.edit_message_text("Вы отклонили цену магазина. Магазин уведомлён.")
    except:
        pass


# ========== FINANCE (shop) & PAYOUTS (courier) ==========
async def finance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    if role != "shop":
        await update.message.reply_html("Команда доступна только магазину.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💸 Неоплаченные доставки", callback_data="finance_unpaid")],
        [InlineKeyboardButton("💵 Оплаченные доставки", callback_data="finance_paid")],
        [InlineKeyboardButton("📊 Сводка", callback_data="finance_summary")]
    ])
    await update.message.reply_html("Финансы магазина:", reply_markup=kb)


async def finance_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = q.from_user.id
    parts = q.data.split("_")
    action = parts[1] if len(parts) > 1 else None
    if action == "unpaid":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, price, status FROM orders WHERE shop_tg_id=? AND paid_to_courier=0 ORDER BY created_at DESC", (tg,))
            rows = await cur.fetchall()
        if not rows:
            await q.edit_message_text("Нет неоплаченных доставок.")
            return
        total = sum([r[1] or 0 for r in rows])
        text_lines = ["💸 <b>Неоплаченные доставки</b>\n"]
        for r in rows:
            oid, price, status = r
            text_lines.append(f"#{oid} — {int(price)} ₽ | Статус: {status} | [оплатить — нажми кнопку]")
        text_lines.append(f"\nИтого к оплате: {int(total)} ₽")
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("Оплатить всё", callback_data="finance_pay_all")]])
        await q.edit_message_text("\n".join(text_lines), parse_mode="HTML", reply_markup=kb)
    elif action == "paid":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, price, paid_at FROM orders WHERE shop_tg_id=? AND paid_to_courier=1 ORDER BY paid_at DESC", (tg,))
            rows = await cur.fetchall()
        if not rows:
            await q.edit_message_text("Нет оплаченных доставок.")
            return
        total = sum([r[1] or 0 for r in rows])
        text_lines = ["💵 <b>Оплаченные доставки</b>\n"]
        for r in rows:
            oid, price, paid_at = r
            text_lines.append(f"#{oid} — {int(price)} ₽ | Оплачено: {paid_at or '—'}")
        text_lines.append(f"\nИтого оплачено: {int(total)} ₽")
        await q.edit_message_text("\n".join(text_lines), parse_mode="HTML")
    elif action == "summary":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT COUNT(*), SUM(CASE WHEN paid_to_courier=1 THEN 1 ELSE 0 END) FROM orders WHERE shop_tg_id=?", (tg,))
            # We'll compute sums separately
            cur2 = await db.execute("SELECT SUM(price) FROM orders WHERE shop_tg_id=? AND paid_to_courier=1", (tg,))
            cur3 = await db.execute("SELECT SUM(price) FROM orders WHERE shop_tg_id=? AND paid_to_courier=0", (tg,))
            row1 = await cur.fetchone()
            row2 = await cur2.fetchone()
            row3 = await cur3.fetchone()
        paid_sum = int(row2[0] or 0) if row2 and row2[0] else 0
        unpaid_sum = int(row3[0] or 0) if row3 and row3[0] else 0
        text = f"📊 <b>Сводка по оплатам</b>\n\nОплачено: {paid_sum} ₽\nНе оплачено: {unpaid_sum} ₽"
        await q.edit_message_text(text, parse_mode="HTML")
    else:
        await q.edit_message_text("Неверное действие.")


# mark single order paid (shop triggers)
async def mark_paid_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split("_")
    if len(parts) < 3:
        await q.edit_message_text("Неверные данные")
        return
    try:
        order_id = int(parts[2])
    except:
        await q.edit_message_text("Неверные данные")
        return
    # check shop ownership
    shop_id = q.from_user.id
    o = await get_order(order_id)
    if not o or o["shop_tg_id"] != shop_id:
        await q.edit_message_text("Заказ не найден или не принадлежит вам.")
        return
    # mark paid
    await update_order(order_id, paid=True, log_add="Магазин отметил как оплачено")
    # add into payments table
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT INTO payments (order_id, shop_tg_id, courier_tg_id, amount, paid_at) VALUES (?, ?, ?, ?, ?)",
                         (order_id, o["shop_tg_id"], o["courier_tg_id"], o["price"], time.strftime("%Y-%m-%d %H:%M:%S")))
        await db.commit()
    try:
        await q.edit_message_text(f"Заказ #{order_id} отмечен как ОПЛАЧЕН.")
    except:
        pass
    # notify courier
    if o["courier_tg_id"]:
        try:
            await ctx.application.bot.send_message(o["courier_tg_id"], f"💵 Магазин отметил заказ #{order_id} как оплаченный. Сумма: {int(o['price'])} ₽")
        except:
            pass


# pay all callback
async def finance_pay_all_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    shop_id = q.from_user.id
    # find all unpaid orders for this shop
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT id, courier_tg_id, price FROM orders WHERE shop_tg_id=? AND paid_to_courier=0", (shop_id,))
        rows = await cur.fetchall()
    if not rows:
        await q.edit_message_text("Нет неоплаченных заказов.")
        return
    total = 0
    for r in rows:
        oid, courier_id, price = r
        total += float(price or 0)
        await update_order(oid, paid=True, log_add="Магазин оплатил (массово)")
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO payments (order_id, shop_tg_id, courier_tg_id, amount, paid_at) VALUES (?, ?, ?, ?, ?)",
                             (oid, shop_id, courier_id, price, time.strftime("%Y-%m-%d %H:%M:%S")))
            await db.commit()
        # notify courier if exists
        if courier_id:
            try:
                await ctx.application.bot.send_message(courier_id, f"💵 Магазин оплатил заказ #{oid}. Сумма: {int(price)} ₽")
            except:
                pass
    await q.edit_message_text(f"Оплачено {int(total)} ₽ по всем неоплаченным заказам.")


# ========== PAYOUTS (courier) ==========
async def payouts(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    if role != "courier":
        await update.message.reply_html("Команда доступна только курьеру.")
        return
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("💰 Полученные выплаты", callback_data="payouts_received")],
        [InlineKeyboardButton("💸 Должны выплатить", callback_data="payouts_unpaid")],
        [InlineKeyboardButton("📊 Сводка", callback_data="payouts_summary")]
    ])
    await update.message.reply_html("Выплаты:", reply_markup=kb)


async def payouts_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = q.from_user.id
    parts = q.data.split("_")
    action = parts[1] if len(parts) > 1 else None
    if action == "received":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, price, paid_at FROM orders WHERE courier_tg_id=? AND paid_to_courier=1 ORDER BY paid_at DESC", (tg,))
            rows = await cur.fetchall()
        if not rows:
            await q.edit_message_text("Нет полученных выплат.")
            return
        total = sum([r[1] or 0 for r in rows])
        text_lines = ["💰 <b>Полученные выплаты</b>\n"]
        for r in rows:
            oid, price, paid_at = r
            text_lines.append(f"#{oid} — {int(price)} ₽ | Оплачено: {paid_at or '—'}")
        text_lines.append(f"\nИтого получено: {int(total)} ₽")
        await q.edit_message_text("\n".join(text_lines), parse_mode="HTML")
    elif action == "unpaid":
        async with aiosqlite.connect(DB_PATH) as db:
            cur = await db.execute("SELECT id, price, status FROM orders WHERE courier_tg_id=? AND paid_to_courier=0 ORDER BY created_at DESC", (tg,))
            rows = await cur.fetchall()
        if not rows:
            await q.edit_message_text("Нет неоплаченных доставок.")
            return
        total = sum([r[1] or 0 for r in rows])
        text_lines = ["💸 <b>Неоплаченные (должны выплатить)</b>\n"]
        for r in rows:
            oid, price, status = r
            text_lines.append(f"#{oid} — {int(price)} ₽ | Статус: {status}")
        text_lines.append(f"\nИтого должны: {int(total)} ₽")
        await q.edit_message_text("\n".join(text_lines), parse_mode="HTML")
    elif action == "summary":
        async with aiosqlite.connect(DB_PATH) as db:
            cur1 = await db.execute("SELECT SUM(price) FROM orders WHERE courier_tg_id=?", (tg,))
            row1 = await cur1.fetchone()
            cur2 = await db.execute("SELECT SUM(price) FROM orders WHERE courier_tg_id=? AND paid_to_courier=1", (tg,))
            row2 = await cur2.fetchone()
        total_all = int(row1[0] or 0) if row1 and row1[0] else 0
        total_paid = int(row2[0] or 0) if row2 and row2[0] else 0
        total_unpaid = total_all - total_paid
        text = f"📊 <b>Ваша финансовая сводка</b>\n\nЗаработано всего: {total_all} ₽\nПолучено: {total_paid} ₽\nДолжны выплатить: {total_unpaid} ₽"
        await q.edit_message_text(text, parse_mode="HTML")
    else:
        await q.edit_message_text("Неверное действие.")


# ========== ERROR HANDLER ==========
async def error_handler(update, ctx):
    logger.error("Ошибка:", exc_info=ctx.error)


# ========== MAIN: register handlers ==========
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = init_db
    app.add_error_handler(error_handler)

    # conv for creating/editing orders
    conv = ConversationHandler(
        entry_points=[
            CommandHandler("new_order", new_order_start),
            CallbackQueryHandler(edit_order_start, pattern="^edit_"),
        ],
        states={
            ADDRESS_FROM: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_from)],
            CONTACT_SHOP: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_shop_contact)],
            CONTACT_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_client)],
            DELIVERY_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, step_price_final)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=False
    )
    app.add_handler(conv)

    # conv for return negotiation
    conv_return = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(client_not_home_entry, pattern="^client_not_home_"),
            CallbackQueryHandler(shop_set_price_entry_cb, pattern="^set_return_price_"),
        ],
        states={
            RETURN_COURIER_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, courier_price_received)],
            RETURN_SHOP_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, shop_price_received)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_user=True,
        per_chat=False
    )
    app.add_handler(conv_return)

    # callbacks and commands
    app.add_handler(CallbackQueryHandler(role_choice, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(take_order, pattern="^take_"))
    app.add_handler(CallbackQueryHandler(finish_order, pattern="^finish_"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_"))
    app.add_handler(CallbackQueryHandler(arrived, pattern="^arrived_"))
    app.add_handler(CallbackQueryHandler(shop_cancel_from_button, pattern="^shop_cancel_"))
    app.add_handler(CallbackQueryHandler(history_switch_cb, pattern="^hist_switch_"))
    app.add_handler(CallbackQueryHandler(client_not_home_entry, pattern="^client_not_home_"))
    app.add_handler(CallbackQueryHandler(shop_accept_return_cb, pattern="^accept_return_"))
    app.add_handler(CallbackQueryHandler(shop_set_price_entry_cb, pattern="^set_return_price_"))
    app.add_handler(CallbackQueryHandler(courier_accept_shop_price_cb, pattern="^accept_return_by_courier_"))
    app.add_handler(CallbackQueryHandler(courier_decline_shop_price_cb, pattern="^decline_return_by_courier_"))
    app.add_handler(CallbackQueryHandler(courier_accept_shop_price_cb, pattern="^accept_return_by_courier_"))
    app.add_handler(CallbackQueryHandler(mark_paid_cb, pattern="^mark_paid_"))
    app.add_handler(CallbackQueryHandler(finance_pay_all_cb, pattern="^finance_pay_all$"))
    app.add_handler(CallbackQueryHandler(finance_cb, pattern="^finance_"))
    app.add_handler(CallbackQueryHandler(payouts_cb, pattern="^payouts_"))

    # order-specific callback patterns (edit/take/... are added above)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("myorders", myorders))
    app.add_handler(CommandHandler("history", history))
    app.add_handler(CommandHandler("finance", finance))
    app.add_handler(CommandHandler("payouts", payouts))

    logger.info("BOT STARTED")
    app.run_polling()


if __name__ == "__main__":
    main()
