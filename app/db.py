from sqlmodel import Session, SQLModel, create_engine

from app.config import settings

# pool_pre_ping: a Postgres restart/idle-kill would otherwise hand out dead
# connections and 500 every request until the app restarts.
engine = create_engine(settings.database_url, echo=False, pool_pre_ping=True)


def get_engine():
    return engine


def get_session():
    with Session(engine) as session:
        yield session


def create_all() -> None:
    """Dev-only helper; migrations (Alembic) are canonical."""
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)
