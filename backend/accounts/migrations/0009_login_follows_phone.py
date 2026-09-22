from django.db import migrations
from django.db.models import Q

from accounts.phone import to_e164


def align(apps, schema_editor):
    # a customer logs in with the username registration set to the number; this aligns accounts whose phone moved without it
    user_model = apps.get_model("accounts", "User")
    outbox = apps.get_model("orders", "OrderOutbox")
    unresolved = []
    for user in (
        user_model.objects.filter(username__startswith="+")
        .exclude(phone__isnull=True)
        .exclude(phone="")
    ):
        number = to_e164(user.phone)
        if number is None:
            unresolved.append(user.pk)
            continue
        if (user.username, user.phone) == (number, number):
            continue
        if (
            user_model.objects.exclude(pk=user.pk)
            .filter(Q(username=number) | Q(phone=number))
            .exists()
        ):
            unresolved.append(user.pk)
            continue
        phone_changed = user.phone != number
        user.username, user.phone = number, number
        if phone_changed:
            user.customer_version += 1
        user.save(update_fields=["username", "phone", "customer_version"])
        if phone_changed:
            outbox.objects.create(
                aggregate_type="customer",
                aggregate_id=str(user.pk),
                aggregate_version=user.customer_version,
                event_type="identity.customer_changed",
                routing_key="identity.customer_changed",
                payload={"customer_id": user.pk, "name": user.first_name or "", "phone": number},
            )
    if unresolved:
        raise RuntimeError(
            f"accounts whose login and phone disagree and cannot be aligned automatically: {unresolved}. "
            f"Give each a valid number no other account uses, as phone and as username, then migrate again"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0008_superuser_without_staff_groups"),
        ("orders", "0011_outbox_aggregate_type"),
    ]

    operations = [
        migrations.RunPython(align, migrations.RunPython.noop),
    ]
