# build context is the repository root (needs packages/ and services/)
FROM python:3.13-slim@sha256:8d9d0b8bcf6506481eae4907c18f5e3e7902e629f5f6d684f9e7c32e85e3ddf0 AS base
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY packages/event_contracts /packages/event_contracts
RUN pip install --upgrade pip && pip install /packages/event_contracts

COPY services/order_operations/pyproject.toml /app/pyproject.toml
COPY services/order_operations/ /app/
RUN pip install -r requirements.txt && pip install --no-deps .

COPY services/order_operations/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh
ENTRYPOINT ["/entrypoint.sh"]

FROM base AS dev
RUN pip install ".[dev]" && pip install "/packages/event_contracts[dev]"
CMD ["python", "manage.py", "runserver", "0.0.0.0:9000"]

FROM base AS prod
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app
USER 10001:10001
CMD ["gunicorn", "config.asgi:application", "-k", "config.workers.BoundedUvicornWorker", "--bind", "0.0.0.0:9000", "--no-control-socket"]
