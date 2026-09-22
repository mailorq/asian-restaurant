import json

import pytest
from django.contrib.auth import get_user_model
from django.test import Client

from tests.admin_forms import rendered_form

pytestmark = pytest.mark.django_db

OLD, NEW = "+380671111111", "+380672222222"


def _url(user) -> str:
    return f"/admin/accounts/user/{user.pk}/change/"


def _fresh(user):
    return get_user_model().objects.get(pk=user.pk)


@pytest.fixture
def customer():
    return get_user_model().objects.create_user(
        username=OLD, phone=OLD, password="Pass!2345", first_name="Олег"
    )


def _login(phone):
    return Client().post(
        "/api/auth/login",
        data=json.dumps({"phone": phone, "password": "Pass!2345"}),
        content_type="application/json",
    )


def test_the_login_of_a_phone_account_is_not_editable_in_the_admin(admin_client, customer):
    form = rendered_form(admin_client, _url(customer))
    assert "username" not in form
    form["username"] = "someone-else"

    response = admin_client.post(_url(customer), form)

    assert response.status_code == 302
    assert _fresh(customer).username == OLD
    assert _login(OLD).status_code == 200


def test_a_phone_and_login_edited_together_leave_the_login_on_the_new_number(
    admin_client, customer
):
    form = rendered_form(admin_client, _url(customer))
    form["username"] = "someone-else"
    form["phone"] = "0672222222"

    response = admin_client.post(_url(customer), form)

    assert response.status_code == 302
    assert (_fresh(customer).username, _fresh(customer).phone) == (NEW, NEW)
    assert _login(NEW).status_code == 200
    assert _login(OLD).status_code == 401


def test_an_account_that_logs_in_by_name_keeps_an_editable_login(admin_client):
    staff = get_user_model().objects.create_user(username="kitchen-lead", password="Pass!2345")
    form = rendered_form(admin_client, _url(staff))
    form["username"] = "kitchen-chief"

    response = admin_client.post(_url(staff), form)

    assert response.status_code == 302
    assert _fresh(staff).username == "kitchen-chief"
