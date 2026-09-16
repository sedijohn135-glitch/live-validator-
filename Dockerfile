FROM python:3.12-slim

# uv installs the locked dependency set; tzdata comes from pyproject (slim images lack the tz database).
COPY --from=ghcr.io/astral-sh/uv:0.9.29 /uv /usr/local/bin/uv

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    PYTHONUNBUFFERED=1

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app ./app
COPY docs ./docs
COPY README.md CLAUDE.md ./

# Railway mounts volumes as root; a non-root user cannot write to them (failure mode D5).
EXPOSE 8080

# Shell form so $PORT expands (failure mode D1).
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8080} --proxy-headers --forwarded-allow-ips '*' --workers 1
