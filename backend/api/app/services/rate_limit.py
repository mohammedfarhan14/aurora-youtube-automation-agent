import hashlib
import hmac
from dataclasses import dataclass

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import get_settings

_RATE_LIMIT_SCRIPT = """
local allowed = 1
local retry_after = 0
local count = #KEYS
for i = 1, count do
    local current = tonumber(redis.call('GET', KEYS[i]) or '0')
    if current >= tonumber(ARGV[i]) then
        allowed = 0
        local ttl = redis.call('TTL', KEYS[i])
        if ttl > retry_after then retry_after = ttl end
    end
end
if allowed == 1 then
    for i = 1, count do
        local current = redis.call('INCR', KEYS[i])
        if current == 1 then
            redis.call('EXPIRE', KEYS[i], tonumber(ARGV[count + i]))
        end
    end
end
return {allowed, retry_after}
"""


@dataclass(frozen=True)
class RateLimit:
    scope: str
    limit: int
    window_seconds: int
    identity: str


def get_redis_client(redis_url: str) -> Redis:
    return Redis.from_url(redis_url, encoding="utf-8", decode_responses=True)


def _key(limit: RateLimit, secret: bytes) -> str:
    digest = hmac.new(
        secret,
        limit.identity.strip().lower().encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return f"aurora:rate-limit:v1:{limit.scope}:{digest}"


async def enforce_rate_limits(request: Request, *limits: RateLimit) -> None:
    """Apply multiple atomic fixed-window limits; fail closed if Redis is unavailable."""
    redis = getattr(request.app.state, "redis", None)
    owns_client = redis is None
    if owns_client:
        redis = get_redis_client(get_settings().redis_url)

    settings = get_settings()
    identity_secret = (
        settings.rate_limit_identity_secret.get_secret_value().encode("utf-8")
        if settings.rate_limit_identity_secret is not None
        else b"aurora-development-rate-limit-key"
    )
    keys = [_key(limit, identity_secret) for limit in limits]
    arguments = [limit.limit for limit in limits]
    arguments.extend(limit.window_seconds for limit in limits)

    try:
        allowed, retry_after = await redis.eval(
            _RATE_LIMIT_SCRIPT,
            len(keys),
            *keys,
            *arguments,
        )
    except RedisError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Rate limiting service is temporarily unavailable.",
            headers={"Retry-After": "5"},
        ) from exc
    finally:
        if owns_client:
            await redis.aclose()

    if int(allowed) == 0:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many requests. Please try again later.",
            headers={"Retry-After": str(max(1, int(retry_after)))},
        )


def client_ip(request: Request) -> str:
    # Deliberately do not trust X-Forwarded-For here. Deployments must configure
    # Uvicorn's trusted proxy handling for known proxy addresses.
    return request.client.host if request.client is not None else "unknown"


async def limit_login(request: Request, email: str) -> None:
    settings = get_settings()
    ip = client_ip(request)
    await enforce_rate_limits(
        request,
        RateLimit("login-ip", settings.auth_login_ip_limit, 60, ip),
        RateLimit("login-account", settings.auth_login_account_limit, 60, email),
    )


async def limit_registration(request: Request) -> None:
    await enforce_rate_limits(
        request,
        RateLimit(
            "register-ip",
            get_settings().auth_registration_ip_limit,
            3600,
            client_ip(request),
        ),
    )


async def limit_verification_resend(request: Request) -> None:
    await enforce_rate_limits(
        request,
        RateLimit(
            "verification-resend-ip",
            get_settings().auth_verification_resend_ip_limit,
            3600,
            client_ip(request),
        ),
    )
