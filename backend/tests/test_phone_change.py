import json

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction

from accounts import service as accounts_service
from orders.models import OrderOutbox
from tests.admin_forms import rendered_form

pytestmark = pytest.mark.django_db

OLD, NEW = "+380671111111", "+380672222222"


@pytest.fixture
def customer():
    # registered by number before logins existed: the number is its login
    return get_user_model().objects.create_user(
        username=OLD, phone=OLD, password="Pass!2345", first_name="Олег"
    )


def _login(client, login):
    return client.post(
        "/api/auth/login",
        data=json.dumps({"login": login, "password": "Pass!2345"}),
        content_type="application/json",
    )


def _register(client, login, phone=NEW, password="Pass!2345word"):
    return client.post(
        "/api/auth/register",
        data=json.dumps({"login": login, "phone": phone, "password": password, "name": "Новый"}),
        content_type="application/json",
    )


def test_a_customer_registers_with_a_login_and_signs_in_with_it(client):
    registered = _register(client, "Noodle.Fan")

    assert registered.status_code == 200
    assert (registered.json()["login"], registered.json()["phone"]) == ("noodle.fan", NEW)
    client.logout()
    signed = client.post(
        "/api/auth/login",
        data=json.dumps({"login": "NOODLE.fan", "password": "Pass!2345word"}),
        content_type="application/json",
    )
    assert signed.status_code == 200 and signed.json()["login"] == "noodle.fan"


def test_a_number_signs_in_nobody_who_only_gave_it_as_a_contact(client):
    _register(client, "noodle_fan", phone=NEW)
    client.logout()

    assert _login(client, NEW).status_code == 401


def test_two_accounts_may_give_the_same_contact_number(client, customer):
    first = _register(client, "first_one", phone=OLD)
    client.logout()
    second = _register(client, "second_one", phone=OLD)

    assert (first.status_code, second.status_code) == (200, 200)
    assert get_user_model().objects.filter(phone=OLD).count() == 3


@pytest.mark.parametrize("login", ["noodle", "NOODLE"])
def test_a_taken_login_is_refused_whatever_its_case(client, login):
    get_user_model().objects.create_user(username="noodle", password="Pass!2345")

    response = _register(client, login)

    assert response.status_code == 400
    assert get_user_model().objects.filter(username__iexact="noodle").count() == 1


@pytest.mark.parametrize("login", [OLD, "380671111111", "ab", "1noodle", "лапша", "noodle fan"])
def test_a_login_that_could_pass_for_a_number_or_breaks_the_pattern_is_refused(
    client, customer, login
):
    response = _register(client, login)

    assert response.status_code == 400
    assert get_user_model().objects.count() == 1


def test_a_registration_needs_a_valid_contact_number(client):
    assert _register(client, "noodle", phone="12345").status_code == 400
    assert not get_user_model().objects.filter(username="noodle").exists()


@pytest.mark.parametrize("typed", [OLD, "067 111 11 11", "0671111111", "+38 (067) 111-11-11"])
def test_an_account_of_the_phone_era_signs_in_with_its_number_as_typed(client, customer, typed):
    assert _login(client, typed).status_code == 200


@pytest.mark.parametrize(
    "path", ["/api/auth/register/code", "/api/auth/password/code", "/api/auth/password/reset"]
)
def test_nothing_confirms_or_recovers_by_text_message(client, customer, path):
    response = client.post(path, data=json.dumps({"phone": OLD}), content_type="application/json")

    assert response.status_code == 404


def test_a_contact_change_keeps_the_login_the_account_signs_in_with(client, customer):
    accounts_service.set_customer_profile(customer.pk, phone=NEW)

    customer.refresh_from_db()
    assert (customer.username, customer.phone) == (OLD, NEW)
    assert _login(client, OLD).status_code == 200
    assert _login(client, NEW).status_code == 401


def test_a_contact_is_stored_in_e164_and_announced(customer):
    accounts_service.set_customer_profile(customer.pk, phone="067 222 22 22")

    customer.refresh_from_db()
    assert customer.phone == NEW
    assert OrderOutbox.objects.filter(aggregate_type="customer", payload__phone=NEW).exists()


