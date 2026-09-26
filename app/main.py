from pathlib import Path
from uuid import UUID

import hmac
import secrets

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select
from starlette.middleware.sessions import SessionMiddleware

from app import auth as auth_mod
from app.config import settings
from app.db import get_session
from app.grade import due_items, submit_answer
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
    if course is None or (
        course.user_id is not None and course.user_id != user.id
    ):
        raise HTTPException(404, "course not found")
    return course


def owns_item(session: Session, user: User, item: QuizItem) -> bool:
    chunk = session.get(Chunk, item.chunk_id)
    resource = session.get(Resource, chunk.resource_id) if chunk else None
    topic = session.get(Topic, resource.topic_id) if resource else None
    course = session.get(Course, topic.course_id) if topic else None
    return course is not None and (
        course.user_id is None or course.user_id == user.id
    )


def _login_redirect(request: Request):
    return RedirectResponse(url="/login", status_code=303)


def csrf_token(request: Request) -> str:
    """Per-session CSRF token, minted lazily and checked on every POST."""
    token = request.session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        request.session["csrf_token"] = token
    return token


@app.exception_handler(401)
async def unauthorized(request: Request, exc: HTTPException):
    if request.url.path.startswith("/api"):
        return HTMLResponse("login required", status_code=401)
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
    allowed = allowed_emails()
    if allowed and email.lower() not in allowed:
        raise HTTPException(403, "sign-in not allowed for this account")
    user = auth_mod.sign_in(session, email, tokens.get("refresh_token"))
    request.session["user_id"] = str(user.id)
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def logout(request: Request):
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
        .where((Course.user_id == user.id) | (Course.user_id.is_(None)))
        .order_by(Course.name)
    ).all()
    counts: dict[str, dict] = {}
    for c in courses:
        n_topics = session.exec(
            select(func.count()).select_from(Topic).where(Topic.course_id == c.id)
        ).one()
        n_resources = session.exec(
            select(func.count())
            .select_from(Resource)
            .join(Topic, Resource.topic_id == Topic.id)
            .where(Topic.course_id == c.id)
        ).one()
        counts[str(c.id)] = {"topics": n_topics, "resources": n_resources}
    due_count = len(due_items(session, user.id))
    return templates.TemplateResponse(
        request,
        "courses.html",
        {
            "courses": courses,
            "counts": counts,
            "user": user,
            "due_count": due_count,
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
    if resource is None or course is None or (
        course.user_id is not None and course.user_id != user.id
    ):
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
    form = await request.form()
    submitted = str(form.get("csrf_token", ""))
    expected = str(request.session.get("csrf_token", ""))
    if not expected or not hmac.compare_digest(submitted, expected):
        raise HTTPException(403, "invalid csrf token")
    answer = str(form.get("answer", ""))
    item = session.get(QuizItem, item_id)
    if item is None or not owns_item(session, user, item):
        raise HTTPException(404, "quiz item not found")
    llm = None
    if item.question_type == "short_answer":
        from app.llm import LLMClient

        llm = LLMClient(
            settings.llm_base_url, settings.llm_api_key, settings.llm_grade_model
        )
    result = submit_answer(session, user.id, item.id, answer, llm)
    queue = due_items(session, user.id)
    return templates.TemplateResponse(
        request,
        "take.html",
        {
            "item": item,
            "remaining": len(queue),
            "result": result,
            "csrf_token": csrf_token(request),
            "user": user,
            "active_page": "review",
        },
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
            "due": len(due_items(session, user.id)),
            "user": user,
            "active_page": "stats",
        },
    )
