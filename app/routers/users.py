from fastapi import APIRouter, Depends, Query, Request

from app.config import settings
from app.db import get_db
from app.schemas import BlockUserRequest, SetRoleRequest, UserOut
from app.auth import telegram_user_from_request
from app.services.users import list_users, set_blocked, set_phone, set_role, upsert_user

router = APIRouter(prefix="/api/users", tags=["users"])


@router.get("")
async def get_users(
    request: Request,
    db=Depends(get_db),
    role: str | None = None,
    is_blocked: bool | None = None,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
):
    current = await current_user(request, db)
    if current.tg_id != settings.owner_id:
        raise PermissionError("Фақат OWNER фойдаланувчилар рўйхатини кўра олади.")
    return await list_users(db, role=role, is_blocked=is_blocked, limit=limit, offset=offset)


@router.post("/{tg_id}/role")
async def post_role(tg_id: int, payload: SetRoleRequest, request: Request, db=Depends(get_db)):
    current = await current_user(request, db)
    if current.tg_id != settings.owner_id:
        raise PermissionError("Фақат OWNER роль ўзгартира олади.")
    return await set_role(db, tg_id, payload.role)


@router.post("/{tg_id}/block")
async def post_block(tg_id: int, payload: BlockUserRequest, request: Request, db=Depends(get_db)):
    current = await current_user(request, db)
    if current.tg_id != settings.owner_id:
        raise PermissionError("Фақат OWNER блоклай олади.")
    return await set_blocked(db, tg_id, payload.is_blocked)


@router.post("/me/role")
async def post_my_role(payload: SetRoleRequest, request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    return await set_role(db, user.tg_id, payload.role)


@router.post("/me/phone")
async def post_my_phone(phone: str, request: Request, db=Depends(get_db)):
    user = await current_user(request, db)
    return await set_phone(db, user.tg_id, phone)


async def current_user(request: Request, db) -> UserOut:
    telegram_user = telegram_user_from_request(request)
    return await upsert_user(
        db,
        int(telegram_user["id"]),
        first_name=telegram_user.get("first_name"),
        last_name=telegram_user.get("last_name"),
        username=telegram_user.get("username"),
        language_code=telegram_user.get("language_code"),
    )
