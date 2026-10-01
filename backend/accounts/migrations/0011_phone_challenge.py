from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("accounts", "0010_user_transitions_via_commands")]

    operations = [
        migrations.CreateModel(
            name="PhoneChallenge",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("phone", models.CharField(max_length=16)),
                (
                    "purpose",
                    models.CharField(
                        choices=[("register", "Регистрация"), ("reset", "Восстановление доступа")],
                        max_length=16,
                    ),
                ),
                ("code_hash", models.CharField(max_length=64)),
                ("attempts", models.PositiveSmallIntegerField(default=0)),
                ("expires_at", models.DateTimeField()),
                ("used_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["phone", "purpose", "-created_at"], name="accounts_ph_phone_46464a_idx")
                ],
            },
        ),
    ]
