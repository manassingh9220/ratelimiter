"""
HTTP layer for the rate limiter.

    uvicorn app:app --reload

One endpoint answers one question: should this request be allowed right now?

    POST /check
    { "key": "user-123", "limit": 100, "window": 60, "algorithm": "token_bucket" }

    200 -> allowed
    429 -> denied, with Retry-After

Response headers follow the conventional X-RateLimit-* names rather than
anything invented here, because clients already parse those.
"""

import math
import os
import time
from typing import Literal

import redis
from fastapi import FastAPI, Response, status
from pydantic import BaseModel, Field

from rate_limiter import STRATEGIES

REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

app = FastAPI(
    title="Rate Limiter",
    description="Four rate limiting algorithms behind one API.",
    version="0.1.0",
)

pool = redis.ConnectionPool.from_url(REDIS_URL, decode_responses=True)


def get_redis() -> redis.Redis:
    return redis.Redis(connection_pool=pool)


# ------------------------------------------------------------- schemas ----

Algorithm = Literal["fixed", "sliding_log", "sliding_counter", "token_bucket"]


class CheckRequest(BaseModel):
    key: str = Field(..., min_length=1, max_length=256,
                     description="Opaque client identifier — user id, API key, IP.")
    limit: int = Field(..., gt=0, le=1_000_000,
                       description="Requests permitted per window.")
    window: int = Field(..., gt=0, le=86_400,
                        description="Window length in seconds.")
    algorithm: Algorithm = "token_bucket"


class CheckResponse(BaseModel):
    allowed: bool
    remaining: int
    limit: int
    reset_at: int
    retry_after: int | None = None
    algorithm: str


# ------------------------------------------------------------- helpers ----

def reset_at(window: int, now: float) -> int:
    """
    When the next quota becomes available, as a unix timestamp.

    This is an UPPER BOUND, not an exact answer. Fixed window and the sliding
    counter genuinely reset at the next boundary, but the log frees its oldest
    entry at an arbitrary moment and the token bucket accrues continuously —
    both of those recover sooner than this reports.

    Computing it exactly would mean returning the oldest score from the sorted
    set and the fractional token count from the bucket. Deferred; a conservative
    Retry-After is safe, just occasionally pessimistic.
    """
    return int((int(now) // window + 1) * window)


# ------------------------------------------------------------ endpoints ----

@app.post("/check", response_model=CheckResponse)
def check(req: CheckRequest, response: Response):
    now = time.time()
    allow_fn = STRATEGIES[req.algorithm]

    allowed, remaining = allow_fn(get_redis(), req.key, req.limit, req.window, now=now)

    resets = reset_at(req.window, now)
    retry_after = None if allowed else max(1, math.ceil(resets - now))

    response.headers["X-RateLimit-Limit"] = str(req.limit)
    response.headers["X-RateLimit-Remaining"] = str(remaining)
    response.headers["X-RateLimit-Reset"] = str(resets)

    if not allowed:
        response.status_code = status.HTTP_429_TOO_MANY_REQUESTS
        response.headers["Retry-After"] = str(retry_after)

    return CheckResponse(
        allowed=allowed,
        remaining=remaining,
        limit=req.limit,
        reset_at=resets,
        retry_after=retry_after,
        algorithm=req.algorithm,
    )


@app.get("/health")
def health(response: Response):
    try:
        get_redis().ping()
        return {"status": "ok", "redis": "up"}
    except redis.RedisError as exc:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "degraded", "redis": "down", "error": str(exc)}


@app.get("/algorithms")
def algorithms():
    """What this service supports, and when each is the right choice."""
    return {
        "token_bucket": {
            "memory": "constant (84 B/key)",
            "bursts": "permitted by design",
            "use_when": "general API limiting; clients idle then burst",
        },
        "sliding_counter": {
            "memory": "constant (88 B/key)",
            "bursts": "suppressed",
            "use_when": "bursts must be smoothed rather than banked",
        },
        "sliding_log": {
            "memory": "proportional to limit (~48 B/request)",
            "bursts": "suppressed",
            "use_when": "the limit must be exact — billing, quotas",
        },
        "fixed": {
            "memory": "constant (48 B/key)",
            "bursts": "2x the limit at window boundaries",
            "use_when": "steady traffic; cheapest option",
        },
    }