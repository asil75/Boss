from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.db import init_db
from app.routers import miniapp, orders, payments, stats, users
from app.security import AuthError


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(miniapp.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(stats.router)
app.include_router(users.router)


@app.exception_handler(AuthError)
async def auth_error_handler(request: Request, exc: AuthError):
    raise HTTPException(status_code=401, detail=str(exc)) from exc


@app.exception_handler(PermissionError)
async def permission_error_handler(request: Request, exc: PermissionError):
    raise HTTPException(status_code=403, detail=str(exc)) from exc


@app.exception_handler(LookupError)
async def lookup_error_handler(request: Request, exc: LookupError):
    raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/health")
async def health():
    return {"ok": True, "app": settings.app_name}


@app.get("/api/config")
async def config():
    return {"app_name": settings.app_name, "mini_app_url": settings.mini_app_url, "owner_id": settings.owner_id, "dev_mode": settings.dev_mode}
