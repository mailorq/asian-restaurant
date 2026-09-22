from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from menu import inventory
from menu.models import Product, StockAdjustment
from orders.models import Order

DEFAULT_QUANTITY = 50


class Command(BaseCommand):
    help = (
        "New installation only: overwrite the stock of every product. Refused once the shop has any order or "
        "stock history, and without --confirm."
    )

    def add_arguments(self, parser) -> None:
        parser.add_argument("--quantity", type=int, default=DEFAULT_QUANTITY)
        parser.add_argument(
            "--confirm", action="store_true", help="required: every product's stock is overwritten"
        )

    def handle(self, *args, quantity: int, confirm: bool, **options) -> None:
        if not confirm:
            raise CommandError(
                "this overwrites the stock of every product; pass --confirm on a new installation"
            )
        if quantity < 0:
            raise CommandError("--quantity must not be negative")
        with transaction.atomic():
            # checkout and stock corrections lock product rows in id order. holding all of them first means an
            # order committed before this point is seen by the check below, and one after it waits for the stock
            ids = list(Product.objects.select_for_update().order_by("id").values_list("id", flat=True))
            if Order.objects.exists() or StockAdjustment.objects.exists():
                raise CommandError(
                    "refused: the shop already has orders or stock history, set stock per product instead"
                )
            for product_id in ids:
                inventory.set_stock(product_id, quantity, reason="initial stock")
        self.stdout.write(
            self.style.SUCCESS(f"initial stock {quantity} set for {len(ids)} products")
        )
