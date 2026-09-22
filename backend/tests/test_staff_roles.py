"""
separation of duties between staff roles

one group granted order handling, stock adjustment, the customer directory and every customer's phone and address at once
an operator who processes orders has no business reading that
"""

import json

import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group

from accounts.roles import StaffRole

pytestmark = pytest.mark.django_db

ORDER_ROUTES = ["/api/employee/orders", "/api/employee/orders/1"]
MANAGER_ROUTES = ["/api/employee/inventory", "/api/employee/users", "/api/employee/users/1"]


def staff_role_names(user) -> list[str]:
    user.refresh_from_db()
    return sorted(g.name for g in user.groups.all())


def _staff(username, role):
    user = get_user_model().objects.create_user(username=username, password="Pass!2345")
    if role is not None:
        group, _ = Group.objects.get_or_create(name=role)
        user.groups.add(group)
    return user


@pytest.fixture
def operator():
    return _staff("+79990000021", StaffRole.OPERATOR)


@pytest.fixture
def manager():
    return _staff("+79990000022", StaffRole.MANAGER)


def test_an_operator_reaches_the_order_queue(api, operator):
    api.force_login(operator)

    assert api.get(ORDER_ROUTES[0]).status_code == 200


@pytest.mark.parametrize("path", MANAGER_ROUTES)
def test_an_operator_is_refused_the_manager_area(api, operator, path):
    api.force_login(operator)

    assert api.get(path).status_code == 403, f"{path} exposes stock or customer data to an operator"


def test_an_operator_cannot_adjust_stock(api, operator, make_product):
    product = make_product(stock=5)
    api.force_login(operator)

    response = api.post(f"/api/employee/inventory/{product.id}/adjust",
                        data=json.dumps({"new_quantity": 99, "reason": "проверка прав", "expected_version": product.version}),
                        content_type="application/json")

    assert response.status_code == 403
    product.refresh_from_db()
    assert product.stock_quantity == 5


@pytest.mark.parametrize("path", MANAGER_ROUTES)
def test_a_manager_reaches_the_manager_area(api, manager, path):
    api.force_login(manager)

    assert api.get(path).status_code in (200, 404)


def test_a_manager_still_handles_orders(api, manager):
    api.force_login(manager)

    assert api.get(ORDER_ROUTES[0]).status_code == 200


def test_a_customer_reaches_nothing(api, user):
    api.force_login(user)

    for path in ORDER_ROUTES + MANAGER_ROUTES:
        assert api.get(path).status_code == 403, path


def test_a_superuser_keeps_every_capability(api, superuser):
    api.force_login(superuser)

    for path in [ORDER_ROUTES[0]] + MANAGER_ROUTES:
        assert api.get(path).status_code in (200, 404), path


def test_only_a_superuser_assigns_roles(api, manager, operator):
    api.force_login(manager)

    response = api.post(f"/api/employee/users/{operator.id}/role",
                        data=json.dumps({"role": StaffRole.MANAGER}),
                        content_type="application/json")

    assert response.status_code == 403
    assert StaffRole.MANAGER not in [g.name for g in operator.groups.all()]


def test_a_staff_member_holds_at_most_one_role(superuser, operator):
    from accounts.roles import set_staff_role

    set_staff_role(actor=superuser, target=operator, role=StaffRole.MANAGER)

    operator.refresh_from_db()
    assert sorted(g.name for g in operator.groups.all()) == [StaffRole.MANAGER]


def test_changing_a_role_bumps_the_authorization_version(superuser, operator):
    from accounts.roles import set_staff_role

    before = operator.authz_version
    set_staff_role(actor=superuser, target=operator, role=StaffRole.MANAGER)
    operator.refresh_from_db()
    bumped = operator.authz_version

    set_staff_role(actor=superuser, target=operator, role=StaffRole.MANAGER)
    operator.refresh_from_db()

    assert bumped == before + 1
    assert operator.authz_version == bumped, "an unchanged role must not invalidate live tokens"


def test_the_token_carries_the_role_the_user_actually_holds(operator, manager):
    import jwt as pyjwt

    from accounts import jwt_service

    for user, expected in ((operator, StaffRole.OPERATOR), (manager, StaffRole.MANAGER)):
        token, _ = jwt_service.issue_employee_token(user)
        claims = pyjwt.decode(token, options={"verify_signature": False})
        assert claims["roles"] == [expected], f"{user.username} got {claims['roles']}"


def test_the_backfill_covers_every_role_not_just_the_legacy_group(operator, manager, superuser):
    from django.core.management import call_command

    from orders.models import OrderOutbox

    OrderOutbox.objects.filter(event_type="identity.authz_changed").delete()
    call_command("emit_authz_state")

    emitted = {
        row.payload["subject_id"]
        for row in OrderOutbox.objects.filter(event_type="identity.authz_changed")
    }
    assert {operator.id, manager.id, superuser.id} <= emitted, "an operator would never be projected"


def test_only_an_active_superuser_may_change_a_role(operator, manager):
    from accounts.roles import NotAuthorized, set_staff_role

    for actor in (manager, None):
        with pytest.raises(NotAuthorized):
            set_staff_role(actor=actor, target=operator, role=StaffRole.MANAGER)

    manager.is_superuser = True
    manager.is_active = False
    manager.save(update_fields=["is_superuser", "is_active"])
    with pytest.raises(NotAuthorized):
        set_staff_role(actor=manager, target=operator, role=StaffRole.MANAGER)

    operator.refresh_from_db()
    assert StaffRole.MANAGER not in [g.name for g in operator.groups.all()]


