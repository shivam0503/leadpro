from __future__ import annotations

import uuid
from typing import Callable

from fastapi import Request, Response


async def request_id_middleware(request: Request, call_next: Callable):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    response: Response = await call_next(request)
    response.headers["x-request-id"] = request_id
    return response


# Very small in-memory rate limiter (good for single-instance demos).
# For production: move to Redis-based limiter (e.g., slowapi/redis) or API gateway.
from time import time
from collections import defaultdict, deque

_RATE_BUCKETS = defaultdict(lambda: deque())  # ip -> timestamps
_RATE_LIMIT = 120  # requests
_RATE_WINDOW = 60  # seconds


async def rate_limit_middleware(request: Request, call_next: Callable):
    ip = request.client.host if request.client else "unknown"
    now = time()
    q = _RATE_BUCKETS[ip]
    # drop old
    while q and (now - q[0]) > _RATE_WINDOW:
        q.popleft()
    if len(q) >= _RATE_LIMIT:
        return Response(content="rate_limited", status_code=429)
    q.append(now)
    return await call_next(request)
