from pathlib import Path
from uuid import UUID

import hmac
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool
from sqlmodel import Session, func, select
from starlette.middleware.sessions import SessionMiddleware

from app import auth as auth_mod
from app.config import settings
from app.db import get_session
from app.grade import InvalidAnswer, due_count, due_items, submit_answer, user_owns_item
from app.llm import LLMClient, LLMError
from app.models import Assignment, Chunk, Course, QuizItem, Resource, Topic, User
from app.security import RateLimitMiddleware, SecurityHeadersMiddleware
from app.stats import compute_stats

app = FastAPI(title="Strathmore Study Buddy")
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(RateLimitMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    same_site="lax",
    https_only=settings.session_secure_cookie,
)
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


def allowed_emails() -> set[str]:
    return {
        e.strip().lower()
        for e in settings.allowed_emails.split(",")
        if e.strip()
    }


def email_allowed(email: str) -> bool:
    """Exact address or "@domain" entry match; an empty allowlist admits anyone."""
    allowed = allowed_emails()
    if not allowed:
        return True
    domain = "@" + email.rpartition("@")[2]
    return email in allowed or domain in allowed


def current_user(
    request: Request, session: Session = Depends(get_session)
) -> User:
    user_id = request.session.get("user_id")
    user = session.get(User, user_id) if user_id else None
    if user is None:
        raise HTTPException(status_code=401, detail="login required")
    return user


def owned_course(session: Session, user: User, course_id: UUID) -> Course:
    course = session.get(Course, course_id)
    if course is None or course.user_id != user.id:
        raise HTTPException(404, "course not found")
    return course


def _login_redirect(request: Request):
    return RedirectResponse(url="/login", status_code=303)


