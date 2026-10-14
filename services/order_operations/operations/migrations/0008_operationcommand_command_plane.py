import uuid

from django.db import migrations, models


def _forbid_legacy_rows(apps, schema_editor):
    # 0008 makes actor_id + idempotency_key required and adds a unique constraint. Rows written
    # before the command plane carry no valid values for them, so rather than backfill invalid
    # placeholders (actor_id=0 / idempotency_key="") that would also collide on the new unique
    # constraint, refuse to migrate and make the operator archive/delete them first.
    OperationCommand = apps.get_model("operations", "OperationCommand")
    if OperationCommand.objects.using(schema_editor.connection.alias).exists():
        raise RuntimeError(
            "operations.0008 cannot add required actor_id / idempotency_key to existing "
            "OperationCommand rows without inventing invalid data. Archive or delete the "
            "pre-command-plane rows first (check they hold nothing of value), then re-run."
        )


class Migration(migrations.Migration):

    dependencies = [
        ("operations", "0007_employee_authorization"),
    ]

    operations = [
        # preflight before any schema change: the required fields below have no default, so this
        # only succeeds on an empty table (see _forbid_legacy_rows)
        migrations.RunPython(_forbid_legacy_rows, migrations.RunPython.noop),
        migrations.AddField(
            model_name="operationcommand",
            name="correlation_id",
            field=models.UUIDField(default=uuid.uuid4, editable=False),
        ),
        migrations.AddField(
            model_name="operationcommand",
            name="actor_id",
            field=models.PositiveBigIntegerField(),
        ),
        migrations.AddField(
            model_name="operationcommand",
            name="idempotency_key",
            field=models.CharField(max_length=200),
        ),
        migrations.AddField(
            model_name="operationcommand",
            name="deadline_at",
            field=models.DateTimeField(blank=True, db_index=True, null=True),
        ),
        migrations.AlterField(
            model_name="operationcommand",
            name="status",
            field=models.CharField(
                choices=[
                    ("pending", "Pending"),
                    ("dispatched", "Dispatched"),
                    ("timed_out", "Timed out"),
                    ("dispatch_failed", "Dispatch failed"),
                    ("succeeded", "Succeeded"),
                    ("rejected", "Rejected"),
                ],
                db_index=True,
                default="pending",
                max_length=16,
            ),
        ),
        migrations.AddConstraint(
            model_name="operationcommand",
            constraint=models.UniqueConstraint(
                fields=("actor_id", "command_type", "target", "idempotency_key"),
                name="uniq_operation_command_idempotency",
            ),
        ),
    ]
