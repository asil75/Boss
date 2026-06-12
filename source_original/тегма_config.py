# Файл общих настроек
DB_PATH = "db.sqlite3"
OWNER_ID = 1309289031  # Твой Telegram ID
BOT_TOKEN = "8555882487:AAFyl9juLHiZ33FIjcretFe0U2yIDau1pYs"

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID