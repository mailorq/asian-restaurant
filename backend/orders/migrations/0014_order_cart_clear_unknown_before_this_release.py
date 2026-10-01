from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0013_order_purchases_leave_the_cart_once"),
    ]

    # no order placed before this release recorded whether its cart was cleared, so false would read as
    # cleared: they become unknown, and so does every order a previous release still inserts while this migrates
    operations = [
        migrations.RemoveField(
            model_name="order",
            name="checkout_fingerprint",
        ),
        migrations.AlterField(
            model_name="order",
            name="cart_clear_pending",
            field=models.BooleanField(null=True),
        ),
        migrations.RunSQL(
            "UPDATE orders_order SET cart_clear_pending = NULL WHERE NOT cart_clear_pending",
            "UPDATE orders_order SET cart_clear_pending = false WHERE cart_clear_pending IS NULL",
        ),
    ]
