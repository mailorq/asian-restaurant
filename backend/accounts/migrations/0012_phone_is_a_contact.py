from django.db import migrations, models
from django.db.models import Count


def blank_for_null(apps, schema_editor):
    apps.get_model("accounts", "User").objects.filter(phone__isnull=True).update(phone="")


def null_for_blank(apps, schema_editor):
    user_model = apps.get_model("accounts", "User")
    # going back makes the number unique again, and since this migration two accounts may give the same one:
    # which of them keeps it is not for a migration to decide, so it stops and names them instead
    shared = (
        user_model.objects.exclude(phone="")
        .values("phone")
        .annotate(n=Count("pk"))
        .filter(n__gt=1)
        .values_list("phone", flat=True)
    )
    if shared:
        holders = sorted(
            user_model.objects.filter(phone__in=list(shared)).values_list("pk", flat=True)
        )
        raise RuntimeError(
            f"accounts that share a phone number, which the earlier schema keeps unique: {holders}. "
            f"Leave the number on one account of each group and clear or change it on the others, then migrate back again"
        )
    # the unique column this goes back to holds any number of nulls but one empty string
    user_model.objects.filter(phone="").update(phone=None)


class Migration(migrations.Migration):
    # accounts keep their username, so one registered by number still logs in with it
    dependencies = [("accounts", "0011_phone_challenge")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="phone",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
        migrations.RunPython(blank_for_null, null_for_blank),
        # only the model makes the number a plain string and lets go of the codes: the release before still writes
        # null for an account without a number and reads and writes the codes while this one migrates, so a later
        # release sets the column not null and drops the table once no process of that one can run
        migrations.SeparateDatabaseAndState(
            state_operations=[
                migrations.AlterField(
                    model_name="user",
                    name="phone",
                    field=models.CharField(blank=True, max_length=16),
                ),
                migrations.DeleteModel(name="PhoneChallenge"),
            ],
        ),
    ]
