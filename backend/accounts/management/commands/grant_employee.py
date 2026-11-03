from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from accounts.roles import InvalidRoleTarget, NotAuthorized, StaffRole, set_staff_role


class Command(BaseCommand):
    help = "assign or clear a staff role for a user (server access = superuser)."

    def add_arguments(self, parser) -> None:
        parser.add_argument("username", help="login of the target user")
        parser.add_argument("--role", choices=sorted(StaffRole.values), default=None,
                            help="omit to clear every staff role")
        parser.add_argument("--actor", required=True,
                            help="username of the active superuser taking responsibility")

    def handle(self, *args, **options) -> None:
        user_model = get_user_model()
        try:
            user = user_model.objects.get(username=options["username"])
        except user_model.DoesNotExist:
            raise CommandError("пользователь не найден") from None
        try:
            actor = user_model.objects.get(username=options["actor"])
        except user_model.DoesNotExist:
            raise CommandError("actor not found") from None
        try:
            set_staff_role(actor=actor, target=user, role=options["role"])
        except (NotAuthorized, InvalidRoleTarget) as exc:
            raise CommandError(str(exc)) from exc
        held = options["role"] or "none"
        self.stdout.write(self.style.SUCCESS(f"staff role of {user.username} is now {held}"))
