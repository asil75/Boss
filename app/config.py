from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    bot_token: str = ""
    owner_id: int = 0
    db_path: str = "data/botim.sqlite3"
    app_name: str = "Botim Delivery"
    mini_app_url: str = "http://localhost:8000/static/index.html"
    dev_mode: bool = False

    model_config = SettingsConfigDict(
        env_prefix="BOTIM_",
        env_file=".env",
        extra="ignore",
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
BASE_DIR = Path(__file__).resolve().parent.parent
DB_PATH = BASE_DIR / settings.db_path
