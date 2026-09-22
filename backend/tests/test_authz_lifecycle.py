from types import SimpleNamespace

import pytest
from django.contrib import admin as dj_admin
from django.contrib.auth.models import Group
from django.contrib.messages.storage.fallback import FallbackStorage
from django.core.management import call_command
from django.test import Client, RequestFactory

from accounts.admin import CustomUserAdmin
from accounts.models import EmployeeRoleAudit, User
from accounts.roles import StaffRole, set_staff_role
from employee import service
from orders.models import OrderOutbox

pytestmark = pytest.mark.django_db


def _authz_rows(subject_id):
    return OrderOutbox.objects.filter(event_type="identity.authz_changed", aggregate_id=str(subject_id))


def _msg_request(actor):
    req = RequestFactory().post("/")
    req.user = actor
    req.session = {}
    req._messages = FallbackStorage(req)
    return req


# role service: correctness, idempotency, active state
def test_grant_bumps_version_audits_and_emits(user, superuser):
    start = user.authz_version
    set_staff_role(actor=superuser, target=user, role=StaffRole.MANAGER)
    user.refresh_from_db()
    assert user.groups.filter(name=StaffRole.MANAGER).exists()
    assert user.authz_version == start + 1
    assert EmployeeRoleAudit.objects.filter(target=user, action="grant").exists()
    row = _authz_rows(user.id).latest("created_at")
    assert row.aggregate_version == user.authz_version
    assert row.payload == {"subject_id": user.id, "authz_version": user.authz_version,
                           "role_active": True, "roles": [StaffRole.MANAGER],
                           "user_active": True}


def test_repeated_grant_is_a_noop(user, superuser):
    set_staff_role(actor=superuser, target=user, role=StaffRole.MANAGER)
    user.refresh_from_db()
    v, audits, events = user.authz_version, EmployeeRoleAudit.objects.filter(target=user).count(), _authz_rows(user.id).count()
    set_staff_role(actor=superuser, target=user, role=StaffRole.MANAGER)  # already granted
    user.refresh_from_db()
    assert user.authz_version == v
    assert EmployeeRoleAudit.objects.filter(target=user).count() == audits
    assert _authz_rows(user.id).count() == events


def test_repeated_revoke_of_absent_role_is_a_noop(user, superuser):
    start = user.authz_version
    set_staff_role(actor=superuser, target=user, role=None)  # not a member
    user.refresh_from_db()
    assert user.authz_version == start
    assert not _authz_rows(user.id).exists()
    assert not EmployeeRoleAudit.objects.filter(target=user).exists()


def test_deactivating_employee_revokes_via_authz(employee_user, superuser):
    start = employee_user.authz_version
    service.set_active(actor=superuser, target=employee_user, active=False)
    employee_user.refresh_from_db()
    assert employee_user.is_active is False
    assert employee_user.authz_version == start + 1
    row = _authz_rows(employee_user.id).latest("created_at")
    assert row.payload["user_active"] is False


def test_deactivating_customer_emits_no_authz(user, superuser):
    start = user.authz_version
    service.set_active(actor=superuser, target=user, active=False)
    user.refresh_from_db()
    assert user.is_active is False and user.authz_version == start
    assert not _authz_rows(user.id).exists()


# Admin: role membership & is_active are not editable in place
def test_admin_cannot_grant_employee_role(user, superuser):
    ma = CustomUserAdmin(User, dj_admin.site)
    group, _ = Group.objects.get_or_create(name=StaffRole.MANAGER)
    form = SimpleNamespace(instance=user, save_edited_m2m=lambda: user.groups.add(group))  # form tries to add
    ma.save_related(_msg_request(superuser), form, [], change=True)
    user.refresh_from_db()
    assert not user.groups.filter(name=StaffRole.MANAGER).exists()
    assert not EmployeeRoleAudit.objects.filter(target=user).exists()


def test_admin_cannot_revoke_employee_role(user, superuser):
    group, _ = Group.objects.get_or_create(name=StaffRole.MANAGER)
    user.groups.add(group)
    ma = CustomUserAdmin(User, dj_admin.site)
    form = SimpleNamespace(instance=user, save_edited_m2m=lambda: user.groups.remove(group))  # form tries to remove
    ma.save_related(_msg_request(superuser), form, [], change=True)
    user.refresh_from_db()
    assert user.groups.filter(name=StaffRole.MANAGER).exists()


