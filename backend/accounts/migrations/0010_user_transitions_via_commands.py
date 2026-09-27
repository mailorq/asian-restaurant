from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0009_login_follows_phone")]

    operations = [
        migrations.AddField(
            model_name="user",
            name="transitions_via_commands",
            field=models.BooleanField(default=False),
        ),
    ]
