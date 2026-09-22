"""
what the app must be told by the proxy in front of it

production terminates TLS on an ingress and the app container never sees https, so an overwritten
x-forwarded-proto turns SECURE_SSL_REDIRECT into a redirect loop, and an overwritten
x-forwarded-for collapses every client into one rate-limit bucket
"""

import pytest
from django.core.cache import cache


@pytest.mark.django_db
def test_a_request_the_ingress_marked_https_is_not_redirected(client, settings):
    settings.SECURE_SSL_REDIRECT = True

    response = client.get("/api/menu/products", HTTP_X_FORWARDED_PROTO="https")

    assert response.status_code != 301, "an https request must not be redirected to https again"


@pytest.mark.django_db
def test_a_plain_request_is_still_redirected(client, settings):
    settings.SECURE_SSL_REDIRECT = True

    response = client.get("/api/menu/products", HTTP_X_FORWARDED_PROTO="http")

    assert response.status_code == 301


@pytest.mark.django_db
def test_the_public_keys_are_served_on_the_internal_plain_http_hop(client, settings):
    # operations fetches them from backend:8000 inside the network, where there is no tls to redirect to
    settings.SECURE_SSL_REDIRECT = True

    response = client.get("/api/auth/jwks", HTTP_HOST="backend:8000")

    assert response.status_code == 200
    assert response.json()["keys"]


def test_the_metrics_are_served_on_the_internal_plain_http_hop(client, settings):
    # prometheus scrapes backend:8000 inside the network, where there is no tls to redirect to
    settings.SECURE_SSL_REDIRECT = True

    response = client.get("/metrics", HTTP_HOST="backend:8000")

    assert response.status_code == 200
    assert b"django_http_requests" in response.content


def test_only_the_internal_endpoints_skip_the_https_redirect(client, settings):
    settings.SECURE_SSL_REDIRECT = True

    assert client.get("/api/menu/products", HTTP_HOST="backend:8000").status_code == 301
    assert client.get("/metricsx", HTTP_HOST="backend:8000").status_code == 301


@pytest.mark.django_db
def test_two_clients_behind_the_ingress_get_their_own_rate_limit_bucket(client, settings):
    settings.RATELIMIT_TRUST_XFF = True
    cache.clear()
    body = {"phone": "+79990000001", "password": "wrong-on-purpose"}

    def attempt(client_ip):
        return client.post(
            "/api/auth/login", data=body, content_type="application/json",
            HTTP_X_FORWARDED_FOR=f"{client_ip}, 127.0.0.1",
        ).status_code

    first = [attempt("203.0.113.7") for _ in range(6)]
    other = attempt("203.0.113.8")

    assert 429 in first, "the limiter never engaged, so this proves nothing"
    assert other != 429, "a second client was throttled by the first client's attempts"