def test_holding_two_roles_grants_nothing(operator):
    from django.contrib.auth.models import Group

    from accounts.roles import Capability, has_capability, staff_role

    group, _ = Group.objects.get_or_create(name=StaffRole.MANAGER)
    operator.groups.add(group)

    assert staff_role(operator) is None, "an ambiguous membership must not resolve to a role"
    assert not has_capability(operator, Capability.ORDERS)


def test_the_last_active_superuser_cannot_lock_everyone_out(superuser):
    from employee import service

    with pytest.raises(ValueError):
        service.set_superuser(actor=superuser, target=superuser, is_superuser=False)
    with pytest.raises(ValueError):
        service.set_active(actor=superuser, target=superuser, active=False)

    superuser.refresh_from_db()
    assert superuser.is_superuser and superuser.is_active


def test_a_second_superuser_may_still_be_demoted(superuser, django_user_model):
    from employee import service

    other = django_user_model.objects.create_superuser(username="+79990000077", password="Pass!2345")

    service.set_superuser(actor=superuser, target=other, is_superuser=False)

    other.refresh_from_db()
    assert other.is_superuser is False


def test_the_audit_records_what_the_role_changed_from_and_to(superuser, operator):
    from accounts.models import EmployeeRoleAudit
    from accounts.roles import set_staff_role

    set_staff_role(actor=superuser, target=operator, role=StaffRole.MANAGER)

    entry = EmployeeRoleAudit.objects.filter(target=operator).latest("created_at")
    assert (entry.from_role, entry.to_role) == (StaffRole.OPERATOR, StaffRole.MANAGER)
    assert entry.action == EmployeeRoleAudit.Action.CHANGE


def test_a_broken_double_grant_can_be_revoked(superuser, operator):
    from django.contrib.auth.models import Group

    from accounts.roles import set_staff_role, staff_role

    operator.groups.add(Group.objects.get_or_create(name=StaffRole.MANAGER)[0])
    assert staff_role(operator) is None  # ambiguous, so nothing resolves

    set_staff_role(actor=superuser, target=operator, role=None)

    operator.refresh_from_db()
    assert list(operator.groups.all()) == [], "revoking must clear what is actually there"


def test_a_legacy_group_is_cleared_by_a_revoke(superuser, operator):
    from django.contrib.auth.models import Group

    from accounts.roles import LEGACY_GROUP, set_staff_role

    operator.groups.add(Group.objects.get_or_create(name=LEGACY_GROUP)[0])

    set_staff_role(actor=superuser, target=operator, role=None)

    operator.refresh_from_db()
    assert list(operator.groups.all()) == []


@pytest.mark.parametrize("call", ["superuser", "active"])
def test_privileged_changes_need_an_active_superuser(manager, operator, call, django_user_model):
    from accounts.roles import NotAuthorized
    from employee import service

    django_user_model.objects.create_superuser(username="+79990000066", password="Pass!2345")
    target = operator
    target.is_superuser = True
    target.save(update_fields=["is_superuser"])

    with pytest.raises(NotAuthorized):
        if call == "superuser":
            service.set_superuser(actor=manager, target=target, is_superuser=False)
        else:
            service.set_active(actor=manager, target=target, active=False)

    target.refresh_from_db()
    assert target.is_superuser and target.is_active


def test_a_deactivated_superuser_can_no_longer_act(superuser, operator, django_user_model):
    from accounts.roles import NotAuthorized
    from employee import service

    django_user_model.objects.create_superuser(username="+79990000067", password="Pass!2345")
    superuser.is_active = False
    superuser.save(update_fields=["is_active"])

    with pytest.raises(NotAuthorized):
        service.set_superuser(actor=superuser, target=operator, is_superuser=True)


def test_a_stale_superuser_object_cannot_still_assign_a_role(superuser, operator, django_user_model):
    """the caller holds an ORM object; the rights it was loaded with can be gone by now"""
    from accounts.models import EmployeeRoleAudit
    from accounts.roles import NotAuthorized, set_staff_role
    from orders.models import OrderOutbox

    django_user_model.objects.create_superuser(username="+79990000301", password="Pass!2345")
    stale = django_user_model.objects.get(pk=superuser.pk)  # loaded while still privileged
    django_user_model.objects.filter(pk=superuser.pk).update(is_active=False)
    outbox_before = OrderOutbox.objects.count()

    with pytest.raises(NotAuthorized):
        set_staff_role(actor=stale, target=operator, role=StaffRole.MANAGER)

    operator.refresh_from_db()
    assert staff_role_names(operator) == [StaffRole.OPERATOR]
    assert not EmployeeRoleAudit.objects.filter(target=operator).exists()
    assert OrderOutbox.objects.count() == outbox_before


def test_a_demoted_superuser_object_cannot_still_assign_a_role(superuser, operator, django_user_model):
    from accounts.roles import NotAuthorized, set_staff_role

    django_user_model.objects.create_superuser(username="+79990000302", password="Pass!2345")
    stale = django_user_model.objects.get(pk=superuser.pk)
    django_user_model.objects.filter(pk=superuser.pk).update(is_superuser=False)

    with pytest.raises(NotAuthorized):
        set_staff_role(actor=stale, target=operator, role=StaffRole.MANAGER)

    assert staff_role_names(operator) == [StaffRole.OPERATOR]


def test_the_audit_names_the_actor_as_the_database_knows_them(superuser, operator):
    from accounts.models import EmployeeRoleAudit
    from accounts.roles import set_staff_role

    set_staff_role(actor=superuser, target=operator, role=StaffRole.MANAGER)

    entry = EmployeeRoleAudit.objects.filter(target=operator).latest("created_at")
    assert entry.actor_id == superuser.pk
