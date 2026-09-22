import uuid

from django.core.management.base import BaseCommand, CommandError
from django.db import connection, transaction

from accounts import service as accounts_service
from accounts.models import User
from menu import inventory
from menu.models import Product
from orders import service as order_service
from orders.models import Order, OrderOutbox

LIVE_EVENT_TYPES = ["inventory.stock_changed", "identity.customer_changed", "order.created"]


class Command(BaseCommand):
    help = "Emit current product/customer/order state through the outbox: --snapshot for a reconciliation run, else live seeding."

    def add_arguments(self, parser) -> None:
        parser.add_argument("--snapshot", action="store_true", help="emit a fenced snapshot run (start/aggregates/completed)")
        parser.add_argument(
            "--force-live",
            action="store_true",
            help="re-run live seeding against an already-seeded projection (dead-letters events)",
        )

    def handle(self, *args, **options) -> None:
        if options["snapshot"]:
            self._snapshot_run()
            return

        already = OrderOutbox.objects.filter(snapshot=False, event_type__in=LIVE_EVENT_TYPES).exists()
        if already and not options["force_live"]:
            raise CommandError(
                "these aggregates were already emitted, so a live re-run would dead-letter every "
                "event at a version the projection already holds. Use --snapshot to reconcile an "
                "existing projection, or --force-live when seeding a known-empty one."
            )
        self._live()

    def _live(self) -> None:
        products = customers = orders = 0
        with transaction.atomic():
            for product in Product.objects.all().iterator():
                inventory.emit_state(product)
                products += 1
            for user in User.objects.exclude(customer_version=0).iterator():
                accounts_service.emit_state(user)
                customers += 1
            for order in Order.objects.iterator():
                order_service.emit_order_state(order)
                orders += 1
        self.stdout.write(self.style.SUCCESS(f"emitted live: {products} products, {customers} customers, {orders} orders"))

    def _snapshot_run(self) -> None:
        run_id = uuid.uuid4().hex
        products = customers = orders = 0
        with transaction.atomic():
            # a consistent source boundary: fix the read snapshot before the first scan so
            # concurrent inserts stay out of this run and are not later flagged as extra
            with connection.cursor() as cur:
                cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                cur.execute("SELECT transaction_timestamp()")
                as_of = cur.fetchone()[0]
            self._control(run_id, "started", as_of)
            for product in Product.objects.all().iterator():
                inventory.emit_state(product, snapshot=True, run_id=run_id)
                products += 1
            for user in User.objects.exclude(customer_version=0).iterator():
                accounts_service.emit_state(user, snapshot=True, run_id=run_id)
                customers += 1
            for order in Order.objects.iterator():
                order_service.emit_order_state(order, snapshot=True, run_id=run_id)
                orders += 1
            self._control(run_id, "completed", as_of, {"product": products, "customer": customers, "order": orders})
        self.stdout.write(
            self.style.SUCCESS(f"emitted snapshot run {run_id}: {products} products, {customers} customers, {orders} orders")
        )

    def _control(self, run_id: str, phase: str, as_of, counts: dict | None = None) -> None:
        OrderOutbox.objects.create(
            aggregate_type=OrderOutbox.AggregateType.SNAPSHOT,
            aggregate_id=run_id,
            aggregate_version=1,
            event_type="snapshot.control",
            routing_key="snapshot.control",
            snapshot_run_id=run_id,
            payload={"run_id": run_id, "phase": phase, "as_of": as_of.isoformat(), "counts": counts or {}},
        )
