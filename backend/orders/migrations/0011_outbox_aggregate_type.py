from django.db import migrations, models

# the aggregate each stored event type belongs to; any other type is an order's
_TYPES = {
    "product": ["inventory.stock_changed"],
    "customer": ["identity.customer_changed"],
    "authz": ["identity.authz_changed"],
    "snapshot": ["snapshot.control"],
}


def backfill(apps, schema_editor):
    outbox = apps.get_model("orders", "OrderOutbox")
    for aggregate_type, event_types in _TYPES.items():
        outbox.objects.filter(event_type__in=event_types).update(aggregate_type=aggregate_type)


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0010_outbox_failed_status"),
    ]

    operations = [
        migrations.AddField(
            model_name="orderoutbox",
            name="aggregate_type",
            field=models.CharField(
                choices=[
                    ("order", "Order"),
                    ("product", "Product"),
                    ("customer", "Customer"),
                    ("authz", "Authz"),
                    ("snapshot", "Snapshot"),
                ],
                default="order",
                max_length=16,
            ),
            preserve_default=False,
        ),
        migrations.RunPython(backfill, migrations.RunPython.noop),
        migrations.AddIndex(
            model_name="orderoutbox",
            index=models.Index(
                condition=models.Q(("status", "pending")),
                fields=["aggregate_type", "aggregate_id", "id"],
                name="outbox_pending_aggregate_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="orderoutbox",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("aggregate_type__in", ["order", "product", "customer", "authz", "snapshot"])
                ),
                name="outbox_aggregate_type_known",
            ),
        ),
    ]
