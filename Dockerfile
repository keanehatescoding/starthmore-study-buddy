FROM python:3.12-slim

WORKDIR /srv
ENV PYTHONUNBUFFERED=1

# Override at build time if your network to PyPI is slow.
ARG PIP_INDEX_URL=https://pypi.org/simple

COPY pyproject.toml alembic.ini ./
COPY app/ app/
COPY alembic/ alembic/
COPY templates/ templates/

RUN pip install --no-cache-dir --index-url $PIP_INDEX_URL .

CMD alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
