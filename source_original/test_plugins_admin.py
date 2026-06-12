# plugins/admin.py
import logging
import aiosqlite
import csv
import io
import re
import os
from datetime import datetime, timedelta
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import ContextTypes, ConversationHandler, MessageHandler, filters, CallbackQueryHandler, CommandHandler
from config import DB_PATH, is_owner

logger = logging.getLogger(__name__)

# ================== ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ==================
def translate_status(status: str) -> str:
    """Переводит статус заказа на русский"""
    status_map = {
        'new': '🆕 Новый',
        'taken': '🚀 Взят курьером',
        'at_shop': '🏪 У магазина',
        'on_delivery': '🚚 В пути',
        'at_client': '📍 У клиента',
        'delivered': '✅ Доставлен',
        'cancelled': '❌ Отменен',
        'cancelled_70_percent': '⚠️ Отменен (70%)',
        'failed_delivery': '🚫 Не удалось доставить',
        'completed_with_return': '↩️ Завершен с возвратом'
    }
    return status_map.get(status, status)

def format_date(timestamp_str: str) -> str:
    """Форматирует timestamp в читаемую дату"""
    try:
        dt = datetime.fromtimestamp(int(timestamp_str))
        return dt.strftime('%d.%m.%Y %H:%M')
    except:
        return timestamp_str

