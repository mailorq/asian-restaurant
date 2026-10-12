import pytest

pytestmark = pytest.mark.django_db


def test_the_debug_page_for_an_unknown_host_shows_no_credentials(client, settings):
    settings.DEBUG = True
    settings.ALLOWED_HOSTS = ["localhost"]
    settings.RABBITMQ_URL = "amqp://storefront:mq-secret-1@rabbitmq:5672/storefront"
    settings.CART_REDIS_URL = "redis://:redis-secret-2@redis:6379/0"

    # a request by the machine's address, as from the local network, reaches the debug page
    response = client.get("/api/menu/products", HTTP_HOST="192.168.1.20:8000")
    body = response.content.decode()

    assert response.status_code == 400
    assert "DisallowedHost" in body
    assert "mq-secret-1" not in body
    assert "redis-secret-2" not in body
    assert "amqp://********************@rabbitmq:5672/storefront" in body
