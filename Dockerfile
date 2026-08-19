FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# uv is the project's package manager; install it first so the dependency layer caches.
RUN pip install --no-cache-dir uv

# The source has to be present before the install: the hatchling build backend builds
# a wheel from `app/`, so installing "." against a context that only holds
# pyproject.toml fails at build time.
COPY pyproject.toml README.md alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY docs ./docs

RUN uv pip install --system --no-cache .

# Runs unprivileged. The uploads volume is mounted here.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /srv/uploads \
    && chown -R appuser:appuser /srv/uploads
USER appuser

ENV FILE_STORAGE_DIR=/srv/uploads

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
