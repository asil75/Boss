# ================== CONFIGURATION ==================
# Храните здесь свои секретные данные

BOT_TOKEN = "8555882487:AAFyl9juLHiZ33FIjcretFe0U2yIDau1pYs"
OWNER_ID = 1309289031
DB_PATH = "db.sqlite3"

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID


