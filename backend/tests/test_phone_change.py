import json

import pytest
from django.contrib.auth import get_user_model

from accounts import service as accounts_service
from orders.models import OrderOutbox
from tests.admin_forms import rendered_form

pytestmark = pytest.mark.django_db

OLD, NEW = "+380671111111", "+380672222222"


@pytest.fixture
def customer():
    return get_user_model().objects.create_user(
        username=OLD, phone=OLD, password="Pass!2345", first_name="Олег"
    )


def _login(client, phone):
    return client.post(
        "/api/auth/login",
        data=json.dumps({"phone": phone, "password": "Pass!2345"}),
        content_type="application/json",
    )


def test_after_a_phone_change_the_new_number_logs_in_and_the_old_one_does_not(client, customer):
    accounts_service.set_customer_profile(customer.pk, phone=NEW)

    assert _login(client, OLD).status_code == 401
    assert _login(client, NEW).status_code == 200


def test_the_old_number_can_be_registered_by_its_next_owner(client, customer):
    accounts_service.set_customer_profile(customer.pk, phone=NEW)

    response = client.post(
        "/api/auth/register",
        content_type="application/json",
        data=json.dumps({"phone": OLD, "password": "Pass!2345word", "name": "Новый"}),
    )

    assert response.status_code == 200


def test_a_phone_is_stored_in_e164_whatever_the_admin_typed(customer):
    accounts_service.set_customer_profile(customer.pk, phone="067 222 22 22")

    customer.refresh_from_db()
    assert (customer.phone, customer.username) == (NEW, NEW)
    assert OrderOutbox.objects.filter(aggregate_type="customer", payload__phone=NEW).exists()


def test_a_number_another_account_logs_in_with_is_refused(customer):
    get_user_model().objects.create_user(username=NEW, password="Pass!2345")

    with pytest.raises(accounts_service.PhoneTaken):
        accounts_service.set_customer_profile(customer.pk, phone=NEW)

    customer.refresh_from_db()
    assert (customer.phone, customer.username) == (OLD, OLD)


def test_an_account_whose_login_is_not_its_phone_keeps_its_login():
    staff = get_user_model().objects.create_user(
        username="kitchen-lead", phone=OLD, password="Pass!2345"
    )

    accounts_service.set_customer_profile(staff.pk, phone=NEW)

    staff.refresh_from_db()
    assert (staff.phone, staff.username) == (NEW, "kitchen-lead")


def test_the_admin_form_changes_phone_and_login_together(admin_client, client, customer):
    url = f"/admin/accounts/user/{customer.pk}/change/"
    form = rendered_form(admin_client, url)
    form["phone"] = "0672222222"

    response = admin_client.post(url, form)

    assert response.status_code == 302
    assert _login(client, NEW).status_code == 200


def test_the_admin_form_reports_a_taken_or_invalid_number(admin_client, customer):
    get_user_model().objects.create_user(username=NEW, phone=NEW, password="Pass!2345")
    url = f"/admin/accounts/user/{customer.pk}/change/"

    for number in (NEW, "12345"):
        form = rendered_form(admin_client, url)
        form["phone"] = number
        response = admin_client.post(url, form)
        assert response.status_code == 200, number

    customer.refresh_from_db()
    assert (customer.phone, customer.username) == (OLD, OLD)


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

        User = get_user_model()
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
        get_user_model().objects.all().delete()
        MigrationExecutor(connection).migrate(leaves)
