# build context is the repository root (needs packages/ and services/)
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS base
# uv installs exactly what uv.lock names, hashes checked, into the image's own python; a lock behind pyproject.toml fails the build
COPY --from=ghcr.io/astral-sh/uv:0.12.17@sha256:10787c682e4184e4f290de1171fd4703dc63de99221f10fe1c99002ce7fa9acc /uv /bin/uv
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 UV_PROJECT_ENVIRONMENT=/usr/local UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy UV_NO_CACHE=1
# the repository layout, so the path uv.lock gives event_contracts resolves here as it does in a checkout
WORKDIR /srv/services/order_operations

COPY packages/event_contracts /srv/packages/event_contracts
COPY services/order_operations/ /srv/services/order_operations/
RUN uv sync --locked --no-dev

COPY services/order_operations/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

FROM base AS dev
RUN uv sync --locked
CMD ["python", "manage.py", "runserver", "0.0.0.0:9000"]

FROM base AS prod
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
USER 10001:10001
CMD ["gunicorn", "config.asgi:application", "-k", "config.workers.BoundedUvicornWorker", "--bind", "0.0.0.0:9000", "--no-control-socket"]