def test_a_contact_another_account_gave_is_accepted(customer):
    other = get_user_model().objects.create_user(username="other", phone=NEW, password="Pass!2345")

    accounts_service.set_customer_profile(customer.pk, phone=NEW)

    customer.refresh_from_db()
    other.refresh_from_db()
    assert (customer.username, customer.phone, other.username, other.phone) == (
        OLD,
        NEW,
        "other",
        NEW,
    )


def test_the_admin_changes_the_contact_and_leaves_the_login(admin_client, client, customer):
    url = f"/admin/accounts/user/{customer.pk}/change/"
    form = rendered_form(admin_client, url)
    form["phone"] = "0672222222"

    response = admin_client.post(url, form)

    assert response.status_code == 302
    customer.refresh_from_db()
    assert (customer.username, customer.phone) == (OLD, NEW)
    assert _login(client, OLD).status_code == 200


def test_the_admin_can_give_a_phone_era_account_a_login(admin_client, client, customer):
    url = f"/admin/accounts/user/{customer.pk}/change/"
    form = rendered_form(admin_client, url)
    assert "username" in form
    form["username"] = "oleg"

    response = admin_client.post(url, form)

    assert response.status_code == 302
    customer.refresh_from_db()
    assert (customer.username, customer.phone) == ("oleg", OLD)
    assert _login(client, "oleg").status_code == 200
    assert _login(client, OLD).status_code == 401


def test_the_admin_clears_the_contact_and_leaves_the_login(admin_client, client, customer):
    url = f"/admin/accounts/user/{customer.pk}/change/"
    form = rendered_form(admin_client, url)
    form["phone"] = ""

    response = admin_client.post(url, form)

    assert response.status_code == 302
    customer.refresh_from_db()
    assert (customer.username, customer.phone) == (OLD, "")
    assert _login(client, OLD).status_code == 200


def test_the_admin_form_reports_an_invalid_number(admin_client, customer):
    url = f"/admin/accounts/user/{customer.pk}/change/"
    form = rendered_form(admin_client, url)
    form["phone"] = "12345"

    response = admin_client.post(url, form)

    assert response.status_code == 200
    customer.refresh_from_db()
    assert (customer.username, customer.phone) == (OLD, OLD)


@pytest.mark.django_db(transaction=True)
def test_the_release_before_writes_accounts_and_codes_while_this_one_migrates():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.utils import timezone

    apps = (
        MigrationExecutor(connection)
        .loader.project_state([("accounts", "0011_phone_challenge")])
        .apps
    )
    users, codes = apps.get_model("accounts", "User"), apps.get_model("accounts", "PhoneChallenge")

    # it serves until the runtime is recreated: an account without a number gets null, and the codes stay
    bare = users.objects.create(username=NEW, password="x")
    code = codes.objects.create(
        phone=NEW, purpose="register", code_hash="h", expires_at=timezone.now()
    )

    assert users.objects.get(pk=bare.pk).phone is None
    assert codes.objects.get(pk=code.pk).phone == NEW


@pytest.mark.django_db(transaction=True)
def test_the_migration_keeps_every_account_and_frees_the_number():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before, after = (
        ("accounts", "0011_phone_challenge"),
        ("accounts", "0012_phone_is_a_contact"),
    )
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([before])
        old = executor.loader.project_state([before]).apps.get_model("accounts", "User")
        account = old.objects.create(username=OLD, phone=OLD, password="kept", first_name="Олег")
        bare = old.objects.create(username="admin", password="kept")

        MigrationExecutor(connection).migrate([after])

        User = (
            MigrationExecutor(connection)
            .loader.project_state([after])
            .apps.get_model("accounts", "User")
        )
        kept = User.objects.get(pk=account.pk)
        assert (kept.username, kept.phone, kept.password, kept.first_name) == (
            OLD,
            OLD,
            "kept",
            "Олег",
        )
        assert (User.objects.get(pk=bare.pk).username, User.objects.get(pk=bare.pk).phone) == (
            "admin",
            "",
        )
        User.objects.create(username="neighbour", phone=OLD, password="x")
        assert User.objects.filter(phone=OLD).count() == 2
    finally:
        MigrationExecutor(connection).migrate(leaves)