# ================== СОСТОЯНИЯ ДЛЯ FSM ==================
(
    SEARCH_CLIENT,      # Поиск по клиенту
    DATE_FILTER_START,  # Начало периода
    BROADCAST_CONFIRM,  # Подтверждение рассылки
    BROADCAST_SEND      # Отправка рассылки
) = range(4)

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
        [InlineKeyboardButton("👤 Поиск по клиенту", callback_data="admin_search_client")],
        [InlineKeyboardButton("📅 Фильтр по дате", callback_data="admin_filter_date")],
        [InlineKeyboardButton("📁 Экспорт в CSV", callback_data="admin_export_csv")],
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
        cursor = await db.execute("""
            SELECT o.id, o.status, o.price, o.created_at, 
                   o.shop_tg_id, o.courier_tg_id,
                   u1.phone as shop_phone, u2.phone as courier_phone
            FROM orders o
            LEFT JOIN users u1 ON o.shop_tg_id = u1.tg_id
            LEFT JOIN users u2 ON o.courier_tg_id = u2.tg_id
            ORDER BY o.created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset))
        
        orders = await cursor.fetchall()
        
        cursor = await db.execute("SELECT COUNT(*) FROM orders")
        total = (await cursor.fetchone())[0]
    
    if not orders:
        await query.edit_message_text("📭 Заказов не найдено")
        return
    
    text = f"📋 <b>Все заказы (стр. {page})</b>\n\n"
    
    for order in orders:
        order_id, status, price, created_at, shop_id, courier_id, shop_phone, courier_phone = order
        
        text += (
            f"<b>#{order_id}</b> | {translate_status(status)}\n"
            f"💰 {price}₽ | 🕒 {format_date(created_at)}\n"
            f"🏪 {shop_phone or 'Не указан'} | 🛵 {courier_phone or 'Не указан'}\n"
            f"{'─' * 30}\n"
        )
    
    buttons = []
    if page > 1:
        buttons.append(InlineKeyboardButton("⬅️ Назад", callback_data=f"admin_orders_page_{page-1}"))
    
    if offset + limit < total:
        buttons.append(InlineKeyboardButton("Вперед ➡️", callback_data=f"admin_orders_page_{page+1}"))
    
    buttons_row = []
    if buttons:
        buttons_row = [buttons] if len(buttons) == 1 else [buttons]
    
    buttons_row.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin_back")])
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(buttons_row)
    )

# ================== ПОИСК ПО КЛИЕНТУ ==================
async def search_client_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало поиска по клиенту"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "🔍 <b>Поиск заказов по клиенту</b>\n\n"
        "Вы можете искать по:\n"
        "1. Номеру телефона (например: 79001234567)\n"
        "2. Имени клиента\n"
        "3. ID клиента в Telegram (если есть)\n\n"
        "Введите данные для поиска:",
        parse_mode="HTML"
    )
    return SEARCH_CLIENT

async def search_client_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Выполнение поиска по клиенту"""
    search_term = update.message.text.strip()
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT o.id, o.status, o.price, o.created_at,
                   o.client_name, o.client_phone, o.shop_tg_id, o.courier_tg_id,
                   u1.phone as shop_phone, u2.phone as courier_phone
            FROM orders o
            LEFT JOIN users u1 ON o.shop_tg_id = u1.tg_id
            LEFT JOIN users u2 ON o.courier_tg_id = u2.tg_id
            WHERE o.client_phone LIKE ? 
               OR o.client_name LIKE ?
            ORDER BY o.created_at DESC
            LIMIT 50
        """, (f"%{search_term}%", f"%{search_term}%"))
        
        orders = await cursor.fetchall()
    
    if not orders:
        await update.message.reply_text(
            f"🔍 <b>Заказы не найдены</b>\nПо запросу: {search_term}",
            parse_mode="HTML"
        )
        return ConversationHandler.END
    
    text = f"🔍 <b>Результаты поиска: '{search_term}'</b>\n"
    text += f"📊 <i>Найдено заказов: {len(orders)}</i>\n\n"
    
    for order in orders:
        order_id, status, price, created_at, client_name, client_phone, shop_id, courier_id, shop_phone, courier_phone = order
        
        text += (
            f"<b>#{order_id}</b> | {translate_status(status)}\n"
            f"👤 <b>Клиент:</b> {client_name} ({client_phone})\n"
            f"💰 <b>Сумма:</b> {price}₽\n"
            f"🕒 <b>Дата:</b> {format_date(created_at)}\n"
            f"🏪 <b>Магазин:</b> {shop_phone or 'Не указан'}\n"
            f"🛵 <b>Курьер:</b> {courier_phone or 'Не указан'}\n"
            f"{'─' * 40}\n"
        )
    
    safe_search_term = search_term.replace(" ", "_")
    keyboard = [
        [InlineKeyboardButton("📁 Экспорт результатов", 
         callback_data=f"export_search_{safe_search_term}")],
        [InlineKeyboardButton("🔄 Новый поиск", 
         callback_data="admin_search_client")],
        [InlineKeyboardButton("⬅️ Назад", 
         callback_data="admin_back")]
    ]
    
    await update.message.reply_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return ConversationHandler.END

async def export_search_results(update: Update, context: ContextTypes.DEFAULT_TYPE, search_term: str):
    """Экспорт результатов поиска по клиенту"""
    query = update.callback_query
    await query.answer("Экспортируем результаты поиска...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT o.id, o.status, o.price, o.created_at,
                   o.client_name, o.client_phone,
                   o.from_address, o.to_address,
                   u1.phone as shop_phone, u2.phone as courier_phone,
                   o.shop_contact, o.paid_to_courier
            FROM orders o
            LEFT JOIN users u1 ON o.shop_tg_id = u1.tg_id
            LEFT JOIN users u2 ON o.courier_tg_id = u2.tg_id
            WHERE o.client_phone LIKE ? 
               OR o.client_name LIKE ?
            ORDER BY o.created_at DESC
        """, (f"%{search_term}%", f"%{search_term}%"))
        
        orders = await cursor.fetchall()
    
    if not orders:
        await query.edit_message_text("❌ Нет данных для экспорта")
        return
    
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    
    writer.writerow([
        'ID', 'Статус', 'Сумма (₽)', 'Дата создания',
        'Имя клиента', 'Телефон клиента',
        'Адрес отправки', 'Адрес доставки',
        'Телефон магазина', 'Телефон курьера',
        'Контакт магазина', 'Статус оплаты'
    ])
    
    for order in orders:
        order_id, status, price, created_at, client_name, client_phone, from_address, to_address, shop_phone, courier_phone, shop_contact, paid_to_courier = order
        
        date_str = datetime.fromtimestamp(int(created_at)).strftime('%Y-%m-%d %H:%M:%S')
        
        payment_status = 'Не оплачено'
        if paid_to_courier == 1:
            payment_status = 'Ожидает подтверждения'
        elif paid_to_courier == 2:
            payment_status = 'Оплачено'
        
        writer.writerow([
            order_id, status, price, date_str,
            client_name or '', client_phone or '',
            from_address or '', to_address or '',
            shop_phone or '', courier_phone or '',
            shop_contact or '', payment_status
        ])
    
    csv_content = output.getvalue()
    output.close()
    
    filename = f"search_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    try:
        with open(filename, 'w', encoding='utf-8-sig') as f:
            f.write(csv_content)
        
        await context.bot.send_document(
            chat_id=query.from_user.id,
            document=open(filename, 'rb'),
            filename=filename,
            caption=f"🔍 <b>Экспорт результатов поиска</b>\n"
                   f"📁 Поисковый запрос: {search_term}\n"
                   f"📊 Найдено записей: {len(orders)}",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Ошибка при экспорте поиска: {e}")
        await query.message.reply_text("❌ Ошибка при создании файла")
    finally:
        try:
            os.remove(filename)
        except:
            pass

async def search_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена поиска"""
    await update.message.reply_text("❌ Поиск отменен")
    return ConversationHandler.END

# ================== ФИЛЬТР ПО ДАТЕ ==================
async def filter_date_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Начало фильтрации по дате"""
    query = update.callback_query
    await query.answer()
    
    await query.edit_message_text(
        "📅 <b>Фильтр заказов по дате</b>\n\n"
        "Введите дату в формате:\n"
        "• <b>ДД.ММ.ГГГГ</b> (например: 15.12.2024)\n"
        "• <b>ДД.ММ.ГГГГ-ДД.ММ.ГГГГ</b> для периода\n\n"
        "Примеры:\n"
        "<code>15.12.2024</code> - заказы за 15 декабря\n"
        "<code>01.12.2024-15.12.2024</code> - заказы с 1 по 15 декабря",
        parse_mode="HTML"
    )
    return DATE_FILTER_START

async def filter_date_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработка введенной даты"""
    date_input = update.message.text.strip()
    
    if '-' in date_input:
        try:
            start_str, end_str = date_input.split('-')
            start_date = datetime.strptime(start_str.strip(), '%d.%m.%Y')
            end_date = datetime.strptime(end_str.strip(), '%d.%m.%Y')
            
            start_timestamp = int(start_date.timestamp())
            end_timestamp = int(end_date.timestamp()) + 86400
            
            period_text = f"{start_str} - {end_str}"
            
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM orders 
                    WHERE created_at BETWEEN ? AND ?
                """, (start_timestamp, end_timestamp))
                total = (await cursor.fetchone())[0]
            
            context.user_data['date_filter'] = {
                'type': 'period',
                'start': start_timestamp,
                'end': end_timestamp,
                'text': period_text
            }
            
            await update.message.reply_text(
                f"📅 <b>Выбран период:</b> {period_text}\n"
                f"📊 <b>Найдено заказов:</b> {total}\n\n"
                "Выберите действие:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👁️ Просмотреть", 
                     callback_data="view_date_filter")],
                    [InlineKeyboardButton("📁 Экспорт", 
                     callback_data="export_date_filter")],
                    [InlineKeyboardButton("⬅️ Назад", 
                     callback_data="admin_back")]
                ])
            )
            
        except ValueError:
            await update.message.reply_text(
                "❌ <b>Неверный формат даты!</b>\n"
                "Используйте формат: ДД.ММ.ГГГГ-ДД.ММ.ГГГГ\n"
                "Например: <code>01.12.2024-15.12.2024</code>",
                parse_mode="HTML"
            )
            return DATE_FILTER_START
    else:
        try:
            date_obj = datetime.strptime(date_input, '%d.%m.%Y')
            start_timestamp = int(date_obj.timestamp())
            end_timestamp = start_timestamp + 86400
            
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute("""
                    SELECT COUNT(*) FROM orders 
                    WHERE created_at BETWEEN ? AND ?
                """, (start_timestamp, end_timestamp))
                total = (await cursor.fetchone())[0]
            
            context.user_data['date_filter'] = {
                'type': 'single',
                'start': start_timestamp,
                'end': end_timestamp,
                'text': date_input
            }
            
            await update.message.reply_text(
                f"📅 <b>Выбран день:</b> {date_input}\n"
                f"📊 <b>Найдено заказов:</b> {total}\n\n"
                "Выберите действие:",
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("👁️ Просмотреть", 
                     callback_data="view_date_filter")],
                    [InlineKeyboardButton("📁 Экспорт", 
                     callback_data="export_date_filter")],
                    [InlineKeyboardButton("⬅️ Назад", 
                     callback_data="admin_back")]
                ])
            )
            
        except ValueError:
            await update.message.reply_text(
                "❌ <b>Неверный формат даты!</b>\n"
                "Используйте формат: ДД.ММ.ГГГГ\n"
                "Например: <code>15.12.2024</code>",
                parse_mode="HTML"
            )
            return DATE_FILTER_START
    
    return ConversationHandler.END

