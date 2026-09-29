"""Web hardening: security headers + in-memory POST rate limiting.

Rate limiter is per-process memory: correct for the single-uvicorn deploy
this project targets. Behind multiple workers use a shared store instead —
see DEPLOY.md.
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict, deque

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse

from app.config import settings

DEFAULT_POST_LIMIT = (120, 60)  # 120 POSTs per 60s per client
MAX_TRACKED_CLIENTS = 10_000  # LRU bound on the hit table


def csp_header(nonce: str) -> str:
    # Scripts and <style> elements need the per-response nonce. Style
    # *attributes* can't carry a nonce, so style-src-attr allows them: they
    # can restyle an element but can't run code or load anything.
    return (
        "default-src 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        f"style-src 'self' 'nonce-{nonce}'; "
        "style-src-attr 'unsafe-inline'; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; "
        "frame-ancestors 'none'"
    )


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        # Templates read request.state.csp_nonce for inline <script>/<style>.
        nonce = secrets.token_urlsafe(16)
        request.state.csp_nonce = nonce
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        response.headers["Content-Security-Policy"] = csp_header(nonce)
        if settings.session_secure_cookie:
            response.headers["Strict-Transport-Security"] = (
                "max-age=31536000; includeSubDomains"
            )
        return response


class HitTable:
    """Sliding-window hit log per client key, bounded as an LRU.

    Keys whose window has emptied are dropped on access, and the least
    recently seen key is evicted past max_keys, so memory stays bounded no
    matter how many distinct clients show up.
    """

    def __init__(self, max_keys: int = MAX_TRACKED_CLIENTS):
        self.max_keys = max_keys
        self._hits: OrderedDict[str, deque[float]] = OrderedDict()

    def __len__(self) -> int:
        return len(self._hits)

    def clear(self) -> None:
        self._hits.clear()

    def hit(self, key: str, limit: int, window: float, now: float) -> float | None:
        """Record a hit; return seconds to wait instead when over the limit."""
        hits = self._hits.pop(key, None) or deque()
        while hits and hits[0] <= now - window:
            hits.popleft()
        if len(hits) >= limit:
            self._hits[key] = hits
            return hits[0] + window - now
        hits.append(now)
        self._hits[key] = hits  # re-inserted: now most recently used
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)
        return None


def hit_table(app) -> HitTable:
    """The app's hit table. Lives on app.state so it dies with the app —
    nothing accumulates at class level across reloads or test apps."""
    table = getattr(app.state, "rate_limit_hits", None)
    if table is None:
        table = app.state.rate_limit_hits = HitTable()
    return table


def client_key(request) -> str:
    """Signed-in users are limited per account, so a classroom behind one
    NAT address doesn't share a budget; anonymous POSTs fall back to the IP
    (uvicorn resolves X-Forwarded-For via --forwarded-allow-ips)."""
    user_id = request.scope.get("session", {}).get("user_id")
    if user_id:
        return f"user:{user_id}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Sliding-window POST limiter keyed by user (or IP). 429 + Retry-After."""

    async def dispatch(self, request, call_next):
        if request.method == "POST":
            limit, window = getattr(
                request.app.state, "rate_limit", DEFAULT_POST_LIMIT
            )
            wait = hit_table(request.app).hit(
                client_key(request), limit, window, time.monotonic()
            )
            if wait is not None:
                return PlainTextResponse(
                    "rate limit exceeded",
                    status_code=429,
                    headers={"Retry-After": str(int(wait) + 1)},
                )
        return await call_next(request)
