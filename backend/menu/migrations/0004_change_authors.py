import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def sources_of_earlier_rows(apps, schema_editor):
    # the writers of the stock named their reasons this way before the source was recorded
    rows = apps.get_model("menu", "StockAdjustment").objects.filter(source="")
    rows.filter(reason__startswith="order #").update(source="sale")
    rows.filter(reason__startswith="cancelled order #").update(source="cancellation")
    rows.filter(staff__isnull=False).update(source="staff")
    rows.update(source="system")


class Migration(migrations.Migration):
    dependencies = [
        ("menu", "0003_product_version"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    # only additions, with a database default for a row of the release before: it writes stock while this one migrates
    operations = [
        migrations.AddField(
            model_name="stockadjustment",
            name="source",
            field=models.CharField(
                blank=True,
                choices=[
                    ("staff", "Сотрудник"),
                    ("sale", "Продажа"),
                    ("cancellation", "Отмена заказа"),
                    ("system", "Система"),
                ],
                db_default="",
                max_length=16,
            ),
        ),
        migrations.RunPython(sources_of_earlier_rows, migrations.RunPython.noop),
        migrations.CreateModel(
            name="ProductChange",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                ("field", models.CharField(max_length=32)),
                ("old_value", models.TextField(blank=True)),
                ("new_value", models.TextField(blank=True)),
                ("reason", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "product",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="changes",
                        to="menu.product",
                    ),
                ),
                (
                    "staff",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="+",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at", "-id"],
                "indexes": [
                    models.Index(
                        fields=["product", "field", "-created_at"],
                        name="menu_produc_product_3d587a_idx",
                    )
                ],
            },
        ),
    ]
