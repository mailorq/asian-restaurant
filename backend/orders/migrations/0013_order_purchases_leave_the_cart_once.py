from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("orders", "0012_outbox_closed_rows_hold_their_aggregate"),
    ]

    # both columns keep a database default, so the previous release still inserts orders while this one migrates
    operations = [
        migrations.AddField(
            model_name="order",
            name="cart_clear_pending",
            field=models.BooleanField(db_default=False, default=False),
        ),
        migrations.AddField(
            model_name="order",
            name="checkout_fingerprint",
            field=models.CharField(blank=True, db_default="", default="", max_length=64),
        ),
    ]
