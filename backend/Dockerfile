# syntax=docker/dockerfile:1
#
# Build from the REPOSITORY ROOT so SKILL.md is inside the build context:
#   docker build -f backend/Dockerfile -t nrb-api .
#
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install -r requirements.txt

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY backend/app ./app
COPY backend/seed.py ./
# Served verbatim by GET /skill.md, with {{PUBLIC_BASE_URL}} substituted at request time.
COPY SKILL.md ./SKILL.md
COPY references ./references

RUN useradd --create-home --uid 10001 beacon && chown -R beacon:beacon /app
USER beacon

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/health || exit 1

# Migrations run at start so a fresh database is usable immediately.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
