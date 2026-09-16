from contextlib import asynccontextmanager
from typing import Any, Dict

from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker
from app.core.config import settings


def _build_engine_kwargs() -> Dict[str, Any]:
    """组装引擎参数

    批量审计下每个 AgentTask 的会话会包住整个审计生命周期（最长 7200s）并全程
    独占一个连接，默认的 pool_size=5 + max_overflow=10（共 15）在并发 10 以上时
    会被耗尽，连带把登录、项目列表、SSE 等普通接口一起拖挂，因此必须显式配置。

    SQLite（含内存库）用的是 StaticPool/NullPool，不接受 QueuePool 参数。
    """
    kwargs: Dict[str, Any] = {"echo": False, "future": True}

    if settings.DATABASE_URL.startswith("sqlite"):
        return kwargs

    kwargs.update(
        pool_size=settings.DB_POOL_SIZE,
        max_overflow=settings.DB_MAX_OVERFLOW,
        pool_timeout=settings.DB_POOL_TIMEOUT,
        pool_recycle=settings.DB_POOL_RECYCLE,
        # 取连接前先 ping，避免 PG 侧超时断连后拿到死连接
        pool_pre_ping=True,
    )
    return kwargs


engine = create_async_engine(settings.DATABASE_URL, **_build_engine_kwargs())

AsyncSessionLocal = sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)

async def get_db():
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()


@asynccontextmanager
async def async_session_factory():
    """Async context manager for creating database sessions"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
        finally:
            await session.close()






