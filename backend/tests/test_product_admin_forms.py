import pytest

from menu import inventory
from tests.admin_forms import rendered_form


@pytest.mark.django_db
def test_a_form_opened_before_another_edit_writes_back_only_what_its_admin_changed(
    admin_client, make_product
):
    product = make_product(price="100.00", stock=10)
    url = f"/admin/menu/product/{product.pk}/change/"
    form = rendered_form(admin_client, url)
    inventory.update_product(
        product.id, {"price": "120.00", "is_active": False}, reason="another admin"
    )

    form["description"] = "новое описание"
    response = admin_client.post(url, form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert (product.description, str(product.price), product.is_active) == (
        "новое описание",
        "120.00",
        False,
    )


@pytest.mark.django_db
def test_a_product_list_opened_before_another_edit_writes_back_only_what_was_edited(
    admin_client, make_product
):
    product = make_product(price="100.00", stock=10)
    form = rendered_form(admin_client, "/admin/menu/product/")
    inventory.update_product(product.id, {"is_active": False}, reason="another admin")

    form["form-0-price"] = "130.00"
    form["_save"] = "Save"
    response = admin_client.post("/admin/menu/product/", form)

    assert response.status_code == 302
    product.refresh_from_db()
    assert (str(product.price), product.is_active) == ("130.00", False)
