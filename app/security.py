import hashlib
import hmac
import json
import urllib.parse
from typing import Any

from app.config import settings


class AuthError(ValueError):
    pass


def parse_telegram_init_data(init_data: str) -> dict[str, Any]:
    if not init_data:
        raise AuthError("Telegram initData берилмаган")
    if not settings.bot_token:
        raise AuthError("BOTIM_BOT_TOKEN созланмаган")

    pairs = dict(urllib.parse.parse_qsl(init_data, keep_blank_values=True))
    received_hash = pairs.pop("hash", "")
    data_check_string = "\n".join(
        f"{key}={value}" for key, value in sorted(pairs.items())
    )
    secret_key = hmac.new(
        b"WebAppData", settings.bot_token.encode(), hashlib.sha256
    ).digest()
    computed_hash = hmac.new(
        secret_key, data_check_string.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise AuthError("Telegram initData нотўғри")

    return pairs


def extract_user_from_init_data(init_data: str) -> dict[str, Any]:
    data = parse_telegram_init_data(init_data)
    user_json = data.get("user")
    if not user_json:
        raise AuthError("Telegram user маълумоти йўқ")
    return json.loads(user_json)
