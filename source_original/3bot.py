import time
import aiosqlite
import logging
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
from telegram.error import TelegramError
import urllib.parse

# --- КОНСТАНТЫ И НАСТРОЙКИ ---
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("delivery")

BOT_TOKEN = "8555882487:AAFyl9juLHiZ33FIjcretFe0U2yIDau1pYs"
DB_PATH = "db.sqlite3"

(
    ADDRESS_FROM,
    CONTACT_SHOP,
    CONTACT_CLIENT,
    DELIVERY_PRICE,
) = range(4)


# ==========================================================
# DATABASE INIT
# ==========================================================
async def init_db(app):
    logger.info("Инициализация базы данных...")
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tg_id INTEGER UNIQUE,
                    role TEXT
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
                    created_at TEXT
                );
            """)
            # Новая таблица для хранения id сообщений, которые бот отправил курьерам
            await db.execute("""
                CREATE TABLE IF NOT EXISTS courier_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    order_id INTEGER,
                    courier_tg_id INTEGER,
                    message_id INTEGER,
                    created_at TEXT
                );
            """)
            await db.commit()
            logger.info("База данных готова")
    except Exception as e:
        logger.error(f"Ошибка DB INIT: {e}")


# ==========================================================
# USER HELPERS
# ==========================================================
async def set_role(tg_id, role):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (tg_id, role)
            VALUES (?, ?)
            ON CONFLICT(tg_id) DO UPDATE SET role=excluded.role;
        """, (tg_id, role))
        await db.commit()

async def get_role(tg_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT role FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        await cur.close()
        return row[0] if row else None

async def get_username(tg_id, app):
    if not tg_id:
        return "—"
    try:
        user = await app.bot.get_chat(tg_id)
        # Используем доступные поля
        if getattr(user, "full_name", None):
            return user.full_name
        if getattr(user, "username", None):
            return f"@{user.username}"
        return f"ID {tg_id}"
    except Exception:
        return f"ID {tg_id}"


# ==========================================================
# ORDER HELPERS
# ==========================================================
async def save_order(data):
    created_at = str(int(time.time()))
    log_text = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Заказ создан."

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders(
                shop_tg_id, courier_tg_id,
                from_address, shop_contact,
                to_address, to_apt, client_name, client_phone,
                price, status, log, created_at
            )
            VALUES (?, NULL, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?)
        """, (
            data["shop_tg_id"],
            data["from_address"],
            data["shop_contact"],
            data["to_address"],
            data["to_apt"],
            data["client_name"],
            data["client_phone"],
            data["price"],
            log_text,
            created_at,
        ))
        await db.commit()
        return cur.lastrowid


async def update_order_details(order_id, data):
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
            data["to_apt"],
            data["client_name"],
            data["client_phone"],
            data["price"],
            log_entry,
            order_id
        ))
        await db.commit()
        return order_id


async def get_order(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT shop_tg_id, courier_tg_id, from_address, shop_contact,
                   to_address, to_apt, client_name, client_phone, price,
                   status, log, created_at
            FROM orders WHERE id=?
        """, (order_id,))
        row = await cur.fetchone()
        await cur.close()

    if not row:
        return None

    return {
        "shop_tg_id": row[0],
        "courier_tg_id": row[1],
        "from_address": row[2],
        "shop_contact": row[3],
        "to_address": row[4],
        "to_apt": row[5],
        "client_name": row[6],
        "client_phone": row[7],
        "price": row[8],
        "status": row[9],
        "log": row[10],
        "created_at": row[11],
    }


async def update_order(order_id, status, courier=None, log_add=None):
    """
    Обновляет статус и опционально привязку courier_tg_id.
    special behavior: if courier == 0 -> sets courier_tg_id = NULL in DB
    if courier is None -> leaves courier_tg_id untouched
    otherwise sets courier_tg_id = courier
    """
    timestamp = time.strftime("%Y-%m-%d %H:%M:%S")

    async with aiosqlite.connect(DB_PATH) as db:
        if log_add:
            await db.execute(
                "UPDATE orders SET log = log || ? WHERE id=?",
                (f"[{timestamp}] {log_add}\n", order_id)
            )

        updates = ["status=?"]
        params = [status]

        # Изменяем courier_tg_id, если явно передали значение
        if courier is not None:
            # special: courier == 0 -> set NULL
            if courier == 0:
                updates.append("courier_tg_id=NULL")
            else:
                updates.append("courier_tg_id=?")
                params.append(courier)

        params.append(order_id)
        await db.execute(
            f"UPDATE orders SET {', '.join(updates)} WHERE id=?",
            tuple(params)
        )
        await db.commit()


