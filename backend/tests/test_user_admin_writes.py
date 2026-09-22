import threading

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import connection
from django.test import Client

from accounts.admin import CustomUserAdmin
from accounts.roles import StaffRole, set_staff_role
from employee import service as employee_service
from orders.models import OrderOutbox
from tests.admin_forms import rendered_form

pytestmark = pytest.mark.django_db


def _url(user) -> str:
    return f"/admin/accounts/user/{user.pk}/change/"


def _fresh(user):
    return get_user_model().objects.get(pk=user.pk)


def _authz_events(user) -> int:
    return OrderOutbox.objects.filter(aggregate_type="authz", aggregate_id=str(user.pk)).count()


def test_a_form_opened_before_a_deactivation_does_not_reactivate(
    admin_client, superuser, employee_user
):
    form = rendered_form(admin_client, _url(employee_user))
    employee_service.set_active(actor=superuser, target=employee_user, active=False)
    revoked = _fresh(employee_user)
    events = _authz_events(employee_user)

    form["email"] = "moved@example.com"
    response = admin_client.post(_url(employee_user), form)

    assert response.status_code == 302
    after = _fresh(employee_user)
    assert (after.is_active, after.authz_version) == (False, revoked.authz_version)
    assert after.email == "moved@example.com"
    assert _authz_events(employee_user) == events


def test_a_form_opened_before_a_superuser_revocation_does_not_restore_it(admin_client, superuser):
    other = get_user_model().objects.create_superuser(username="+79990000012", password="x")
    form = rendered_form(admin_client, _url(other))
    employee_service.set_superuser(actor=superuser, target=other, is_superuser=False)
    revoked = _fresh(other)

    form["email"] = "moved@example.com"
    response = admin_client.post(_url(other), form)

    assert response.status_code == 302
    after = _fresh(other)
    assert (after.is_superuser, after.authz_version) == (False, revoked.authz_version)


def test_a_form_opened_before_a_role_revocation_does_not_restore_the_role(
    admin_client, superuser, employee_user
):
    form = rendered_form(admin_client, _url(employee_user))
    set_staff_role(actor=superuser, target=employee_user, role=None)

    form["email"] = "moved@example.com"
    admin_client.post(_url(employee_user), form)

    staff = [*StaffRole.values, "restaurant_employee"]
    assert not _fresh(employee_user).groups.filter(name__in=staff).exists()


def test_an_explicit_access_change_from_a_form_older_than_the_last_one_is_refused(
    admin_client, superuser, employee_user
):
    form = rendered_form(admin_client, _url(employee_user))
    set_staff_role(actor=superuser, target=employee_user, role=StaffRole.OPERATOR)
    changed = _fresh(employee_user)

    form.pop("is_active", None)
    response = admin_client.post(_url(employee_user), form)

    assert response.status_code == 200
    assert "Пользователь изменился" in response.content.decode()
    after = _fresh(employee_user)
    assert (after.is_active, after.authz_version) == (True, changed.authz_version)


@pytest.mark.django_db(transaction=True)
def test_a_deactivation_landing_inside_an_admin_save_is_not_undone(monkeypatch, settings):
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }
    users = get_user_model()
    admin = users.objects.create_superuser(username="+79990000011", password="Pass!2345")
    target = users.objects.create_user(username="+79990000010", password="Pass!2345")
    target.groups.add(Group.objects.get_or_create(name=StaffRole.MANAGER)[0])
    client = Client()
    client.force_login(admin)
    form = rendered_form(client, _url(target))
    form["email"] = "moved@example.com"

    loaded, release, errors = threading.Event(), threading.Event(), []
    real = CustomUserAdmin.get_object

    def admin_loads_then_waits(self, request, object_id, from_field=None):
        obj = real(self, request, object_id, from_field)
        if threading.current_thread().name == "admin" and request.method == "POST":
            loaded.set()
            assert release.wait(timeout=20)
        return obj

    monkeypatch.setattr(CustomUserAdmin, "get_object", admin_loads_then_waits)

    def save():
        try:
            client.post(_url(target), form)
        except Exception as exc:
            errors.append(exc)
        finally:
            connection.close()

    thread = threading.Thread(target=save, name="admin")
    thread.start()
    assert loaded.wait(timeout=15)
    employee_service.set_active(actor=admin, target=target, active=False)
    revoked = _fresh(target)
    release.set()
    thread.join(timeout=30)

    assert not errors, errors
    after = _fresh(target)
    assert (after.is_active, after.authz_version) == (False, revoked.authz_version)
    assert after.email == "moved@example.com"


def test_the_change_page_carries_no_password_hash(admin_client, employee_user):
    page = admin_client.get(_url(employee_user)).content.decode()

    assert 'name="initial-password"' not in page
    assert employee_user.password not in page
