from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "switch employees to order transitions through operations commands, or back with --off"

    def add_arguments(self, parser) -> None:
        parser.add_argument("usernames", nargs="+", help="username / phone of each employee")
        parser.add_argument("--off", action="store_true", help="back to the direct transition")

    def handle(self, *args, **options) -> None:
        users = get_user_model().objects.filter(username__in=options["usernames"])
        missing = sorted(set(options["usernames"]) - set(users.values_list("username", flat=True)))
        if missing:
            raise CommandError(f"пользователи не найдены: {', '.join(missing)}")
        users.update(transitions_via_commands=not options["off"])
        way = "direct" if options["off"] else "commands"
        self.stdout.write(
            self.style.SUCCESS(f"{users.count()} employee(s) now change order status by {way}")
        )
