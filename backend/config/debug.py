import re

from django.views.debug import SafeExceptionReporterFilter

_CREDENTIALS = re.compile(r"(?P<scheme>\b[a-z][a-z0-9+.-]*://)[^\s/@]+@", re.IGNORECASE)


class CredentialsHiddenFilter(SafeExceptionReporterFilter):
    """
    the debug page hides settings and environment by name, and a connection url is named for what it reaches,
    not for the password inside it (RABBITMQ_URL, CART_REDIS_URL), so credentials in any url are hidden by value
    """

    def cleanse_setting(self, key, value):
        value = super().cleanse_setting(key, value)
        if isinstance(value, str):
            return _CREDENTIALS.sub(lambda m: f"{m.group('scheme')}{self.cleansed_substitute}@", value)
        return value
