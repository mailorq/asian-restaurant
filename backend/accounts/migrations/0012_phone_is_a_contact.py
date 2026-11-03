from django.db import migrations, models


def blank_for_null(apps, schema_editor):
    apps.get_model("accounts", "User").objects.filter(phone__isnull=True).update(phone="")


def null_for_blank(apps, schema_editor):
    # the unique column this goes back to holds any number of nulls but one empty string
    apps.get_model("accounts", "User").objects.filter(phone="").update(phone=None)


class Migration(migrations.Migration):
    # accounts keep their username, so one registered by number still logs in with it; only the codes go
    dependencies = [("accounts", "0011_phone_challenge")]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="phone",
            field=models.CharField(blank=True, max_length=16, null=True),
        ),
        migrations.RunPython(blank_for_null, null_for_blank),
        migrations.AlterField(
            model_name="user",
            name="phone",
            field=models.CharField(blank=True, max_length=16),
        ),
        migrations.DeleteModel(name="PhoneChallenge"),
    ]
