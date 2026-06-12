from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse

from app.config import BASE_DIR, settings
from app.db import get_db
from app.schemas import UserOut
from app.auth import telegram_user_from_request
from app.services.users import upsert_user

router = APIRouter()


@router.get("/miniapp")
async def miniapp_page():
    return FileResponse(BASE_DIR / "app" / "static" / "index.html")


@router.get("/api/me")
async def me(request: Request, db=Depends(get_db)):
    telegram_user = telegram_user_from_request(request)
    user = await upsert_user(
        db,
        int(telegram_user["id"]),
        first_name=telegram_user.get("first_name"),
        last_name=telegram_user.get("last_name"),
        username=telegram_user.get("username"),
        language_code=telegram_user.get("language_code"),
    )
    return user