# ==========================================================
# COURIER MESSAGES STORE HELPERS
# ==========================================================
async def save_courier_message_record(order_id, courier_tg_id, message_id):
    created_at = str(int(time.time()))
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO courier_messages (order_id, courier_tg_id, message_id, created_at)
            VALUES (?, ?, ?, ?)
        """, (order_id, courier_tg_id, message_id, created_at))
        await db.commit()

async def get_courier_message_records(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            SELECT courier_tg_id, message_id FROM courier_messages
            WHERE order_id=?
        """, (order_id,))
        rows = await cur.fetchall()
        await cur.close()
    return [(r[0], r[1]) for r in rows]

async def delete_courier_message_records(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM courier_messages WHERE order_id=?", (order_id,))
        await db.commit()

async def delete_specific_courier_message_record(order_id, courier_tg_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            DELETE FROM courier_messages
            WHERE order_id=? AND courier_tg_id=? AND message_id=?
        """, (order_id, courier_tg_id, message_id))
        await db.commit()


# Безопасное удаление или редактирование старого сообщения
async def deactivate_or_delete_message(app, chat_id, message_id, text_override=None):
    """
    Пытаемся удалить сообщение. Если не получается (например, старое), пытаемся
    отредактировать текст/кнопки, чтобы убрать интерактивность.
    """
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        # Попытка отредактировать сообщение: убрать кнопки и поставить сообщение "неактуально"
        try:
            if text_override:
                await app.bot.edit_message_text(text_override, chat_id=chat_id, message_id=message_id, parse_mode="HTML")
            else:
                await app.bot.edit_message_reply_markup(chat_id=chat_id, message_id=message_id, reply_markup=None)
            return True
        except Exception as e2:
            logger.info(f"Не удалось деактивировать/удалить сообщение {message_id} в чате {chat_id}: {e2}")
            return False


# ==========================================================
# HTML REPORT
# ==========================================================
def html_report(order_id, o, courier_name, include_log=True):
    apt = o.get("to_apt") or "—"
    price_display = int(o.get("price") or 0)
    log_content = o.get("log") or "Лог пуст."

    # clickable
    def clickable(addr):
        if not addr:
            return "Н/Д"
        enc = urllib.parse.quote_plus(addr)
        return f'<a href="https://yandex.ru/maps/?text={enc}">{addr}</a>'

    report = (
        f"<b>ЗАКАЗ #{order_id}</b>\n\n"
        f"<b>Статус:</b> {o['status'].upper()}\n"
        f"<b>Цена доставки:</b> {price_display} ₽\n"
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


# ==========================================================
# SEND TO COURIERS (с сохранением message_id)
# ==========================================================
async def get_couriers():
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT tg_id FROM users WHERE role='courier'")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def send_order_to_couriers(order_id, app):
    """
    Отправляет заказ всем курьерам, сохраняет message_id каждой отправки
    """
    o = await get_order(order_id)
    if not o or o["status"] != "new":
        return

    txt = html_report(order_id, o, courier_name="—", include_log=False)
    kb = [[InlineKeyboardButton(f"🚀 Взять заказ #{order_id}", callback_data=f"take_{order_id}")]]
    markup = InlineKeyboardMarkup(kb)

    # Сначала удаляем старые записи и деактивируем старые сообщения
    existing = await get_courier_message_records(order_id)
    if existing:
        for cid, mid in existing:
            try:
                await deactivate_or_delete_message(app, cid, mid, text_override=f"❌ <b>Заказ #{order_id} неактуален (обновлён)</b>")
            except:
                continue
        await delete_courier_message_records(order_id)

    # Теперь отправляем новую рассылку
    couriers = await get_couriers()
    for cid in couriers:
        try:
            sent_msg = await app.bot.send_message(cid, txt, parse_mode="HTML", reply_markup=markup)
            # сохраняем записанную отправку
            if getattr(sent_msg, "message_id", None):
                await save_courier_message_record(order_id, cid, sent_msg.message_id)
        except Exception as e:
            logger.info(f"Не удалось отправить заказ {order_id} курьеру {cid}: {e}")
            continue


# ==========================================================
# START / ROLE
# ==========================================================
async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)

    if role:
        await update.message.reply_html(f"Вы уже зарегистрированы как <b>{role.upper()}</b>")
        return

    kb = [
        [InlineKeyboardButton("🏪 Магазин", callback_data="role_shop")],
        [InlineKeyboardButton("🛵 Курьер", callback_data="role_courier")],
    ]

    await update.message.reply_text("Выберите роль:", reply_markup=InlineKeyboardMarkup(kb))


async def role_choice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    tg = q.from_user.id

    if q.data == "role_shop":
        await set_role(tg, "shop")
        await q.edit_message_text("Ты теперь Магазин. Используй /new_order")
    else:
        await set_role(tg, "courier")
        await q.edit_message_text("Ты теперь Курьер. Жди заказов!")


# ==========================================================
# WHOAMI / STATS
# ==========================================================
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


# ==========================================================
# FSM — NEW / EDIT ORDER
# ==========================================================
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
        f"Текущая цена: <b>{current_price}</b>\nВведите новую цену:"
    )
    return DELIVERY_PRICE


async def step_price_final(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["price"] = float(update.message.text.replace(",", "."))
    except:
        await update.message.reply_html("⚠️ Введите корректное число")
        return DELIVERY_PRICE

    order_id = ctx.user_data.get("order_id")
    courier_id = ctx.user_data.get("courier_tg_id")

    if order_id is None:
        # создаём новый
        oid = await save_order(ctx.user_data)
        await send_order_to_couriers(oid, ctx.application)

        await update.message.reply_html(
            f"✅ <b>Заказ #{oid} создан</b>\nРазослан всем курьерам."
        )
    else:
        # обновляем
        oid = await update_order_details(order_id, ctx.user_data)
        await update.message.reply_html(f"✏️ <b>Заказ #{oid} обновлён</b>")

        new_order_data = await get_order(oid)
        courier_name = await get_username(courier_id, ctx.application)

        # Обновление у курьера, если заказ взят (status: taken)
        if courier_id:
            report = html_report(oid, new_order_data, courier_name, include_log=True)

            # Отправляем курьеру новое, актуальное сообщение с обновленным отчетом и кнопками
            try:
                sent_msg = await ctx.application.bot.send_message(
                    courier_id,
                    f"❗ <b>ЗАКАЗ #{oid} ОБНОВЛЕН!</b>\n\n{report}",
                    parse_mode="HTML",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{oid}")],
                        [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{oid}")],
                        [InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{oid}")],
                        [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{oid}")],
                        # ссылка на магазин (если есть)
                        [InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={new_order_data['shop_tg_id']}")]
                    ])
                )
                if getattr(sent_msg, "message_id", None):
                    # Удаляем все старые сообщения для этого заказа (так как данные изменились)
                    existing = await get_courier_message_records(oid)
                    for cid, mid in existing:
                        try:
                            await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"❌ <b>Заказ #{oid} неактуален (обновлён)</b>")
                        except:
                            continue
                    await delete_courier_message_records(oid)
                    # Сохраняем новую отправку (вдруг нужно будет деактивировать позже)
                    await save_courier_message_record(oid, courier_id, sent_msg.message_id)
            except Exception as e:
                logger.warning(f"Не удалось отправить обновление курьеру: {e}")

        # Обновление для всех курьеров, если заказ NEW
        else:
            # используем общий helper: удаляем старые сообщения и разослать новые
            await send_order_to_couriers(oid, ctx.application)

            await update.message.reply_html(f"✏️ <b>Заказ #{oid} обновлён</b>\nСтарые кнопки деактивированы. Новая версия разослана всем курьерам.")

    ctx.user_data.clear()
    return ConversationHandler.END


async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ctx.user_data.clear()
    await update.message.reply_html("Операция отменена.")
    return ConversationHandler.END


# ==========================================================
# MYORDERS
# ==========================================================
async def myorders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)

    if not role:
        await update.message.reply_text("Вы не выбрали роль.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        if role == "courier":
            # Курьер видит только свои взятые заказы
            query = "SELECT * FROM orders WHERE courier_tg_id=? AND status='taken' ORDER BY created_at DESC"
            params = (tg,)
        else:
            # Магазин видит свои активные заказы
            query = "SELECT * FROM orders WHERE shop_tg_id=? AND status NOT IN ('delivered','cancelled') ORDER BY created_at DESC"
            params = (tg,)

        cur = await db.execute(query, params)
        rows = await cur.fetchall()

    if not rows:
        await update.message.reply_html("<b>Нет активных заказов</b>")
        return

    for row in rows:
        (
            order_id, shop_tg_id, courier_tg_id,
            from_address, shop_contact,
            to_address, to_apt,
            client_name, client_phone, price,
            status, log, created_at
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
            "log": log
        }

        courier_name = await get_username(courier_tg_id, ctx.application)

        txt = html_report(order_id, o, courier_name, include_log=False)

        kb = []
        final_kb = []

        if role == "shop":
            # Магазин: кнопки редактирования и отмены
            if status in ("new", "taken"):
                kb.append(InlineKeyboardButton("✏️ Редактировать", callback_data=f"edit_{order_id}"))
            kb.append(InlineKeyboardButton("❌ Отменить", callback_data=f"shop_cancel_{order_id}"))

            # Если заказ взят — добавить кнопку написать курьеру (url)
            if status == "taken" and courier_tg_id:
                kb.append(InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_tg_id}"))

            if len(kb) == 2:
                final_kb.append(kb)
            else:
                final_kb = [[btn] for btn in kb]

        elif role == "courier":
            # Курьер: стандартные кнопки
            # Если заказ взят — добавить кнопку написать магазину (url) вверху
            if status == "taken" and shop_tg_id:
                final_kb.append([InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_tg_id}")])

            final_kb += [
                [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{order_id}")],
                [InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{order_id}")],
                [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")],
                [InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{order_id}")]
            ]

        if final_kb:
            await update.message.reply_html(txt, reply_markup=InlineKeyboardMarkup(final_kb))
        else:
            await update.message.reply_html(txt)


# ==========================================================
# COURIER ACTIONS
# ==========================================================
async def take_order(update: Update, ctx):
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

    # Кнопки для курьера — добавляем ссылку "написать магазину"
    kb = [
        [InlineKeyboardButton("📍 У магазина", callback_data=f"arrived_shop_{order_id}")],
        [InlineKeyboardButton("📍 У клиента", callback_data=f"arrived_client_{order_id}")],
        [InlineKeyboardButton("❌ Отменить", callback_data=f"cancel_{order_id}")],
        [InlineKeyboardButton("✅ Завершить", callback_data=f"finish_{order_id}")]
    ]

    # Добавляем URL-кнопку "Написать магазину" если есть shop id
    if o.get("shop_tg_id"):
        kb.append([InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={o['shop_tg_id']}")])

    # Отправляем сообщение курьеру и сохраняем его сообщение в БД (для последующих обновлений)
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
        # Редактируем сообщение рассылки, чтобы оно стало неактивным текстом
        await q.edit_message_text("Вы взяли заказ", reply_markup=None)
    except:
        pass

    # удаляем/деактивируем рассылку у других курьеров (оставляем только у взявшего, если нужно)
    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            if cid != courier_id:
                try:
                    await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"⛔ <b>Заказ #{order_id} уже взят</b>")
                except:
                    pass
                # удаляем запись
                try:
                    await delete_specific_courier_message_record(order_id, cid, mid)
                except:
                    pass
    except Exception as e:
        logger.warning(f"Ошибка при деактивации старых рассылок: {e}")

    # уведомляем магазин с кнопкой написать курьеру
    try:
        shop_kb = None
        if courier_id:
            shop_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_id}")]
            ])
        await ctx.application.bot.send_message(
            o["shop_tg_id"],
            f"🚀 Курьер {courier_name} взял заказ #{order_id}",
            parse_mode="HTML",
            reply_markup=shop_kb
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить магазин о взятии заказа: {e}")


async def arrived(update: Update, ctx):
    q = update.callback_query
    await q.answer()

    where = q.data.split("_")[1]  # shop/client
    order_id = int(q.data.split("_")[2])

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


async def cancel_order(update: Update, ctx):
    """
    Обработка отмены заказа самим курьером:
      - Сначала деактивируем/удаляем все сообщения рассылки (чтобы не осталось "старых" кнопок)
      - Затем явно сбрасываем courier_tg_id в NULL и статус -> new
      - Уведомляем магазин
      - Разсылаем заказ снова всем курьерам
    """
    q = update.callback_query
    await q.answer()

    order_id = int(q.data.split("_")[1])
    courier_id = q.from_user.id

    o = await get_order(order_id)
    if not o or o["courier_tg_id"] != courier_id:
        await q.edit_message_text("⛔ Этот заказ не ваш")
        return

    courier_name = await get_username(courier_id, ctx.application)

    # 1) Сначала очищаем и деактивируем все старые сообщения (если есть)
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

    # 2) Явно сбросим привязку курьера -> устанавливаем NULL (через courier=0 convention в update_order)
    await update_order(
        order_id,
        status="new",
        courier=0,  # special marker: update_order interprets 0 as NULL
        log_add=f"Курьер {courier_name} отменил заказ"
    )

    # 3) Уведомляем магазин что курьер отменил
    try:
        await ctx.application.bot.send_message(
            o["shop_tg_id"],
            f"❌ Курьер {courier_name} отменил заказ #{order_id}",
            parse_mode="HTML"
        )
    except:
        pass

    # 4) Разослать заказ всем курьерам заново
    await send_order_to_couriers(order_id, ctx.application)

    # 5) Ответ курьеру (который отменил)
    try:
        await q.edit_message_text(f"Заказ #{order_id} отменён и снова доступен курьерам", reply_markup=None)
    except:
        pass


async def finish_order(update: Update, ctx):
    """
    Завершение заказа курьером:
      - Ставим статус delivered
      - Формируем финальный отчет (html_report)
      - Редактируем сообщение именно того курьера (q.edit_message_text) — он увидит отчет
      - Отправляем отчет магазину (с кнопкой написать курьеру)
      - Деактивируем/удаляем рассылки у других курьеров, но НЕ трогаем финальное сообщение курьера, чтобы отчет остался видим
      - В БД заказ остаётся — это нужно для истории
    """
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

    # 1) Обновляем статус и лог
    await update_order(
        order_id,
        status="delivered",
        log_add=f"Курьер {courier_name} завершил заказ"
    )

    # 2) Получаем актуальные данные заказа
    o = await get_order(order_id)

    # 3) Формируем отчет
    report = html_report(order_id, o, courier_name, include_log=True)

    # 4) Редактируем сообщение у курьера — оставляем финальный отчет (не будем перезаписывать его далее)
    try:
        await q.edit_message_text(report, parse_mode="HTML", reply_markup=None)
    except:
        pass

    # 5) Отправляем финальный отчет магазину с кнопкой написать курьеру
    try:
        shop_kb = None
        if courier_id:
            shop_kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Написать курьеру", url=f"tg://user?id={courier_id}")]
            ])
        await ctx.application.bot.send_message(shop_id, report, parse_mode="HTML", reply_markup=shop_kb)
    except Exception as e:
        logger.warning(f"Не удалось отправить отчет магазину: {e}")

    # 6) Деактивируем/удаляем рассылки только у других курьеров (чтобы не трогать сообщение у курьера, который завершил заказ)
    try:
        existing = await get_courier_message_records(order_id)
        for cid, mid in existing:
            if cid == courier_id:
                # пропускаем — оставляем сообщение курьера как финальный отчет
                continue
            try:
                await deactivate_or_delete_message(ctx.application, cid, mid, text_override=f"ℹ️ <b>Заказ #{order_id} завершён</b>")
            except:
                pass
            # удаляем запись конкретно для этого курьера
            try:
                await delete_specific_courier_message_record(order_id, cid, mid)
            except:
                pass
        # не удаляем запись курьера (чтобы сообщение осталось и можно было увидеть его в чате)
    except Exception as e:
        logger.warning(f"Ошибка при очистке сообщений после завершения: {e}")


# ==========================================================
# SHOP CANCEL
# ==========================================================
async def shop_cancel_from_button(update: Update, ctx):
    q = update.callback_query
    await q.answer()

    order_id = int(q.data.split("_")[2])
    shop_id = q.from_user.id

    o = await get_order(order_id)
    if not o or o["shop_tg_id"] != shop_id:
        await q.answer("⛔ Нельзя отменить")
        return

    courier_id = o["courier_tg_id"]
    courier_name = await get_username(courier_id, ctx.application)

    await update_order(order_id, status="cancelled", log_add="Магазин отменил заказ")

    await q.edit_message_text(f"❌ Заказ #{order_id} отменён", reply_markup=None)

    # Уведомление и деактивация заказа у курьера(ов)
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

    # Если заказ был взят — уведомляем конкретного курьера (и даём ссылку на магазин в уведомлении)
    if courier_id:
        try:
            report = html_report(order_id, o, courier_name)
            # добавить кнопку "Написать магазину" в сообщение для курьера
            kb = InlineKeyboardMarkup([
                [InlineKeyboardButton("✉️ Написать магазину", url=f"tg://user?id={shop_id}")]
            ])
            await ctx.application.bot.send_message(courier_id, f"❌ <b>Заказ #{order_id} отменён магазином</b>\n\n{report}", parse_mode="HTML", reply_markup=kb)
        except:
            pass


# ==========================================================
# HISTORY COMMAND (для курьера и магазина)
# ==========================================================
async def history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    tg = update.effective_user.id
    role = await get_role(tg)
    if not role:
        await update.message.reply_text("Вы не выбрали роль.")
        return

    async with aiosqlite.connect(DB_PATH) as db:
        if role == "courier":
            # Полные отчеты по доставленным заказам, где courier_tg_id = tg and status = 'delivered'
            cur = await db.execute("""
                SELECT id, shop_tg_id, courier_tg_id, from_address, shop_contact,
                       to_address, to_apt, client_name, client_phone, price,
                       status, log, created_at
                FROM orders
                WHERE courier_tg_id=? AND status='delivered'
                ORDER BY created_at DESC
            """, (tg,))
        else:
            # Магазин: его доставленные заказы
            cur = await db.execute("""
                SELECT id, shop_tg_id, courier_tg_id, from_address, shop_contact,
                       to_address, to_apt, client_name, client_phone, price,
                       status, log, created_at
                FROM orders
                WHERE shop_tg_id=? AND status='delivered'
                ORDER BY created_at DESC
            """, (tg,))
        rows = await cur.fetchall()
        await cur.close()

    if not rows:
        await update.message.reply_html("<b>Нет доставленных заказов в истории.</b>")
        return

    total_sum = 0
    count = 0
    messages = []
    for row in rows:
        (
            order_id, shop_tg_id, courier_tg_id,
            from_address, shop_contact,
            to_address, to_apt,
            client_name, client_phone, price,
            status, log, created_at
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
            "log": log
        }

        courier_name = await get_username(courier_tg_id, ctx.application)
        report = html_report(order_id, o, courier_name, include_log=True)
        # добавим дату/время сверху
        created_ts = created_at or ""
        report_with_date = f"<b>Дата:</b> {created_ts}\n\n{report}"
        messages.append(report_with_date)

        total_sum += float(price or 0)
        count += 1

    # Соберём итоговое сообщение. Если очень длинное — разобьём на части (Telegram ограничение на длину сообщения)
    footer = f"\n\n<b>ИТОГО:</b>\nВсего заказов: {count}\nОбщая сумма: {int(total_sum)} ₽"
    full_text = "\n\n――――――――――――――――\n\n".join(messages) + footer

    # Telegram имеет ограничение на длину сообщения — разобьём на части по 4000 символов (примерно)
    MAX_CHUNK = 4000
    chunks = []
    cur_text = full_text
    while cur_text:
        if len(cur_text) <= MAX_CHUNK:
            chunks.append(cur_text)
            break
        # ищем последний перенос перед пределом
        cut = cur_text.rfind("\n\n", 0, MAX_CHUNK)
        if cut == -1:
            cut = MAX_CHUNK
        chunks.append(cur_text[:cut])
        cur_text = cur_text[cut:]

    for chunk in chunks:
        await update.message.reply_html(chunk)


# ==========================================================
# ERROR HANDLER
# ==========================================================
async def error_handler(update, ctx):
    logger.error("Ошибка:", exc_info=ctx.error)


# ==========================================================
# MAIN — ВАЖНО: правильный порядок handler-ов
# ==========================================================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.post_init = init_db

    app.add_error_handler(error_handler)

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

    app.add_handler(CallbackQueryHandler(role_choice, pattern="^role_"))
    app.add_handler(CallbackQueryHandler(take_order, pattern="^take_"))
    app.add_handler(CallbackQueryHandler(finish_order, pattern="^finish_"))
    app.add_handler(CallbackQueryHandler(cancel_order, pattern="^cancel_"))
    app.add_handler(CallbackQueryHandler(arrived, pattern="^arrived_"))
    app.add_handler(CallbackQueryHandler(shop_cancel_from_button, pattern="^shop_cancel_"))

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("whoami", whoami))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("myorders", myorders))
    app.add_handler(CommandHandler("history", history))  # <-- команда истории

    logger.info("BOT STARTED")
    app.run_polling()


if __name__ == "__main__":
    main()
