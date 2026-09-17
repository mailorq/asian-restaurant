"""production must not boot on a placeholder-shaped secret

each run is a fresh process built only from what the test supplies, so it behaves the same in
the image and on a CI runner
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

SERVICE_ROOT = Path(__file__).resolve().parents[1]

BASE = {
    "OPERATIONS_PRODUCTION": "1",
    "OPERATIONS_DEBUG": "0",
    "OPERATIONS_ALLOWED_HOSTS": "ops.example.com",
    "OPERATIONS_SECRET_KEY": "y" * 50,
    "OPERATIONS_DATABASE_URL": "postgres://u:p@operations-db:5432/operations",
}


def _boot(**overrides) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    env |= BASE | overrides
    return subprocess.run(
        [sys.executable, "-c",
         "import os, django; os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'; django.setup()"],
        cwd=SERVICE_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )


def test_the_baseline_starts():
    result = _boot()
    assert result.returncode == 0, f"the test itself must not be why a start fails:\n{result.stderr[-800:]}"


@pytest.mark.parametrize("secret", [
    "ops-dev-insecure", "change-me-ops-secret", "CHANGE-ME-OPS", "dev-anything",
])
def test_a_placeholder_shaped_secret_refuses_to_start(secret):
    result = _boot(OPERATIONS_SECRET_KEY=secret)
    assert result.returncode != 0
    assert "ImproperlyConfigured" in result.stderr and "OPERATIONS_SECRET_KEY" in result.stderr, (
        f"{secret!r} failed for another reason:\n{result.stderr[-800:]}"
    )


def test_the_schema_is_not_served_in_production():
    # the schema names every staff endpoint and its fields
    env = {k: v for k, v in os.environ.items() if k != "DJANGO_SETTINGS_MODULE"}
    env |= BASE
    result = subprocess.run(
        [sys.executable, "-c",
         "import os, django; os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'; django.setup();"
         " from config.api import api; print('docs_url', api.docs_url, 'openapi_url', api.openapi_url)"],
        cwd=SERVICE_ROOT, env=env, capture_output=True, text=True, timeout=60,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "docs_url None openapi_url None" in result.stdout, result.stdout
