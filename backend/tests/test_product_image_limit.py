import io

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from PIL import Image

from menu.admin import PRODUCT_IMAGE_MAX_BYTES
from tests.admin_forms import rendered_form


def _photo(size: int) -> SimpleUploadedFile:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, "JPEG")
    head = buffer.getvalue()
    # decoders stop at the end of the jpeg, so the padding keeps it a valid photo of an exact size
    return SimpleUploadedFile(
        "photo.jpg", head + b"\0" * (size - len(head)), content_type="image/jpeg"
    )


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("size", "stored"),
    [(PRODUCT_IMAGE_MAX_BYTES, True), (PRODUCT_IMAGE_MAX_BYTES + 1, False)],
)
def test_a_product_photo_is_taken_up_to_the_limit_and_refused_past_it(
    admin_client, make_product, settings, tmp_path, size, stored
):
    settings.MEDIA_ROOT = tmp_path
    product = make_product()
    url = f"/admin/menu/product/{product.pk}/change/"
    form = rendered_form(admin_client, url)
    form["image"] = _photo(size)

    response = admin_client.post(url, form)

    product.refresh_from_db()
    assert bool(product.image) is stored
    if stored:
        assert response.status_code == 302
        assert product.image.size == size
    else:
        assert response.status_code == 200
        assert "Фото больше 5 МБ" in response.content.decode()
