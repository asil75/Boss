from fastapi import APIRouter, Depends, Request

from app.db import get_db
from app.schemas import UserOut
from app.auth import telegram_user_from_request
from app.services.stats import stats
from app.services.users import upsert_user

router = APIRouter(prefix="/api/stats", tags=["stats"])


@router.get("")
async def get_stats(request: Request, db=Depends(get_db)):
    telegram_user = telegram_user_from_request(request)
    user = await upsert_user(
        db,
        int(telegram_user["id"]),
        first_name=telegram_user.get("first_name"),
        last_name=telegram_user.get("last_name"),
        username=telegram_user.get("username"),
        language_code=telegram_user.get("language_code"),
    )
    return await stats(db, user)