def test_admin_membership_has_no_shared_request_state(user, employee_user, superuser):
    # two independent users through the SAME ModelAdmin singleton must not cross state
    ma = CustomUserAdmin(User, dj_admin.site)
    group, _ = Group.objects.get_or_create(name=StaffRole.MANAGER)
    # employee_user is a member; a form tries to remove it -> must be restored
    form_a = SimpleNamespace(instance=employee_user, save_edited_m2m=lambda: employee_user.groups.remove(group))
    ma.save_related(_msg_request(superuser), form_a, [], change=True)
    # user is not a member; a form tries to add it -> must be reverted
    form_b = SimpleNamespace(instance=user, save_edited_m2m=lambda: user.groups.add(group))
    ma.save_related(_msg_request(superuser), form_b, [], change=True)
    employee_user.refresh_from_db()
    user.refresh_from_db()
    assert employee_user.groups.filter(name=StaffRole.MANAGER).exists()      # restored
    assert not user.groups.filter(name=StaffRole.MANAGER).exists()           # reverted


def test_admin_is_active_change_routes_through_service(employee_user, superuser):
    ma = CustomUserAdmin(User, dj_admin.site)
    start = employee_user.authz_version
    employee_user.is_active = False  # what the admin form submitted
    ma.save_model(_msg_request(superuser), employee_user, SimpleNamespace(changed_data=["is_active"]), change=True)
    employee_user.refresh_from_db()
    assert employee_user.is_active is False and employee_user.authz_version == start + 1
    assert _authz_rows(employee_user.id).latest("created_at").payload["user_active"] is False


def test_admin_is_superuser_change_routes_through_service(user, superuser):
    ma = CustomUserAdmin(User, dj_admin.site)
    start = user.authz_version
    user.is_superuser = True  # what the admin form submitted
    ma.save_model(_msg_request(superuser), user, SimpleNamespace(changed_data=["is_superuser"]), change=True)
    user.refresh_from_db()
    assert user.is_superuser is True and user.authz_version == start + 1
    assert _authz_rows(user.id).latest("created_at").payload["role_active"] is True


def test_revoking_superuser_emits_revocation(superuser, employee_user, django_user_model):
    # someone has to be left holding the keys, so the guard against locking everyone out does not fire on what this test is actually about
    actor = django_user_model.objects.create_superuser(username="+79990000088", password="Pass!2345")
    start = superuser.authz_version
    service.set_superuser(actor=actor, target=superuser, is_superuser=False)
    superuser.refresh_from_db()
    assert superuser.is_superuser is False and superuser.authz_version == start + 1
    row = _authz_rows(superuser.id).latest("created_at")
    assert row.payload["role_active"] is False
    assert row.aggregate_version == superuser.authz_version
    assert EmployeeRoleAudit.objects.filter(target=superuser, action="revoke").exists()


def test_repeated_superuser_change_is_a_noop(superuser, employee_user, django_user_model):
    actor = django_user_model.objects.create_superuser(username="+79990000089", password="Pass!2345")
    start = superuser.authz_version
    service.set_superuser(actor=actor, target=superuser, is_superuser=True)
    superuser.refresh_from_db()
    assert superuser.authz_version == start
    assert not _authz_rows(superuser.id).exists()


# superuser is an operations employee (decision A)
def test_backfill_emits_authz_for_superuser(superuser):
    call_command("emit_authz_state")
    row = _authz_rows(superuser.id).latest("created_at")
    assert row.payload["role_active"] is True and row.payload["user_active"] is True


# CSRF on session-authenticated POSTs
def test_employee_token_and_logout_enforce_csrf(employee_user):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(employee_user)
    assert csrf_client.post("/api/auth/employee-token").status_code == 403
    assert csrf_client.post("/api/auth/logout").status_code == 403


def test_employee_token_works_with_csrf(employee_user):
    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(employee_user)
    csrf_client.get("/api/auth/csrf")
    token = csrf_client.cookies["csrftoken"].value
    resp = csrf_client.post("/api/auth/employee-token", content_type="application/json", HTTP_X_CSRFTOKEN=token)
    assert resp.status_code == 200 and resp.json()["token"]