def csrf_token(request: Request) -> str:
    """Per-session CSRF token, minted lazily and checked on every POST."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


templates.env.globals["session_csrf_token"] = csrf_token


async def checked_form(request: Request):
    """Parsed POST form, after verifying its CSRF token."""
    form = await request.form()
    submitted = str(form.get("csrf_token", ""))
    expected = str(request.session.get("csrf_token", ""))
    if not expected or not hmac.compare_digest(submitted, expected):
        raise HTTPException(403, "invalid csrf token")
    return form


def _flash(request: Request, kind: str, text: str) -> None:
    request.session["flash"] = {"kind": kind, "text": text}


@app.exception_handler(401)
async def unauthorized(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return _login_redirect(request)


@app.get("/health")
def health(session: Session = Depends(get_session)):
    # Platform restart decisions use this: verify Postgres is actually reachable.
    try:
        session.exec(select(User.id)).first()
    except Exception:
        return JSONResponse(
            {"status": "degraded", "db": "unreachable"}, status_code=503
        )
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if request.session.get("user_id"):
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/login/google")
def login_google(request: Request):
    state = auth_mod.new_state()
    request.session["oauth_state"] = state
    redirect_uri = str(request.url_for("auth_callback"))
    return RedirectResponse(
        auth_mod.login_url(settings.google_client_id, redirect_uri, state),
        status_code=303,
    )


@app.get("/auth/callback")
def auth_callback(
    request: Request, session: Session = Depends(get_session),
    code: str = "", state: str = "",
):
    if not code or state != request.session.pop("oauth_state", None):
        raise HTTPException(400, "invalid oauth state")
    redirect_uri = str(request.url_for("auth_callback"))
    tokens = auth_mod.exchange_code(
        settings.google_client_id, settings.google_client_secret, code, redirect_uri
    )
    email = auth_mod.fetch_email(tokens["access_token"])
    if not email_allowed(email):
        raise HTTPException(403, "sign-in not allowed for this account")
    user = auth_mod.sign_in(session, email, tokens.get("refresh_token"))
    request.session["user_id"] = str(user.id)
    return RedirectResponse(url="/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    await checked_form(request)
    request.session.clear()
    return RedirectResponse(url="/login", status_code=303)


@app.get("/", response_class=HTMLResponse)
def course_list(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    courses = session.exec(
        select(Course)
        .where(Course.user_id == user.id)
        .order_by(Course.name)
    ).all()
    ids = [c.id for c in courses]
    n_topics = dict(session.exec(
        select(Topic.course_id, func.count())
        .where(Topic.course_id.in_(ids))
        .group_by(Topic.course_id)
    ).all())
    n_resources = dict(session.exec(
        select(Topic.course_id, func.count(Resource.id))
        .join(Resource, Resource.topic_id == Topic.id)
        .where(Topic.course_id.in_(ids))
        .group_by(Topic.course_id)
    ).all())
    counts = {
        str(c.id): {"topics": n_topics.get(c.id, 0), "resources": n_resources.get(c.id, 0)}
        for c in courses
    }
    return templates.TemplateResponse(
        request,
        "courses.html",
        {
            "courses": courses,
            "counts": counts,
            "user": user,
            "due_count": due_count(session, user.id),
            "active_page": "courses",
        },
    )


@app.get("/courses/{course_id}", response_class=HTMLResponse)
def course_detail(
    course_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    course = owned_course(session, user, course_id)
    topics = session.exec(
        select(Topic).where(Topic.course_id == course.id).order_by(Topic.order)
    ).all()
    resources_by_topic: dict[str, list] = {}
    for t in topics:
        resources_by_topic[str(t.id)] = session.exec(
            select(Resource).where(Resource.topic_id == t.id).order_by(Resource.title)
        ).all()
    assignments = session.exec(
        select(Assignment)
        .where(Assignment.course_id == course.id)
        .order_by(Assignment.due_date)
    ).all()
    return templates.TemplateResponse(
        request,
        "course.html",
        {
            "course": course,
            "topics": topics,
            "resources_by_topic": resources_by_topic,
            "assignments": assignments,
            "user": user,
            "active_page": "courses",
        },
    )


@app.get("/resources/{resource_id}", response_class=HTMLResponse)
def resource_detail(
    resource_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    resource = session.get(Resource, resource_id)
    topic = session.get(Topic, resource.topic_id) if resource else None
    course = session.get(Course, topic.course_id) if topic else None
    if resource is None or course is None or course.user_id != user.id:
        raise HTTPException(404, "resource not found")
    chunks = session.exec(
        select(Chunk).where(Chunk.resource_id == resource.id).order_by(Chunk.order)
    ).all()
    return templates.TemplateResponse(
        request,
        "resource.html",
        {
            "resource": resource,
            "topic": topic,
            "course": course,
            "chunks": chunks,
            "user": user,
            "active_page": "courses",
        },
    )


@app.get("/review", response_class=HTMLResponse)
def review_queue(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    return templates.TemplateResponse(
        request,
        "review.html",
        {
            "items": due_items(session, user.id),
            "user": user,
            "active_page": "review",
        },
    )


@app.get("/review/take", response_class=HTMLResponse)
def review_take(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    queue = due_items(session, user.id)
    if not queue:
        return templates.TemplateResponse(
            request, "review.html", {"items": [], "user": user, "active_page": "review"}
        )
    return templates.TemplateResponse(
        request,
        "take.html",
        {
            "item": queue[0],
            "remaining": len(queue) - 1,
            "result": None,
            "csrf_token": csrf_token(request),
            "user": user,
            "active_page": "review",
        },
    )


@app.post("/review/{item_id}/answer", response_class=HTMLResponse)
async def review_answer(
    item_id: UUID,
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = await checked_form(request)
    answer = str(form.get("answer", ""))
    item = session.get(QuizItem, item_id)
    if item is None or not user_owns_item(session, user.id, item_id):
        raise HTTPException(404, "quiz item not found")

    def grade():
        llm = None
        if item.question_type == "short_answer":
            llm = LLMClient(
                settings.llm_base_url, settings.llm_api_key, settings.llm_grade_model
            )
        return submit_answer(session, user.id, item.id, answer, llm)

    result, error, status = None, None, 200
    try:
        result = await run_in_threadpool(grade)
    except InvalidAnswer:
        error, status = "Pick one of the listed options.", 400
    except LLMError:
        # Nothing was recorded; hand the answer back so it isn't lost.
        error, status = "The grader is unavailable right now. Your answer is below — try again shortly.", 503
    return templates.TemplateResponse(
        request,
        "take.html",
        {
            "item": item,
            "remaining": max(0, due_count(session, user.id) - (1 if error else 0)),
            "result": result,
            "error": error,
            "answer": answer if error else "",
            "csrf_token": csrf_token(request),
            "user": user,
            "active_page": "review",
        },
        status_code=status,
    )


@app.get("/stats", response_class=HTMLResponse)
def stats_page(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "stats": compute_stats(session, user.id),
            "due": due_count(session, user.id),
            "user": user,
            "active_page": "stats",
        },
    )


@app.get("/settings/moodle", response_class=HTMLResponse)
def moodle_settings(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    from app.moodle_tokens import decrypt_token, token_for

    own = decrypt_token(user.moodle_token) is not None
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "connected": own,
            "shared": not own and token_for(user) is not None,
            "stale": bool(user.moodle_token) and not own,
            "moodle_url": settings.moodle_base_url,
            "flash": request.session.pop("flash", None),
            "csrf_token": csrf_token(request),
            "user": user,
            "due_count": due_count(session, user.id),
            "active_page": "settings",
        },
    )


def _connect_moodle(session: Session, user: User, token: str) -> str:
    """Verify `token`, store it encrypted, queue a first sync; returns the Moodle name."""
    from app.jobs import enqueue
    from app.moodle_tokens import encrypt_token, verify_token

    info = verify_token(settings.moodle_base_url, token)
    user.moodle_token = encrypt_token(token)
    session.add(user)
    session.commit()
    enqueue(session, "sync", {
        "source": "moodle", "course_id": None, "user_email": user.email,
    })
    return str(info.get("fullname") or info.get("username") or "your account")


async def _connect_and_redirect(request, session, user, get_token) -> RedirectResponse:
    from app.moodle import MoodleError

    try:
        token = await run_in_threadpool(get_token)
        name = await run_in_threadpool(_connect_moodle, session, user, token)
    except MoodleError as e:
        _flash(request, "error", f"Moodle said: {e}")
    else:
        _flash(request, "success",
               f"Connected as {name}. Your courses will sync in the next few minutes.")
    return RedirectResponse("/settings/moodle", status_code=303)


@app.post("/settings/moodle/login")
async def moodle_login(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    from app.moodle_tokens import fetch_token

    form = await checked_form(request)
    username = str(form.get("username", "")).strip()
    password = str(form.get("password", ""))
    if not username or not password:
        _flash(request, "error", "Enter your Moodle username and password.")
        return RedirectResponse("/settings/moodle", status_code=303)
    return await _connect_and_redirect(
        request, session, user,
        lambda: fetch_token(settings.moodle_base_url, username, password),
    )


@app.post("/settings/moodle/token")
async def moodle_token(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    form = await checked_form(request)
    token = str(form.get("token", "")).strip()
    if not token:
        _flash(request, "error", "Paste your Moodle mobile web service key.")
        return RedirectResponse("/settings/moodle", status_code=303)
    return await _connect_and_redirect(request, session, user, lambda: token)


@app.post("/settings/moodle/disconnect")
async def moodle_disconnect(
    request: Request,
    session: Session = Depends(get_session),
    user: User = Depends(current_user),
):
    await checked_form(request)
    user.moodle_token = None
    session.add(user)
    session.commit()
    _flash(request, "success",
           "Disconnected. Already-synced courses stay; new material won't sync.")
    return RedirectResponse("/settings/moodle", status_code=303)
