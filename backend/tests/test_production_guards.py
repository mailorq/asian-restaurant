"""production must not boot on a placeholder-shaped secret

the guards named two historical defaults by hand, so every placeholder the repository shipped
afterwards booted a production stack without complaint. these assert the shape, not a list.
each run is a fresh process built only from what the test supplies, so it behaves the same in
the image and on a CI runner
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]
# anything the host environment could otherwise supply and so decide the outcome
ISOLATED = ("IDENTITY_JWT_PRIVATE_KEY", "IDENTITY_JWT_PRIVATE_KEY_FILE", "DJANGO_SETTINGS_MODULE")

BASE = {
    "DJANGO_PRODUCTION": "1",
    "DJANGO_DEBUG": "0",
    "DJANGO_ALLOWED_HOSTS": "app.example.com",
    "DJANGO_SECRET_KEY": "x" * 50,
    "DATABASE_URL": "postgres://u:p@db:5432/s",
    "REDIS_URL": "redis://redis:6379/1",
    "CART_REDIS_URL": "redis://redis:6379/0",
    "RABBITMQ_URL": "amqp://u:p@rabbitmq:5672/storefront",
    "IDENTITY_JWT_KID": "prod-2026-01",
}


@pytest.fixture
def signing_key(tmp_path):
    key = tmp_path / "jwt.pem"
    key.write_text("placeholder: the guard only checks the file is readable\n")
    return key


def _boot(signing_key, **overrides) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ISOLATED}
    env |= BASE | {"IDENTITY_JWT_PRIVATE_KEY_FILE": str(signing_key)} | overrides
    return subprocess.run(
        [sys.executable, "-c",
         "import os, django; os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'; django.setup()"],
        cwd=SERVICE_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


def _refused(result, reason: str) -> bool:
    return result.returncode != 0 and "ImproperlyConfigured" in result.stderr and reason in result.stderr


def test_the_baseline_starts(signing_key):
    result = _boot(signing_key)
    assert result.returncode == 0, f"the test itself must not be why a start fails:\n{result.stderr[-800:]}"


@pytest.mark.parametrize("secret", [
    "dev-insecure-change-me",
    "change-me-to-a-50-char-random-string",
    "change-me-anything",
    "CHANGE-ME-UPPERCASE",
    "dev-whatever",
])
def test_a_placeholder_shaped_secret_refuses_to_start(signing_key, secret):
    result = _boot(signing_key, DJANGO_SECRET_KEY=secret)
    assert _refused(result, "DJANGO_SECRET_KEY"), f"{secret!r}:\n{result.stderr[-800:]}"


@pytest.mark.parametrize("kid", ["dev-1", "dev-local-1", "dev-anything"])
def test_a_development_signing_kid_refuses_to_start(signing_key, kid):
    result = _boot(signing_key, IDENTITY_JWT_KID=kid)
    assert _refused(result, "development signing kid"), f"{kid!r}:\n{result.stderr[-800:]}"


def test_the_schema_is_not_served_in_production(signing_key):
    # the schema names every endpoint and its fields, the staff ones included
    env = {k: v for k, v in os.environ.items() if k not in ISOLATED}
    env |= BASE | {"IDENTITY_JWT_PRIVATE_KEY_FILE": str(signing_key)}
    result = subprocess.run(
        [sys.executable, "-c",
         "import os, django; os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'; django.setup();"
         " from config.api import api; print('docs_url', api.docs_url, 'openapi_url', api.openapi_url)"],
        cwd=SERVICE_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "docs_url None openapi_url None" in result.stdout, result.stdout
