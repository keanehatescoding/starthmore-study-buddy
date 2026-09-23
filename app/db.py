from sqlalchemy.ext.asyncio import create_async_engine  # noqa: F401 (placeholder)
from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

engine = create_engine(settings.database_url, echo=False)


def get_engine():
    return engine


def get_session():
    with Session(engine) as session:
        yield session


def create_all() -> None:
    """Dev-only helper; migrations (Alembic) are canonical."""
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
