"""Security tests: headers on every response, POST rate limiting."""

from app.main import app
from app.security import RateLimitMiddleware


def test_security_headers_present(testapp):
    client = testapp["client"]
    r = client.get("/login")
    assert r.status_code == 200
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "same-origin"
    assert "camera=()" in r.headers["Permissions-Policy"]


def test_post_rate_limit_429s_then_recovers(testapp):
    client = testapp["client"]
    RateLimitMiddleware.reset_all()
    app.state.rate_limit = (2, 60)
    try:
        url = "/review/00000000-0000-0000-0000-000000000000/answer"
        assert client.post(url, data={"answer": "x"}).status_code == 403  # CSRF, still counts
        assert client.post(url, data={"answer": "x"}).status_code == 403
        r = client.post(url, data={"answer": "x"})
        assert r.status_code == 429
        assert "Retry-After" in r.headers
    finally:
        del app.state.rate_limit
        RateLimitMiddleware.reset_all()
    # default budget restored
    url = "/review/00000000-0000-0000-0000-000000000000/answer"
    assert client.post(url, data={"answer": "x"}).status_code == 403
