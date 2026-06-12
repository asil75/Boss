from typing import Any, Literal

from pydantic import BaseModel, Field


Role = Literal["shop", "courier"]


class UserOut(BaseModel):
    tg_id: int
    role: str | None
    phone: str | None
    first_name: str | None
    last_name: str | None
    username: str | None
    language_code: str | None
    is_blocked: bool


class SetRoleRequest(BaseModel):
    role: Role


class BlockUserRequest(BaseModel):
    is_blocked: bool


class OrderCreateRequest(BaseModel):
    from_address: str
    shop_contact: str
    to_address: str
    to_apt: str | None = None
    client_name: str
    client_phone: str
    price: float = Field(ge=0)


class OrderStatusRequest(BaseModel):
    action: str


class OrderOut(BaseModel):
    id: int
    shop_tg_id: int | None
    courier_tg_id: int | None
    from_address: str | None
    shop_contact: str | None
    to_address: str | None
    to_apt: str | None
    client_name: str | None
    client_phone: str | None
    price: float | None
    status: str
    log: str | None
    created_at: str | None
    return_for: int | None
    paid_to_courier: int
    paid_at: str | None


class ApiResponse(BaseModel):
    ok: bool
    message: str | None = None
    data: Any = None
