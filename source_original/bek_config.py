# config.py
DB_PATH = "db.sqlite3"
OWNER_ID = 1309289031

def is_owner(user_id: int) -> bool:
    return user_id == OWNER_ID

