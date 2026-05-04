from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

import app.models  # ensure all ORM models are registered before mapper use
from app.core.config import settings
from app.core.redis import init_redis, close_redis
from app.api.v1.auth import router as auth_router
from app.api.v1.chat import router as chat_router
from app.api.v1.customers import router as customers_router
from app.api.v1.home import router as home_router
from app.api.v1.profile import router as profile_router


@asynccontextmanager
async def lifespan(application: FastAPI):
    await init_redis()
    yield
    await close_redis()


app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    docs_url="/docs",
    redoc_url="/redoc",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(customers_router)
app.include_router(home_router)
app.include_router(profile_router)


@app.get("/")
async def root() -> dict:
    return {
        "app": settings.app_name,
        "version": settings.app_version,
        "status": "running",
        "docs": "/docs",
    }


@app.get("/health")
async def health_check() -> dict[str, str]:
    return {"status": "ok"}
