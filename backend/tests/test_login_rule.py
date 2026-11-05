import json

import pytest
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.management import CommandError, call_command
from django.db import IntegrityError, transaction

from tests.admin_forms import rendered_form

pytestmark = pytest.mark.django_db

PHONE_ERA = "+380671111111"
ADD = "/admin/accounts/user/add/"
EMAIL = "root@example.com"


@pytest.fixture
def alice():
    return get_user_model().objects.create_user(username="alice", password="Pass!2345")


@pytest.fixture
def phone_era():
    return get_user_model().objects.create_user(
        username=PHONE_ERA, phone=PHONE_ERA, password="Pass!2345", first_name="Олег"
    )


def _change(admin_client, user, **fields):
    url = f"/admin/accounts/user/{user.pk}/change/"
    form = rendered_form(admin_client, url)
    form.update(fields)
    return admin_client.post(url, form)


def _add(admin_client, username):
    form = rendered_form(admin_client, ADD)
    form.update({"username": username, "usable_password": "false"})
    return admin_client.post(ADD, form)


@pytest.mark.parametrize("username", ["Alice", "ALICE"])
def test_the_admin_gives_no_login_that_differs_only_in_case(
    admin_client, alice, phone_era, username
):
    response = _change(admin_client, phone_era, username=username)

    assert response.status_code == 200
    phone_era.refresh_from_db()
    assert phone_era.username == PHONE_ERA


@pytest.mark.parametrize("username", ["0672222222", "+380672222222", "380672222222"])
def test_the_admin_gives_no_login_shaped_like_a_number(admin_client, phone_era, username):
    response = _change(admin_client, phone_era, username=username)

    assert response.status_code == 200
    phone_era.refresh_from_db()
    assert phone_era.username == PHONE_ERA


def test_a_login_the_admin_gives_is_kept_in_lower_case(admin_client, phone_era):
    response = _change(admin_client, phone_era, username="Oleg.K")

    assert response.status_code == 302
    phone_era.refresh_from_db()
    assert phone_era.username == "oleg.k"


def test_an_account_of_the_phone_era_stays_editable_with_its_number_as_login(
    admin_client, phone_era
):
    response = _change(admin_client, phone_era, first_name="Олег Петрович")

    assert response.status_code == 302
    phone_era.refresh_from_db()
    assert (phone_era.username, phone_era.first_name) == (PHONE_ERA, "Олег Петрович")


@pytest.mark.parametrize("username", ["Alice", "0671111111", "+380671111111"])
def test_the_admin_adds_no_account_with_a_login_against_the_rule(admin_client, alice, username):
    before = get_user_model().objects.count()

    response = _add(admin_client, username)

    assert response.status_code == 200
    assert get_user_model().objects.count() == before


def test_the_admin_adds_an_account_under_the_rule(admin_client):
    response = _add(admin_client, "Bob_1")

    assert response.status_code == 302
    assert get_user_model().objects.filter(username="bob_1").exists()


def test_the_database_holds_no_two_logins_that_differ_only_in_case(alice):
    with pytest.raises(IntegrityError), transaction.atomic():
        get_user_model().objects.create_user(username="Alice", password="Pass!2345")


@pytest.mark.parametrize("username", ["0671111111", "+380671111111"])
def test_createsuperuser_gives_no_login_shaped_like_a_number(username):
    with pytest.raises(CommandError, match="Логин"):
        call_command("createsuperuser", interactive=False, username=username, email=EMAIL)

    assert not get_user_model().objects.filter(username=username).exists()


def test_createsuperuser_gives_no_login_that_differs_only_in_case(alice):
    with pytest.raises(CommandError, match="занят"):
        call_command("createsuperuser", interactive=False, username="Alice", email=EMAIL)

    assert get_user_model().objects.filter(username__iexact="alice").count() == 1


def test_createsuperuser_gives_a_login_under_the_rule():
    call_command("createsuperuser", interactive=False, username="root", email=EMAIL, verbosity=0)

    assert get_user_model().objects.get(username="root").is_superuser


# the same cases as frontend/tests/login.test.mjs: what the form lets through, the server takes
ACCEPTED = [
    ("abc", "abc"),
    ("Noodle.Fan", "noodle.fan"),
    ("a_b-c.d", "a_b-c.d"),
    ("a" + "b" * 31, "a" + "b" * 31),
    ("\u212aat", "kat"),
]
REFUSED = [
    "ab",
    "a" + "b" * 32,
    "1abc",
    "_abc",
    "\u017fam",
    "\u0131van",
    "\u0430bc",
    "ab c",
    "+380671111111",
    "",
]


def _register(client, login):
    return client.post(
        "/api/auth/register",
        data=json.dumps(
            {"login": login, "phone": "+380672222222", "password": "Pass!2345word", "name": "Новый"}
        ),
        content_type="application/json",
    )


@pytest.mark.parametrize(("login", "stored"), ACCEPTED)
def test_the_server_takes_the_logins_the_form_takes(client, login, stored):
    response = _register(client, login)

    assert response.status_code == 200, response.content
    assert response.json()["login"] == stored


@pytest.mark.parametrize("login", REFUSED)
def test_the_server_refuses_the_logins_the_form_refuses(client, login):
    response = _register(client, login)

    assert response.status_code in (400, 422)
    assert not get_user_model().objects.exists()


@pytest.mark.parametrize(
    ("username", "stored"),
    [
        ("alice", True),
        (PHONE_ERA, True),
        ("0671111111", False),
        ("al ice", False),
        ("\u017fam", False),
        ("+3\u0668\u0660\u0666\u0667\u0661\u0661\u0661\u0661\u0661\u0661", False),
    ],
)
def test_a_stored_login_is_a_login_or_a_number_of_the_phone_era(username, stored):
    user = get_user_model()(username=username, password="x")

    if stored:
        user.full_clean(exclude=["password"])
    else:
        with pytest.raises(ValidationError, match="Логин"):
            user.full_clean(exclude=["password"])


def _executor():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    return MigrationExecutor(connection)


def _users_at(node):
    return _executor().loader.project_state([node]).apps.get_model("accounts", "User")


@pytest.mark.django_db(transaction=True)
def test_the_rule_migration_stops_at_logins_that_differ_only_in_case():
    before, after = ("accounts", "0012_phone_is_a_contact"), ("accounts", "0013_login_rule")
    leaves = _executor().loader.graph.leaf_nodes()
    try:
        _executor().migrate([before])
        users = _users_at(before)
        users.objects.create(username="alice", password="x")
        users.objects.create(username="Alice", password="x")

        with pytest.raises(RuntimeError, match="differ only in case"):
            _executor().migrate([after])
    finally:
        _users_at(before).objects.filter(username="Alice").delete()
        _executor().migrate(leaves)
