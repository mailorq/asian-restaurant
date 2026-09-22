from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0011_outbox_aggregate_type"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="orderoutbox",
            name="outbox_pending_aggregate_idx",
        ),
        migrations.AlterField(
            model_name="orderoutbox",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "В очереди"),
                    ("published", "Отправлено"),
                    ("failed", "Снято оператором"),
                    ("superseded", "Заменено состоянием"),
                ],
                db_index=True,
                default="pending",
                max_length=12,
            ),
        ),
        migrations.AddIndex(
            model_name="orderoutbox",
            index=models.Index(
                condition=models.Q(("status__in", ["pending", "failed"])),
                fields=["aggregate_type", "aggregate_id", "id"],
                name="outbox_unsent_aggregate_idx",
            ),
        ),
    ]
