"""createsuperuser held to the rule the site and the admin give a login by"""

from django.contrib.auth.management.commands import createsuperuser
from django.core.exceptions import ValidationError

from accounts import logins


class Command(createsuperuser.Command):
    def _validate_username(self, username, verbose_field_name, database):
        refused = super()._validate_username(username, verbose_field_name, database)
        if refused or not username:
            return refused
        try:
            logins.new_login(username)
        except ValidationError as exc:
            return "; ".join(exc.messages)
        return None