async def view_date_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Просмотр результатов фильтра по дате"""
    query = update.callback_query
    await query.answer()
    
    date_filter = context.user_data.get('date_filter', {})
    if not date_filter:
        await query.edit_message_text("❌ Фильтр не найден. Начните заново.")
        return
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT o.id, o.status, o.price, o.created_at,
                   o.client_name, o.client_phone, o.shop_tg_id, o.courier_tg_id,
                   u1.phone as shop_phone, u2.phone as courier_phone
            FROM orders o
            LEFT JOIN users u1 ON o.shop_tg_id = u1.tg_id
            LEFT JOIN users u2 ON o.courier_tg_id = u2.tg_id
            WHERE o.created_at BETWEEN ? AND ?
            ORDER BY o.created_at DESC
            LIMIT 30
        """, (date_filter['start'], date_filter['end']))
        
        orders = await cursor.fetchall()
    
    text = f"📅 <b>Заказы за период:</b> {date_filter['text']}\n"
    text += f"📊 <i>Показано: {len(orders)} заказов</i>\n\n"
    
    total_amount = 0
    status_counts = {}
    
    for order in orders:
        order_id, status, price, created_at, client_name, client_phone, shop_id, courier_id, shop_phone, courier_phone = order
        
        total_amount += float(price or 0)
        status_counts[status] = status_counts.get(status, 0) + 1
        
        text += (
            f"<b>#{order_id}</b> | {translate_status(status)}\n"
            f"👤 {client_name} | 💰 {price}₽\n"
            f"🕒 {format_date(created_at)}\n"
            f"{'─' * 30}\n"
        )
    
    text += f"\n📈 <b>Статистика:</b>\n"
    text += f"• Всего заказов: {len(orders)}\n"
    text += f"• Общая сумма: {total_amount}₽\n"
    
    for status, count in status_counts.items():
        text += f"• {translate_status(status)}: {count}\n"
    
    keyboard = [
        [InlineKeyboardButton("📁 Экспорт в CSV", 
         callback_data="export_date_filter")],
        [InlineKeyboardButton("🔄 Новый фильтр", 
         callback_data="admin_filter_date")],
        [InlineKeyboardButton("⬅️ Назад", 
         callback_data="admin_back")]
    ]
    
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== ЭКСПОРТ В CSV ==================
async def export_to_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Экспорт заказов в CSV"""
    query = update.callback_query
    await query.answer("Формируем CSV файл...")
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute("""
            SELECT o.id, o.status, o.price, o.created_at,
                   o.client_name, o.client_phone,
                   o.from_address, o.to_address,
                   u1.phone as shop_phone, u2.phone as courier_phone,
                   o.shop_contact, o.paid_to_courier
            FROM orders o
            LEFT JOIN users u1 ON o.shop_tg_id = u1.tg_id
            LEFT JOIN users u2 ON o.courier_tg_id = u2.tg_id
            ORDER BY o.created_at DESC
        """)
        
        orders = await cursor.fetchall()
    
    if not orders:
        await query.edit_message_text("❌ Нет данных для экспорта")
        return
    
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quoting=csv.QUOTE_MINIMAL)
    
    writer.writerow([
        'ID', 'Статус', 'Сумма (₽)', 'Дата создания',
        'Имя клиента', 'Телефон клиента',
        'Адрес отправки', 'Адрес доставки',
        'Телефон магазина', 'Телефон курьера',
        'Контакт магазина', 'Статус оплаты'
    ])
    
    for order in orders:
        order_id, status, price, created_at, client_name, client_phone, from_address, to_address, shop_phone, courier_phone, shop_contact, paid_to_courier = order
        
        date_str = datetime.fromtimestamp(int(created_at)).strftime('%Y-%m-%d %H:%M:%S')
        
        payment_status = 'Не оплачено'
        if paid_to_courier == 1:
            payment_status = 'Ожидает подтверждения'
        elif paid_to_courier == 2:
            payment_status = 'Оплачено'
        
        writer.writerow([
            order_id, status, price, date_str,
            client_name or '', client_phone or '',
            from_address or '', to_address or '',
            shop_phone or '', courier_phone or '',
            shop_contact or '', payment_status
        ])
    
    csv_content = output.getvalue()
    output.close()
    
    filename = f"orders_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    try:
        with open(filename, 'w', encoding='utf-8-sig') as f:
            f.write(csv_content)
        
        await context.bot.send_document(
            chat_id=query.from_user.id,
            document=open(filename, 'rb'),
            filename=filename,
            caption=f"📁 <b>Экспорт заказов</b>\n"
                   f"📊 Всего записей: {len(orders)}\n"
                   f"🕒 Создан: {datetime.now().strftime('%d.%m.%Y %H:%M')}",
            parse_mode="HTML"
        )
        await query.message.reply_text("✅ Файл успешно экспортирован!")
    except Exception as e:
        logger.error(f"Ошибка при экспорте CSV: {e}")
        await query.message.reply_text("❌ Ошибка при создании файла")
    finally:
        try:
            os.remove(filename)
        except:
            pass

async def filter_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Отмена фильтрации"""
    await update.message.reply_text("❌ Фильтр отменен")
    return ConversationHandler.END

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
    
    if audience == 'broadcast_shops':
        role_filter = "WHERE role = 'shop'"
    elif audience == 'broadcast_couriers':
        role_filter = "WHERE role = 'courier'"
    else:
        role_filter = ""
    
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(f"SELECT tg_id FROM users {role_filter}")
        users = await cursor.fetchall()
    
    total_users = len(users)
    sent = 0
    failed = 0
    
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
    
    async with aiosqlite.connect(DB_PATH) as db:
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
    
    try:
        with open('bot.log', 'r', encoding='utf-8') as f:
            logs = f.readlines()[-50:]
    except FileNotFoundError:
        logs = ["Файл логов не найден"]
    
    text = "📜 <b>Последние логи системы</b>\n\n"
    text += "```\n"
    text += "".join(logs[-20:])
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
            await query.edit_message_text("🔄 Активные заказы...")
        elif data == "admin_orders_completed":
            await query.edit_message_text("✅ Завершенные заказы...")
        elif data == "admin_search_client":
            await search_client_start(update, context)
        elif data == "admin_filter_date":
            await filter_date_start(update, context)
        elif data == "admin_export_csv":
            await export_to_csv(update, context)
        elif data.startswith("admin_orders_page_"):
            page = int(data.replace("admin_orders_page_", ""))
            context.user_data['orders_page'] = page
            await show_all_orders(update, context)
    
    elif data.startswith("export_search_"):
        search_term = data.replace("export_search_", "").replace("_", " ")
        await export_search_results(update, context, search_term)
    
    elif data == "view_date_filter":
        await view_date_filter(update, context)
    elif data == "export_date_filter":
        await export_to_csv(update, context)
    
    elif data == "admin_shops_stats":
        await query.edit_message_text("📊 Статистика магазинов...")
    
    elif data == "admin_logs_refresh":
        await admin_logs(update, context)

