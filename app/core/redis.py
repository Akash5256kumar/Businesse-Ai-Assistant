import redis.asyncio as aioredis
from app.core.config import settings

redis_client: aioredis.Redis = None


async def init_redis():
    global redis_client
    redis_client = aioredis.from_url(
        settings.redis_url,
        encoding="utf-8",
        decode_responses=True,
    )


async def close_redis():
    global redis_client
    if redis_client:
        await redis_client.close()


def get_redis() -> aioredis.Redis:
    return redis_client