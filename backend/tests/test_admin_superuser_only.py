import pytest
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission

from accounts.roles import StaffRole, set_staff_role, staff_role
from orders import service as order_service
from orders.models import OrderStatusHistory
from orders.service import CheckoutError
from tests.admin_forms import rendered_form

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def plain_static(settings):
    # admin pages reference static files, and tests run without a collected manifest
    settings.STORAGES = {
        **settings.STORAGES,
        "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    }


@pytest.fixture
def order(user, make_product, seed_cart):
    product = make_product(stock=10)
    seed_cart(user.id, {product.id: 1}, version=1)
    return order_service.checkout(user, "ул. Пушкина, 12", "cash", "idem-admin")


def _staff(phone: str, **extra):
    return get_user_model().objects.create_user(username=phone, password="Pass!2345", **extra)


def test_staff_with_view_order_neither_opens_the_admin_nor_moves_an_order(client, order):
    staff = _staff("+79990000301", is_staff=True)
    staff.user_permissions.add(Permission.objects.get(codename="view_order"))
    client.force_login(staff)

    assert client.get("/admin/orders/order/").status_code == 302
    response = client.post(
        "/admin/orders/order/", {"action": "transition_to_confirmed", "_selected_action": [order.pk]}
    )
    assert response.status_code == 302
    assert response["Location"].startswith("/admin/login/")
    order.refresh_from_db()
    assert order.status == "created"


def test_a_manager_works_in_the_panel_and_leaves_the_catalog_to_the_superuser(client, api, superuser, make_product):
    product = make_product(name="Рамен", price="100.00", stock=5)
    manager = _staff("+79990000302", is_staff=True)
    set_staff_role(actor=superuser, target=manager, role=StaffRole.MANAGER)
    client.force_login(manager)

    for url in (
        "/admin/",
        f"/admin/menu/product/{product.pk}/change/",
        f"/admin/accounts/user/{superuser.pk}/password/",
        "/admin/auth/group/",
    ):
        assert client.get(url).status_code == 302, url
    client.post(f"/admin/menu/product/{product.pk}/change/", {"name": "Другое", "price": "1.00"})

    # the panel changes stock only: card fields sent along are not applied
    adjusted = api.post(
        f"/api/employee/inventory/{product.pk}/adjust",
        {"new_quantity": 7, "reason": "пересчет", "expected_version": product.version, "name": "Другое", "price": "1.00"},
    )
    assert adjusted.status_code == 200
    product.refresh_from_db()
    assert (product.name, str(product.price), product.stock_quantity) == ("Рамен", "100.00", 7)


def test_the_admin_login_admits_an_active_superuser_only(client):
    users = get_user_model()
    root = users.objects.create_superuser(username="+79990000303", password="Pass!2345")
    users.objects.filter(pk=root.pk).update(is_staff=False)
    staff = _staff("+79990000304", is_staff=True)

    refused = client.post("/admin/login/", {"username": staff.username, "password": "Pass!2345"})
    assert refused.status_code == 200
    assert "_auth_user_id" not in client.session

    admitted = client.post("/admin/login/", {"username": root.username, "password": "Pass!2345"})
    assert admitted.status_code == 302
    assert client.session["_auth_user_id"] == str(root.pk)


def test_role_groups_are_read_only_and_their_names_are_taken(admin_client, superuser):
    manager = _staff("+79990000305")
    set_staff_role(actor=superuser, target=manager, role=StaffRole.MANAGER)
    role_group = Group.objects.get(name=StaffRole.MANAGER)
    kitchen = Group.objects.create(name="kitchen")

    # a role name in another case passes the unique name, and is refused as a role all the same
    admin_client.post("/admin/auth/group/add/", {"name": "Restaurant_Operator"})
    admin_client.post(f"/admin/auth/group/{kitchen.pk}/change/", {"name": "Restaurant_Manager"})
    assert admin_client.post(f"/admin/auth/group/{role_group.pk}/change/", {"name": "renamed"}).status_code == 403
    assert admin_client.post(f"/admin/auth/group/{role_group.pk}/delete/", {"post": "yes"}).status_code == 403
    admin_client.post(
        "/admin/auth/group/", {"action": "delete_selected", "_selected_action": [role_group.pk], "post": "yes"}
    )

    assert not Group.objects.filter(name="Restaurant_Operator").exists()
    kitchen.refresh_from_db()
    assert kitchen.name == "kitchen"
    assert Group.objects.filter(pk=role_group.pk, name=StaffRole.MANAGER.value).exists()
    assert staff_role(manager) == StaffRole.MANAGER

    # a group that is not a role is still the admin's to make
    admin_client.post("/admin/auth/group/add/", {"name": "bar"})
    assert Group.objects.filter(name="bar").exists()


def test_is_staff_and_model_permissions_are_not_edited_in_the_admin(admin_client, user):
    url = f"/admin/accounts/user/{user.pk}/change/"
    form = rendered_form(admin_client, url)
    form["is_staff"] = "on"
    form["user_permissions"] = [Permission.objects.get(codename="change_order").pk]
    admin_client.post(url, form)
    user.refresh_from_db()
    assert not user.is_staff
    assert not user.user_permissions.exists()


def test_a_status_change_needs_an_actor_with_the_orders_right(order, user, superuser):
    for actor in (None, user):
        with pytest.raises(CheckoutError) as refused:
            order_service.transition(order, "confirmed", changed_by=actor, expected_status="created")
        assert refused.value.code == "actor_not_authorized"
    order.refresh_from_db()
    assert order.status == "created"
    assert not OrderStatusHistory.objects.filter(order=order, to_status="confirmed").exists()

    operator = _staff("+79990000306")
    set_staff_role(actor=superuser, target=operator, role=StaffRole.OPERATOR)
    order_service.transition(order, "confirmed", changed_by=operator, expected_status="created")
    order.refresh_from_db()
    assert order.status == "confirmed"
