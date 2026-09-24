import pytest
import redis as redis_sync
from django.contrib.auth import get_user_model

from cart import service
from menu.models import Product


@pytest.fixture(scope="session")
def _identity_key_file(tmp_path_factory):
    # ephemeral signing key generated in-process; no PEM ever touches the repo, .env or CI
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    path = tmp_path_factory.mktemp("identity") / "jwt_private_key.pem"
    path.write_bytes(pem)
    return str(path)


@pytest.fixture(autouse=True)
def identity_signing_key(settings, _identity_key_file):
    settings.IDENTITY_JWT_PRIVATE_KEY = ""
    settings.IDENTITY_JWT_PRIVATE_KEY_FILE = _identity_key_file


@pytest.fixture(scope="session")
def _static_root(tmp_path_factory):
    return tmp_path_factory.mktemp("staticfiles")


@pytest.fixture(autouse=True)
def static_root(settings, _static_root):
    # whitenoise indexes STATIC_ROOT whenever a test client builds the middleware, and only the image runs
    # collectstatic, so tests get an existing empty root instead of the missing one in the checkout
    settings.STATIC_ROOT = _static_root


@pytest.fixture(autouse=True)
def cart_redis(settings):
    # isolate cart data on a throwaway redis db and force the async client to
    # rebind to it (the module singleton is otherwise cached per event loop)
    from django.core.cache import cache

    # derive db 15 from whatever redis is configured (compose host or CI localhost)
    test_url = f"{settings.CART_REDIS_URL.rsplit('/', 1)[0]}/15"
    settings.CART_REDIS_URL = test_url
    settings.GEOCODER_ENABLED = False  # never hit the external provider in tests
    # isolate the rate-limit cache per test so buckets don't leak between tests
    settings.CACHES = {
        "default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "tests"}
    }
    cache.clear()
    service._clients.clear()
    service._sync_client = None
    conn = redis_sync.from_url(test_url)
    conn.flushdb()
    yield
    try:
        conn.flushdb()
    finally:
        conn.close()
    service._clients.clear()
    service._sync_client = None


@pytest.fixture
def api(client):
    # the sync test client may run each async request in a fresh event loop, so
    # drop the cached redis client before every call to rebind it to that loop
    class _Api:
        def _reset(self):
            service._clients.clear()

        def get(self, *a, **k):
            self._reset()
            return client.get(*a, **k)

        # the spa sends every write as json, and the api refuses any other body
        def post(self, *a, **k):
            self._reset()
            return client.post(*a, **{"content_type": "application/json", **k})

        def put(self, *a, **k):
            self._reset()
            return client.put(*a, **{"content_type": "application/json", **k})

        def delete(self, *a, **k):
            self._reset()
            return client.delete(*a, **k)

        def force_login(self, user):
            client.force_login(user)

        @property
        def cookies(self):
            return client.cookies

    return _Api()


@pytest.fixture
def make_product(db):
    counter = {"n": 0}

    def _make(name="Товар", price="100.00", stock=50, is_active=True, category="dish"):
        counter["n"] += 1
        n = counter["n"]
        return Product.objects.create(
            code=f"test_{n}",
            category=category,
            name=name,
            description="desc",
            price=price,
            stock_quantity=stock,
            is_active=is_active,
        )

    return _make


@pytest.fixture
def user(db):
    return get_user_model().objects.create_user(
        username="+79990000001",
        password="Pass!2345",
        first_name="Иван",
        phone="+79990000001",
    )


@pytest.fixture
def employee_user(db):
    from django.contrib.auth.models import Group

    from accounts.roles import StaffRole

    account = get_user_model().objects.create_user(
        username="+79990000010", password="Pass!2345", first_name="Сотрудник"
    )
    group, _ = Group.objects.get_or_create(name=StaffRole.MANAGER)
    account.groups.add(group)
    return account


@pytest.fixture
def superuser(db):
    return get_user_model().objects.create_superuser(
        username="+79990000011", password="Pass!2345"
    )


@pytest.fixture
def admin_client(client, superuser, settings):
    # admin pages reference static files, and tests run without a collected manifest
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    client.force_login(superuser)
    return client


@pytest.fixture
def seed_cart():
    def _seed(user_id: int, items: dict[int, int], version: int = 1):
        key = service.user_key(user_id)
        mapping = {str(pid): qty for pid, qty in items.items()}
        mapping[service.VERSION_FIELD] = version
        service._sync_redis().hset(key, mapping=mapping)
        return key

    return _seed
