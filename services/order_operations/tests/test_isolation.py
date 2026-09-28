import importlib
import os
from urllib.parse import urlsplit

import psycopg
import pytest


def test_storefront_orm_is_not_importable():
    for module in ("orders.models", "menu.models", "accounts.models"):
        with pytest.raises(ModuleNotFoundError):
            importlib.import_module(module)


@pytest.mark.django_db
def test_operations_db_contains_no_storefront_tables():
    from django.db import connection

    tables = set(connection.introspection.table_names())
    assert not any(t.startswith(("orders_", "menu_", "accounts_", "cart_")) for t in tables)
    assert any(t.startswith("operations_") for t in tables)


def test_operations_credentials_cannot_reach_storefront_db():
    # the operations runtime role against the storefront server, which must itself refuse the login
    # with 28P01. libpq hands the code on only as the server's message, so the message is matched
    # and a network failure fails the test
    dsn = os.environ.get("STOREFRONT_DSN_FOR_ISOLATION")
    if not dsn:
        pytest.skip("STOREFRONT_DSN_FOR_ISOLATION not configured")
    with pytest.raises(psycopg.OperationalError) as refused:
        psycopg.connect(dsn, connect_timeout=5)
    refusal = f'FATAL:  password authentication failed for user "{urlsplit(dsn).username}"'
    assert refusal in str(refused.value)
