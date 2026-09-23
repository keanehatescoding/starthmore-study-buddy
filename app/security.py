"""Web hardening: security headers + in-memory POST rate limiting.

Rate limiter is per-process memory: correct for the single-uvicorn deploy
this project targets. Behind multiple workers use a shared store instead —
see DEPLOY.md.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.config import settings

DEFAULT_POST_LIMIT = (120, 60)  # 120 POSTs per 60s per IP


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        # Safe: templates contain no inline scripts or external resources.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; frame-ancestors 'none'"
        )
        if settings.session_secure_cookie:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window POST limiter keyed by client IP. 429 + Retry-After."""

    _instances: list = []

    def __init__(self, app):
        super().__init__(app)
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        RateLimitMiddleware._instances.append(self)

    @classmethod
    def reset_all(cls) -> None:
        """Clear hit history (test isolation)."""
        for instance in cls._instances:
            instance._hits.clear()

    async def dispatch(self, request, call_next):
        if request.method == "POST":
            limit, window = getattr(
                request.app.state, "rate_limit", DEFAULT_POST_LIMIT
            )
            ip = request.client.host if request.client else "unknown"
            now = time.monotonic()
            hits = self._hits[ip]
            while hits and hits[0] <= now - window:
                hits.popleft()
            if len(hits) >= limit:
                retry = int(hits[0] + window - now) + 1
                return PlainTextResponse(
                    "rate limit exceeded",
                    status_code=429,
                    headers={"Retry-After": str(retry)},
                )
            hits.append(now)
        return await call_next(request)
