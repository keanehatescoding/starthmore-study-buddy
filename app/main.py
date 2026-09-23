from pathlib import Path
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app.db import get_session
from app.grade import due_items, submit_answer
from app.models import Assignment, Chunk, Course, QuizItem, Resource, Topic
from app.stats import compute_stats, get_or_create_user

app = FastAPI(title="Strathmore Study Buddy")
templates = Jinja2Templates(directory=str(Path(__file__).parent.parent / "templates"))


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def course_list(request: Request, session: Session = Depends(get_session)):
    courses = session.exec(select(Course).order_by(Course.name)).all()
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
    return templates.TemplateResponse(
        request, "courses.html", {"courses": courses, "counts": counts}
    )


@app.get("/courses/{course_id}", response_class=HTMLResponse)
def course_detail(
    course_id: UUID, request: Request, session: Session = Depends(get_session)
):
    course = session.get(Course, course_id)
    if course is None:
        raise HTTPException(404, "course not found")
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
        },
    )


@app.get("/resources/{resource_id}", response_class=HTMLResponse)
def resource_detail(
    resource_id: UUID, request: Request, session: Session = Depends(get_session)
):
    resource = session.get(Resource, resource_id)
    if resource is None:
        raise HTTPException(404, "resource not found")
    topic = session.get(Topic, resource.topic_id)
    course = session.get(Course, topic.course_id) if topic else None
    chunks = session.exec(
        select(Chunk).where(Chunk.resource_id == resource.id).order_by(Chunk.order)
    ).all()
    return templates.TemplateResponse(
        request,
        "resource.html",
        {"resource": resource, "topic": topic, "course": course, "chunks": chunks},
    )


def _user(session: Session):
    return get_or_create_user(session)


@app.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, session: Session = Depends(get_session)):
    user = _user(session)
    return templates.TemplateResponse(
        request, "review.html", {"items": due_items(session, user.id)}
    )


@app.get("/review/take", response_class=HTMLResponse)
def review_take(request: Request, session: Session = Depends(get_session)):
    user = _user(session)
    queue = due_items(session, user.id)
    if not queue:
        return templates.TemplateResponse(request, "review.html", {"items": []})
    return templates.TemplateResponse(
        request, "take.html",
        {"item": queue[0], "remaining": len(queue) - 1, "result": None},
    )


@app.post("/review/{item_id}/answer", response_class=HTMLResponse)
async def review_answer(
    item_id: UUID, request: Request, session: Session = Depends(get_session)
):
    user = _user(session)
    form = await request.form()
    answer = str(form.get("answer", ""))
    item = session.get(QuizItem, item_id)
    if item is None:
        raise HTTPException(404, "quiz item not found")
    llm = None
    if item.question_type == "short_answer":
        from app.config import settings
        from app.llm import LLMClient

        llm = LLMClient(
            settings.llm_base_url, settings.llm_api_key, settings.llm_grade_model
        )
    result = submit_answer(session, user.id, item.id, answer, llm)
    queue = due_items(session, user.id)
    return templates.TemplateResponse(
        request, "take.html",
        {"item": item, "remaining": len(queue), "result": result},
    )


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request, session: Session = Depends(get_session)):
    user = _user(session)
    return templates.TemplateResponse(
        request, "stats.html",
        {"stats": compute_stats(session, user.id),
         "due": len(due_items(session, user.id))},
    )
