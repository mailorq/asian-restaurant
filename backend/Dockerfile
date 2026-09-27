# syntax=docker/dockerfile:1@sha256:ecfaec9ed6d810b56388c508f4121597bfbba70d41a6dfeee4d8cad5f295fc32

# base: deps + source
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS base

# uv installs exactly what uv.lock names, hashes checked, into the image's own python; a lock behind pyproject.toml fails the build
COPY --from=ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc /uv /bin/uv
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/usr/local \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev curl \
    && rm -rf /var/lib/apt/lists/*

# build context is the repository root (needs packages/ and backend/)
COPY packages/event_contracts /packages/event_contracts
COPY backend/ /app/
RUN uv sync --locked --no-dev

RUN chmod +x entrypoint.sh
ENTRYPOINT ["./entrypoint.sh"]

# dev: adds tooling; command comes from the compose override
FROM base AS dev
RUN uv sync --locked
CMD ["python", "manage.py", "runserver", "0.0.0.0:8000"]

# prod: gunicorn + uvicorn worker (asgi) behind nginx — async endpoints run concurrently
FROM base AS prod
# static files are collected at build time and stay read-only for the runtime user
RUN python manage.py collectstatic --noinput \
    && groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
USER 10001:10001
EXPOSE 8000
CMD ["gunicorn", "config.asgi:application", \
     "-k", "config.workers.BoundedUvicornWorker", \
     "--bind", "0.0.0.0:8000", \
     "--workers", "4", \
     "--timeout", "60", \
     "--no-control-socket", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]
