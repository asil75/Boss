from typing import Any

from fastapi import Request

from app.config import settings
from app.security import extract_user_from_init_data


class DevUserError(ValueError):
    pass


def telegram_user_from_request(request: Request) -> dict[str, Any]:
    init_data = request.headers.get("x-telegram-init-data", "")
    if init_data:
        return extract_user_from_init_data(init_data)

    if not settings.dev_mode:
        raise ValueError("Telegram initData керак")

    tg_id = request.headers.get("x-user-id") or request.headers.get("x-telegram-user-id")
    if not tg_id or not str(tg_id).isdigit():
        raise DevUserError("Dev mode учун X-User-Id header керак")

    return {
        "id": int(tg_id),
        "first_name": request.headers.get("x-user-first-name", "Dev"),
        "last_name": request.headers.get("x-user-last-name"),
        "username": request.headers.get("x-user-username"),
        "language_code": request.headers.get("x-user-language-code", "uz"),
    }
