from django.db import migrations, models
from django.db.models import Count
from django.db.models.functions import Lower

import accounts.logins


def refuse_case_twins(apps, schema_editor):
    user_model = apps.get_model("accounts", "User")
    twins = (
        user_model.objects.annotate(login=Lower("username"))
        .values("login")
        .annotate(n=Count("pk"))
        .filter(n__gt=1)
        .values_list("login", flat=True)
    )
    if twins:
        holders = sorted(
            user_model.objects.annotate(login=Lower("username"))
            .filter(login__in=list(twins))
            .values_list("pk", flat=True)
        )
        raise RuntimeError(
            f"accounts whose logins differ only in case, so sign in cannot tell them apart: {holders}. "
            f"Give all but one of each group another login, then migrate again"
        )


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0012_phone_is_a_contact"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.AlterField(
            model_name="user",
            name="username",
            field=models.CharField(
                error_messages={"unique": "Этот логин уже занят, выберите другой"},
                max_length=150,
                unique=True,
                validators=[accounts.logins.validate_login_shape],
                verbose_name="username",
            ),
        ),
        migrations.RunPython(refuse_case_twins, migrations.RunPython.noop),
        migrations.AddConstraint(
            model_name="user",
            constraint=models.UniqueConstraint(
                Lower("username"),
                name="accounts_user_login_ci_unique",
                violation_error_message="Этот логин уже занят, выберите другой",
            ),
        ),
    ]
