import uuid

import django.db.models.deletion
from django.db import migrations, models


def _forbid_legacy_rows(apps, schema_editor):
    OperationCommand = apps.get_model("operations", "OperationCommand")
    if OperationCommand.objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError(
            "operations.0009 adds a required unique request_event_id; pre-existing commands "
            "carry no such id. Archive or delete them first (check they hold nothing of value)."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0008_operationcommand_command_plane"),
    ]

    operations = [
        migrations.RunPython(_forbid_legacy_rows, migrations.RunPython.noop),
        migrations.AddField(
            model_name="operationcommand",
            name="request_event_id",
            field=models.UUIDField(default=uuid.uuid4, unique=True),
            preserve_default=False,
        ),
        migrations.AddField(
            model_name="operationsoutbox",
            name="causation_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="operationsoutbox",
            name="command",
            field=models.OneToOneField(
                blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL,
                related_name="outbox_event", to="operations.operationcommand",
            ),
        ),
        migrations.AddField(
            model_name="operationsoutbox",
            name="producer",
            field=models.CharField(default="operations", max_length=32),
        ),
        migrations.AddField(
            model_name="operationsoutbox",
            name="aggregate_id",
            field=models.CharField(default="", max_length=64),
        ),
        migrations.AddField(
            model_name="operationsoutbox",
            name="aggregate_version",
            field=models.PositiveIntegerField(default=1),
        ),
    ]
