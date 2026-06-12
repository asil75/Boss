from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, MessageHandler, CallbackQueryHandler, filters
import aiosqlite
import logging
from config import DB_PATH, is_owner

logger = logging.getLogger(__name__)

# Количество пользователей на одной странице
PAGE_SIZE = 8

async def send_users_page(update: Update, page: int):
    """Отправка страницы со списком пользователей"""
    offset = page * PAGE_SIZE
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Считаем общее количество для пагинации
        async with db.execute("SELECT COUNT(*) FROM users") as c:
            total = (await c.fetchone())[0]
            
        async with db.execute(
            "SELECT tg_id, role, phone, is_blocked FROM users ORDER BY id DESC LIMIT ? OFFSET ?",
            (PAGE_SIZE, offset)
        ) as cursor:
            rows = await cursor.fetchall()

    if not rows:
        text = "👥 Пользователи не найдены."
        kb = []
    else:
        text = f"👥 <b>Управление пользователями</b>\nВсего: {total} | Стр: {page + 1}\n\n"
        kb = []
        for tg_id, role, phone, is_blocked in rows:
            status = "⛔" if is_blocked else "✅"
            role_text = "🏪 Магазин" if role == "shop" else "🛵 Курьер" if role == "courier" else "👤 Клиент"
            text += f"{status} <code>{tg_id}</code> | {role_text} | {phone or '—'}\n"
            
            # Кнопка для блокировки/разблокировки конкретного юзера
            btn_text = "🔓 Разблокировать" if is_blocked else "🔒 Заблокировать"
            kb.append([InlineKeyboardButton(f"{btn_text} {tg_id}", callback_data=f"us_toggle:{tg_id}:{page}")])

        # Кнопки навигации
        nav = []
        if page > 0:
            nav.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"us_page:{page-1}"))
        if offset + PAGE_SIZE < total:
            nav.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"us_page:{page+1}"))
        if nav:
            kb.append(nav)
        
        # Кнопка возврата в админку
        kb.append([InlineKeyboardButton("🔙 В админ-панель", callback_data="admin_back")])

    if update.message:
        await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

async def admin_users_entry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вход в раздел управления пользователями"""
    if not is_owner(update.effective_user.id): 
        await update.message.reply_text("⛔ Доступ запрещён")
        return
    await send_users_page(update, 0)

async def toggle_block_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Переключение блокировки пользователя"""
    query = update.callback_query
    if not is_owner(query.from_user.id): 
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
    
    # Данные: us_toggle:TG_ID:PAGE
    _, tg_id, page = query.data.split(":")
    tg_id, page = int(tg_id), int(page)

    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT is_blocked FROM users WHERE tg_id=?", (tg_id,))
        row = await cur.fetchone()
        if not row:
            await query.answer("❌ Пользователь не найден", show_alert=True)
            return
        
        new_status = 0 if row[0] else 1
        await db.execute("UPDATE users SET is_blocked=? WHERE tg_id=?", (new_status, tg_id))
        await db.commit()

    status_text = "заблокирован" if new_status else "разблокирован"
    await query.answer(f"✅ Пользователь {status_text}")
    await send_users_page(update, page)

async def page_nav_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Навигация по страницам"""
    query = update.callback_query
    if not is_owner(query.from_user.id):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return
        
    page = int(query.data.split(":")[1])
    await send_users_page(update, page)
    await query.answer()

def register(app):
    """Регистрация обработчиков модуля пользователей"""
    # Реагируем на кнопку в админке
    app.add_handler(MessageHandler(filters.TEXT & filters.Regex("^👥 Пользователи$"), admin_users_entry))
    # Обработка переключения страниц
    app.add_handler(CallbackQueryHandler(page_nav_cb, pattern="^us_page:"))
    # Обработка блокировки
    app.add_handler(CallbackQueryHandler(toggle_block_cb, pattern="^us_toggle:"))
    
    logger.info("✅ Users management handlers registered")
