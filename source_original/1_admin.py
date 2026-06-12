# plugins/admin.py
import logging
import aiosqlite
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler
from config import DB_PATH, is_owner

logger = logging.getLogger(__name__)

# ================== ADMIN PANEL ENTRY ==================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает главное меню админ-панели"""
    if not is_owner(update.effective_user.id):
        await update.message.reply_text("⛔ Доступ запрещён")
        return

    keyboard = [
        ["🧾 Заказы", "👥 Пользователи"],
        ["🏪 Магазины", "🛵 Курьеры"],
        ["📢 Рассылка", "⚙️ Настройки"],
        ["📜 Логи", "📊 Статистика"]
    ]

    await update.message.reply_text(
        "👑 Админ-панель\nВыберите раздел:",
        reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    )

# ================== ORDERS MANAGEMENT ==================
async def admin_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Управление заказами - главное меню"""
    if not is_owner(update.effective_user.id):
        return

    keyboard = [
        [InlineKeyboardButton("📋 Все заказы", callback_data="admin_orders_all")],
        [InlineKeyboardButton("🔄 Активные", callback_data="admin_orders_active")],
        [InlineKeyboardButton("✅ Завершенные", callback_data="admin_orders_completed")],
        [InlineKeyboardButton("❌ Отмененные", callback_data="admin_orders_cancelled")],
        [InlineKeyboardButton("🔍 Поиск по ID", callback_data="admin_orders_search")],
        [InlineKeyboardButton("📈 Статистика", callback_data="admin_orders_stats")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")]
    ]

    await update.message.reply_text(
        "🧾 <b>Управление заказами</b>\nВыберите действие:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def show_all_orders(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает все заказы с пагинацией"""
    query = update.callback_query
    await query.answer()
    
    page = int(context.user_data.get('orders_page', 1))
    limit = 5
    offset = (page - 1) * limit
    
    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем заказы
        cursor = await db.execute("""
            SELECT o.id, o.status, o.price, o.created_at, 
                   o.shop_tg_id, o.courier_tg_id,
                   u1.role as shop_role, u2.role as courier_role
            FROM orders o
            LEFT JOIN users u1 ON o.shop_tg_id = u1.tg_id
            LEFT JOIN users u2 ON o.courier_tg_id = u2.tg_id
            ORDER BY o.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        
        orders = await cursor.fetchall()
        
        # Считаем общее количество
        cursor = await db.execute("SELECT COUNT(*) FROM orders")
        total = (await cursor.fetchone())[0]
    
    if not orders:
        await query.edit_message_text("📭 Заказов не найдено")
        return
    
    text = f"📋 <b>Все заказы (стр. {page})</b>\n\n"
    
    for order in orders:
        order_id, status, price, created_at, shop_id, courier_id, shop_role, courier_role = order
        
        # Форматируем время
        created_time = datetime.fromtimestamp(int(created_at)).strftime('%d.%m %H:%M')
        
        text += (
            f"<b>#{order_id}</b> | {status.upper()}\n"
            f"💰 {price}₽ | 🕒 {created_time}\n"
            f"🏪 {shop_role if shop_role else 'N/A'} | 🛵 {courier_role if courier_role else 'N/A'}\n"
            f"{'─' * 30}\n"
        )
    
    # Кнопки пагинации
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_orders_page_{page-1}"))
    
    if offset + limit < total:
        buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"admin_orders_page_{page+1}"))
    
    buttons_row = []
    if buttons:
        buttons_row = [buttons] if len(buttons) == 1 else [buttons]
    
    buttons_row.append([InlineKeyboardButton("🔍 Детали", callback_data=f"admin_order_detail_{order_id}")])
    buttons_row.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_orders")])
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons_row)
    )

# ================== SHOPS & COURIERS LISTS ==================
async def admin_shops(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список всех магазинов"""
    if not is_owner(update.effective_user.id):
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT u.tg_id, u.phone, u.is_blocked,
                   COUNT(o.id) as total_orders,
                   SUM(CASE WHEN o.status = 'delivered' THEN 1 ELSE 0 END) as completed_orders
            FROM users u
            LEFT JOIN orders o ON u.tg_id = o.shop_tg_id
            WHERE u.role = 'shop'
            GROUP BY u.tg_id
            ORDER BY total_orders DESC
        """)
        
        shops = await cursor.fetchall()
    
    if not shops:
        await update.message.reply_text("🏪 Магазины не найдены")
        return
    
    text = "🏪 <b>Список магазинов</b>\n\n"
    
    for shop in shops:
        tg_id, phone, is_blocked, total_orders, completed_orders = shop
        
        status = "🔴 Заблокирован" if is_blocked else "🟢 Активен"
        completion_rate = (completed_orders / total_orders * 100) if total_orders > 0 else 0
        
        text += (
            f"<b>ID:</b> {tg_id}\n"
            f"<b>Телефон:</b> {phone or 'Не указан'}\n"
            f"<b>Статус:</b> {status}\n"
            f"<b>Заказов:</b> {total_orders} (✅ {completed_orders})\n"
            f"<b>Успешных:</b> {completion_rate:.1f}%\n"
            f"{'─' * 30}\n"
        )
    
    keyboard = [[
        InlineKeyboardButton("📊 Статистика", callback_data="admin_shops_stats"),
        InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")
    ]]
    
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def admin_couriers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Список всех курьеров"""
    if not is_owner(update.effective_user.id):
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT u.tg_id, u.phone, u.is_blocked,
                   COUNT(o.id) as total_orders,
                   SUM(CASE WHEN o.status = 'delivered' THEN 1 ELSE 0 END) as completed_orders,
                   SUM(o.price) as total_earned
            FROM users u
            LEFT JOIN orders o ON u.tg_id = o.courier_tg_id
            WHERE u.role = 'courier'
            GROUP BY u.tg_id
            ORDER BY total_earned DESC
        """)
        
        couriers = await cursor.fetchall()
    
    if not couriers:
        await update.message.reply_text("🛵 Курьеры не найдены")
        return
    
    text = "🛵 <b>Список курьеров</b>\n\n"
    
    for courier in couriers:
        tg_id, phone, is_blocked, total_orders, completed_orders, total_earned = courier
        
        status = "🔴 Заблокирован" if is_blocked else "🟢 Активен"
        completion_rate = (completed_orders / total_orders * 100) if total_orders > 0 else 0
        
        text += (
            f"<b>ID:</b> {tg_id}\n"
            f"<b>Телефон:</b> {phone or 'Не указан'}\n"
            f"<b>Статус:</b> {status}\n"
            f"<b>Доставок:</b> {total_orders} (✅ {completed_orders})\n"
            f"<b>Успешных:</b> {completion_rate:.1f}%\n"
            f"<b>Заработал:</b> {total_earned or 0}₽\n"
            f"{'─' * 30}\n"
        )
    
    keyboard = [[
        InlineKeyboardButton("📊 Статистика", callback_data="admin_couriers_stats"),
        InlineKeyboardButton("📋 Рейтинг", callback_data="admin_couriers_rating"),
        InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")
    ]]
    
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== BROADCAST SYSTEM ==================
BROADCAST_CONFIRM, BROADCAST_SEND = range(2)

async def admin_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало рассылки сообщений"""
    if not is_owner(update.effective_user.id):
        return
    
    keyboard = [
        [InlineKeyboardButton("📢 Всем пользователям", callback_data="broadcast_all")],
        [InlineKeyboardButton("🏪 Только магазинам", callback_data="broadcast_shops")],
        [InlineKeyboardButton("🛵 Только курьерам", callback_data="broadcast_couriers")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        "📢 <b>Рассылка сообщений</b>\nВыберите аудиторию:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return BROADCAST_CONFIRM

async def broadcast_select_audience(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выбор аудитории для рассылки"""
    query = update.callback_query
    await query.answer()
    
    audience = query.data
    context.user_data['broadcast_audience'] = audience
    
    audiences = {
        'broadcast_all': 'всем пользователям',
        'broadcast_shops': 'только магазинам',
        'broadcast_couriers': 'только курьерам'
    }
    
    await query.edit_message_text(
        f"📝 Вы выбрали рассылку: <b>{audiences[audience]}</b>\n\n"
        "Теперь отправьте сообщение для рассылки.\n"
        "Вы можете использовать HTML-разметку.",
        parse_mode="HTML"
    )
    return BROADCAST_SEND

async def broadcast_send_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отправка сообщения для рассылки"""
    message_text = update.message.text
    audience = context.user_data.get('broadcast_audience', 'broadcast_all')
    
    # Определяем получателей
    if audience == 'broadcast_shops':
        role_filter = "WHERE role = 'shop'"
    elif audience == 'broadcast_couriers':
        role_filter = "WHERE role = 'courier'"
    else:
        role_filter = ""
    
    # Получаем список пользователей
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(f"SELECT tg_id FROM users {role_filter}")
        users = await cursor.fetchall()
    
    total_users = len(users)
    sent = 0
    failed = 0
    
    # Отправляем сообщение каждому пользователю
    for user in users:
        try:
            await context.bot.send_message(
                chat_id=user[0],
                text=message_text,
                parse_mode="HTML"
            )
            sent += 1
        except Exception as e:
            logger.error(f"Ошибка отправки пользователю {user[0]}: {e}")
            failed += 1
    
    await update.message.reply_text(
        f"✅ <b>Рассылка завершена!</b>\n\n"
        f"📊 Статистика:\n"
        f"• Всего получателей: {total_users}\n"
        f"• Успешно отправлено: {sent}\n"
        f"• Не удалось отправить: {failed}\n"
        f"• Процент успеха: {(sent/total_users*100):.1f}%",
        parse_mode="HTML"
    )
    
    context.user_data.clear()
    return ConversationHandler.END

async def broadcast_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена рассылки"""
    context.user_data.clear()
    await update.message.reply_text("❌ Рассылка отменена")
    return ConversationHandler.END

# ================== SETTINGS ==================
async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Настройки бота"""
    if not is_owner(update.effective_user.id):
        return
    
    # Получаем текущую статистику
    async with aiosqlite.connect(DB_PATH) as db:
        # Общая статистика
        cursor = await db.execute("SELECT COUNT(*) FROM users")
        total_users = (await cursor.fetchone())[0]
        
        cursor = await db.execute("SELECT COUNT(*) FROM orders")
        total_orders = (await cursor.fetchone())[0]
        
        cursor = await db.execute("SELECT SUM(price) FROM orders WHERE status = 'delivered'")
        total_money = (await cursor.fetchone())[0] or 0
    
    text = (
        "⚙️ <b>Настройки бота</b>\n\n"
        f"📊 <b>Статистика системы:</b>\n"
        f"• Пользователей: {total_users}\n"
        f"• Заказов: {total_orders}\n"
        f"• Оборот: {total_money}₽\n\n"
        f"<b>Доступные настройки:</b>"
    )
    
    keyboard = [
        [InlineKeyboardButton("📊 Общая статистика", callback_data="settings_stats")],
        [InlineKeyboardButton("🔄 Сброс статистики", callback_data="settings_reset")],
        [InlineKeyboardButton("💾 Экспорт данных", callback_data="settings_export")],
        [InlineKeyboardButton("🛠 Тех. обслуживание", callback_data="settings_maintenance")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== LOGS VIEWER ==================
async def admin_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр логов системы"""
    if not is_owner(update.effective_user.id):
        return
    
    # Получаем последние логи из базы (если у вас есть таблица logs)
    # Или читаем из файла
    try:
        with open('bot.log', 'r', encoding='utf-8') as f:
            logs = f.readlines()[-50:]  # Последние 50 строк
    except FileNotFoundError:
        logs = ["Файл логов не найден"]
    
    text = "📜 <b>Последние логи системы</b>\n\n"
    text += "```\n"
    text += "".join(logs[-20:])  # Показываем последние 20 строк
    text += "\n```"
    
    keyboard = [
        [InlineKeyboardButton("🔄 Обновить", callback_data="admin_logs_refresh")],
        [InlineKeyboardButton("📥 Скачать логи", callback_data="admin_logs_download")],
        [InlineKeyboardButton("🧹 Очистить логи", callback_data="admin_logs_clear")],
        [InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== CALLBACK HANDLERS ==================
async def admin_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик callback-ов админ-панели"""
    query = update.callback_query
    await query.answer()
    
    data = query.data
    
    if data == "admin_back":
        # Возвращаемся в главное меню админки
        keyboard = [
            ["🧾 Заказы", "👥 Пользователи"],
            ["🏪 Магазины", "🛵 Курьеры"],
            ["📢 Рассылка", "⚙️ Настройки"],
            ["📜 Логи", "📊 Статистика"]
        ]
        
        await query.edit_message_text(
            "👑 Админ-панель\nВыберите раздел:",
            reply_markup=ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        )
    
    elif data.startswith("admin_orders"):
        if data == "admin_orders_all":
            await show_all_orders(update, context)
        elif data == "admin_orders_active":
            await query.edit_message_text("🔄 Активные заказы... (функция в разработке)")
        elif data == "admin_orders_completed":
            await query.edit_message_text("✅ Завершенные заказы... (функция в разработке)")
    
    elif data == "admin_shops_stats":
        await query.edit_message_text("📊 Статистика магазинов... (функция в разработке)")
    
    elif data == "admin_logs_refresh":
        await admin_logs(update, context)

# ================== REGISTRATION ==================
def register_admin_handlers(app):
    """Регистрация всех обработчиков админ-панели"""
    
    # Главная команда админки (уже есть в bot.py)
    # app.add_handler(CommandHandler("admin", admin_panel))
    
    # Обработчики кнопок админ-панели
    app.add_handler(MessageHandler(filters.Regex("^🧾 Заказы$"), admin_orders))
    app.add_handler(MessageHandler(filters.Regex("^🏪 Магазины$"), admin_shops))
    app.add_handler(MessageHandler(filters.Regex("^🛵 Курьеры$"), admin_couriers))
    app.add_handler(MessageHandler(filters.Regex("^📢 Рассылка$"), admin_broadcast))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Настройки$"), admin_settings))
    app.add_handler(MessageHandler(filters.Regex("^📜 Логи$"), admin_logs))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), admin_settings))  # Пока объединим с настройками
    
    # ConversationHandler для рассылки
    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 Рассылка$"), admin_broadcast)],
        states={
            BROADCAST_CONFIRM: [CallbackQueryHandler(broadcast_select_audience, pattern="^broadcast_")],
            BROADCAST_SEND: [MessageHandler(filters.TEXT & ~filters.COMMAND, broadcast_send_message)]
        },
        fallbacks=[
            MessageHandler(filters.Regex("^Отмена$"), broadcast_cancel),
            CommandHandler("cancel", broadcast_cancel)
        ]
    )
    
    app.add_handler(broadcast_conv)
    
    # Обработчик callback-ов админки
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
    
    logger.info("Admin panel handlers registered successfully")
