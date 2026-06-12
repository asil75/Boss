# Botim Mini App

Янги модулли лойиҳа. Юкланган эски файллар `source_original/` ичида сақланган, ишлатиладиган тоза лойиҳа эса `app/`, `bot_worker.py` ва `data/` ичида жойлаштирилган.

## Қайси версия асос қилиб олинди

Менинг фикримча давом эттириш учун энг мос файл: `source_original/1bot.py`.

Сабаблари:

- `phone`, `is_blocked`, `owner` ҳимояси бор.
- Магазин, курьер ва OWNER роле аниқ ажратилган.
- Заказать, тўлов, статистика, выплата ва админ панел логикаси тўлиқроқ.
- Plugin (`plugins/users.py`, `plugins/admin.py`) тузилмасига мос келади, шу учун кейинчалик модулли қилиш осон.

## Лойиҳа тузилмаси

- `app/main.py` — FastAPI ишга тушиш нуқтаси.
- `app/config.py` — `.env` созламалари.
- `app/db.py` — SQLite улаш ва миграциялар.
- `app/security.py` — Telegram Mini App `initData` текшируви.
- `app/services/` — users, orders, payments, stats бизнес логикаси.
- `app/routers/` — API route лар.
- `app/static/` — Mini App frontend.
- `bot_worker.py` — Telegram бот учун минимал `/start` веб-иллова тугмаси.
- `data/botim.sqlite3` — юкланган `delivery_full.db` нусхаси.

## Ишга тушириш

```bash
cp .env.example .env
# .env ичига BOTIM_BOT_TOKEN ва BOTIM_OWNER_ID ни тўлдиринг

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

Mini App URL:

```text
http://localhost:8000/static/index.html
```

API:

- `GET /api/health`
- `GET /api/me`
- `GET /api/orders`
- `POST /api/orders`
- `PATCH /api/orders/{order_id}/take`
- `PATCH /api/orders/{order_id}/status`
- `POST /api/payments/{order_id}/mark-paid`
- `POST /api/payments/{order_id}/confirm`
- `GET /api/payments/summary`
- `GET /api/stats`
- `GET /api/users`
- `POST /api/users/{tg_id}/role`
- `POST /api/users/{tg_id}/block`

Telegram Mini App да `initData` автоматик юборилади. Local dev учун `.env` да `BOTIM_DEV_MODE=true` бўлса, `X-User-Id` header билан ҳам текшириш мумкин.

## Эслатма

Эски `config.py` ва `*.py` файллардаги Bot token лар лойиҳага кўчирилмади. Token фақат `.env` да бўлиши керак.

## Валидация

Лойиҳа текширилди:

- `python -m venv .venv`
- `pip install -r requirements.txt`
- `python -m compileall app bot_worker.py`
- `uvicorn app.main:app --reload`
- `/api/health`, `/api/config`, `/api/me`, `/api/orders`, `/api/stats`, `/static/index.html` smoke test

Эслатма: `.venv/` локал текширув учун яратилди, кейин ўчирилди.