# ================== REGISTRATION ==================
def register_admin_handlers(app):
    """Регистрация всех обработчиков админ-панели"""
    
    app.add_handler(MessageHandler(filters.Regex("^🧾 Заказы$"), admin_orders))
    app.add_handler(MessageHandler(filters.Regex("^🏪 Магазины$"), admin_shops))
    app.add_handler(MessageHandler(filters.Regex("^🛵 Курьеры$"), admin_couriers))
    app.add_handler(MessageHandler(filters.Regex("^📢 Рассылка$"), admin_broadcast))
    app.add_handler(MessageHandler(filters.Regex("^⚙️ Настройки$"), admin_settings))
    app.add_handler(MessageHandler(filters.Regex("^📜 Логи$"), admin_logs))
    app.add_handler(MessageHandler(filters.Regex("^📊 Статистика$"), admin_settings))
    
    # ConversationHandler для поиска по клиенту
    search_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(search_client_start, pattern="^admin_search_client$")],
        states={
            SEARCH_CLIENT: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_client_execute)]
        },
        fallbacks=[
            CommandHandler("cancel", search_cancel)
        ]
    )
    
    # ConversationHandler для фильтра по дате
    date_filter_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(filter_date_start, pattern="^admin_filter_date$")],
        states={
            DATE_FILTER_START: [MessageHandler(filters.TEXT & ~filters.COMMAND, filter_date_process)]
        },
        fallbacks=[
            CommandHandler("cancel", filter_cancel)
        ]
    )
    
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
    
    app.add_handler(search_conv)
    app.add_handler(date_filter_conv)
    app.add_handler(broadcast_conv)
    
    app.add_handler(CallbackQueryHandler(admin_callback_handler, pattern="^admin_"))
    app.add_handler(CallbackQueryHandler(view_date_filter, pattern="^view_date_filter$"))
    app.add_handler(CallbackQueryHandler(export_to_csv, pattern="^export_date_filter$"))
    app.add_handler(CallbackQueryHandler(export_to_csv, pattern="^admin_export_csv$"))
    
    logger.info("Admin panel handlers registered successfully")