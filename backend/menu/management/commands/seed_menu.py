import importlib.util
import shutil
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import transaction

from menu import inventory
from menu.models import Ingredient, Product

SEED_DIR = Path(settings.BASE_DIR) / "seed"
SOURCE_IMAGES = SEED_DIR / "images" / "optimized"


def _load_products() -> list[dict]:
    path = SEED_DIR / "products_data.py"
    spec = importlib.util.spec_from_file_location("products_data", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.PRODUCTS_DATA


def _split_allergens(description: str) -> tuple[str, str]:
    marker = "Аллергены:"
    if marker not in description:
        return description.strip(), ""
    desc, allergens = description.split(marker, 1)
    allergens = allergens.strip()
    if allergens.lower().startswith("отсутств"):
        allergens = ""
    return desc.strip(), allergens


class Command(BaseCommand):
    help = (
        "Create the catalog products from backend/seed/products_data.py that do not exist yet, with no stock. "
        "Existing products, their stock, prices and images are never changed; stock is set by seed_initial_stock "
        "on a new installation and by employees afterwards."
    )

    def handle(self, *args, **options) -> None:
        media_products = Path(settings.MEDIA_ROOT) / "products"
        media_products.mkdir(parents=True, exist_ok=True)
        created = kept = 0

        for row in _load_products():
            basename = Path(row["image"]).name
            code = Path(basename).stem  # dish_1, drink_4, dessert_2
            source, target = SOURCE_IMAGES / basename, media_products / basename
            if source.exists() and not target.exists():
                shutil.copy2(source, target)

            description, allergens = _split_allergens(row["description"])
            with transaction.atomic():
                product, is_new = Product.objects.get_or_create(
                    code=code,
                    defaults={
                        "category": row["category"],
                        "name": row["name"],
                        "description": description,
                        "allergens": allergens,
                        "price": row["price"],
                        "image": row["image"],
                        "is_active": row["is_active"],
                        "is_featured": row["is_featured"],
                        "stock_quantity": 0,
                    },
                )
                if is_new:
                    product.ingredients.set(
                        Ingredient.objects.get_or_create(name=name)[0]
                        for name in row["ingredients"]
                    )
                    inventory.emit_state(product)
            created += int(is_new)
            kept += int(not is_new)

        self.stdout.write(self.style.SUCCESS(f"seeded: {created} new, {kept} left as they are"))
