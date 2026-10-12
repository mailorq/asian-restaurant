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
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from config.published_keys import fingerprint, is_published

SERVICE_ROOT = Path(__file__).resolve().parents[1]
# anything the host environment could otherwise supply and so decide the outcome
ISOLATED = ("IDENTITY_JWT_PRIVATE_KEY", "IDENTITY_JWT_PRIVATE_KEY_FILE", "DJANGO_SETTINGS_MODULE", "SMS_BACKEND")

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


# the public part of ops/identity/jwt_dev_private_key.pem, which the history of dev still holds
PUBLISHED_DEV_KEY = rsa.RSAPublicNumbers(65537, int(
    "bd0b47e5b1ae8d78a221bd6f54ae607afde3fe74ac8f4770f67db5829d410e3b3d2f7c4e40df53d35689ce2ca10a2bfe"
    "399a83a721a9454e30f2ae6ec57e791b159ea25df60ef47a865227eb0a6c544a10b5baa494b4beaadb04f13617505e04"
    "92a07d5e39392fec915c4771ddef9805e39b294fdd788cf58987e6d4436e6f4a5629b51fd2a577eee6d17b9899e12e56"
    "ec83e37d4d89b16f761dec123eef3238d57750483317cceb64a75cde715bccbe2d63277e90969c151dfcecdba1cd4ced"
    "f8f72f0e9a09922021e80e8284cc4753a0625b773f71756271c3647d09d9013e3302458753df112593a659dacf32038c"
    "134670a08440e6ae96811f13a0eaa2af", 16,
)).public_key()


@pytest.fixture
def signing_key(tmp_path):
    key = tmp_path / "jwt.pem"
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    key.write_bytes(private.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    return key


def _public(path: Path):
    return serialization.load_pem_private_key(path.read_bytes(), password=None).public_key()


def _boot(signing_key, published: str = "", **overrides) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in ISOLATED}
    env |= BASE | {"IDENTITY_JWT_PRIVATE_KEY_FILE": str(signing_key)} | overrides
    # a test may name one more key as published, as if its private part had leaked
    extra = f"import config.published_keys as k; k.PUBLISHED = k.PUBLISHED | {{'{published}'}}; " if published else ""
    return subprocess.run(
        [sys.executable, "-c",
         extra + "import os, django; os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'; django.setup()"],
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


def test_an_sms_backend_that_logs_codes_refuses_to_start(signing_key):
    result = _boot(signing_key, SMS_BACKEND="accounts.sms.ConsoleSender")
    assert _refused(result, "SMS_BACKEND"), result.stderr[-800:]


def test_production_sends_no_sms_until_a_provider_is_chosen(signing_key):
    env = {k: v for k, v in os.environ.items() if k not in ISOLATED}
    env |= BASE | {"IDENTITY_JWT_PRIVATE_KEY_FILE": str(signing_key)}
    result = subprocess.run(
        [sys.executable, "-c",
         "import os, django; os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'; django.setup();"
         " from django.conf import settings; print('sms', settings.SMS_BACKEND)"],
        cwd=SERVICE_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "sms accounts.sms.DisabledSender" in result.stdout, result.stdout


def test_the_published_dev_key_is_known_by_its_public_part():
    assert is_published(PUBLISHED_DEV_KEY)
    assert not is_published(rsa.generate_private_key(public_exponent=65537, key_size=2048).public_key())


def test_a_published_signing_key_refuses_to_start(signing_key):
    result = _boot(signing_key, published=fingerprint(_public(signing_key)))
    assert _refused(result, "signing key is published"), result.stderr[-800:]


def test_a_published_previous_key_refuses_to_start(signing_key):
    previous = PUBLISHED_DEV_KEY.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ).decode()
    result = _boot(signing_key, IDENTITY_JWT_PREVIOUS_PUBLIC_KEY=previous, IDENTITY_JWT_PREVIOUS_KID="prod-2025-12")
    assert _refused(result, "previous signing key is published"), result.stderr[-800:]


def test_a_key_file_that_holds_no_key_refuses_to_start(signing_key):
    signing_key.write_text("placeholder")
    result = _boot(signing_key)
    assert _refused(result, "does not hold a private key"), result.stderr[-800:]