@pytest.mark.django_db(transaction=True)
def test_the_migration_aligns_the_login_with_the_phone_an_admin_changed():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before, after = (
        ("accounts", "0008_superuser_without_staff_groups"),
        ("accounts", "0009_login_follows_phone"),
    )
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([before])
        old = executor.loader.project_state([before]).apps.get_model("accounts", "User")
        split = old.objects.create(username=OLD, phone=NEW, password="x")
        typed = old.objects.create(username="+380673333333", phone="0673333333", password="x")
        admin = old.objects.create(username="admin", phone="+380674444444", password="x")

        MigrationExecutor(connection).migrate([after])

        # the schema of the migration under test, which later migrations may have grown since
        User = (
            MigrationExecutor(connection)
            .loader.project_state([after])
            .apps.get_model("accounts", "User")
        )
        assert User.objects.get(pk=split.pk).username == NEW
        assert (User.objects.get(pk=typed.pk).username, User.objects.get(pk=typed.pk).phone) == (
            "+380673333333",
            "+380673333333",
        )
        assert User.objects.get(pk=admin.pk).username == "admin"
    finally:
        MigrationExecutor(connection).migrate(leaves)


@pytest.mark.django_db(transaction=True)
def test_the_migration_stops_at_a_split_it_cannot_resolve():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before, after = (
        ("accounts", "0008_superuser_without_staff_groups"),
        ("accounts", "0009_login_follows_phone"),
    )
    executor = MigrationExecutor(connection)
    leaves = executor.loader.graph.leaf_nodes()
    try:
        executor.migrate([before])
        old = executor.loader.project_state([before]).apps.get_model("accounts", "User")
        old.objects.create(username=OLD, phone=NEW, password="x")
        old.objects.create(username=NEW, password="x")

        with pytest.raises(RuntimeError, match="login and phone"):
            MigrationExecutor(connection).migrate([after])
    finally:
        executor = MigrationExecutor(connection)
        executor.loader.project_state([before]).apps.get_model(
            "accounts", "User"
        ).objects.all().delete()
        executor.migrate(leaves)


def _executor():
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    return MigrationExecutor(connection)


def _users_at(node):
    return _executor().loader.project_state([node]).apps.get_model("accounts", "User")


@pytest.mark.django_db(transaction=True)
def test_going_back_stops_while_two_accounts_share_a_number():
    leaves = _executor().loader.graph.leaf_nodes()
    users = get_user_model()
    first = users.objects.create_user(username="first", phone=OLD, password="x")
    second = users.objects.create_user(username="second", phone=OLD, password="x")
    try:
        with pytest.raises(
            RuntimeError, match=rf"share a phone number.*\[{first.pk}, {second.pk}\]"
        ):
            _executor().migrate([("accounts", "0011_phone_challenge")])

        # nothing of the refused step is applied: both accounts keep the number
        assert sorted(users.objects.filter(phone=OLD).values_list("pk", flat=True)) == [
            first.pk,
            second.pk,
        ]
    finally:
        _executor().migrate(leaves)


@pytest.mark.django_db(transaction=True)
def test_going_back_without_a_shared_number_restores_the_unique_one():
    target = ("accounts", "0011_phone_challenge")
    leaves = _executor().loader.graph.leaf_nodes()
    users = get_user_model()
    holder = users.objects.create_user(username="holder", phone=OLD, password="x")
    users.objects.create_user(username="bare_one", password="x")
    users.objects.create_user(username="bare_two", password="x")
    try:
        _executor().migrate([target])

        old = _users_at(target)
        assert old.objects.get(pk=holder.pk).phone == OLD
        assert old.objects.filter(phone__isnull=True).count() == 2
        with pytest.raises(IntegrityError), transaction.atomic():
            old.objects.create(username="another", phone=OLD, password="x")
    finally:
        _executor().migrate(leaves)
