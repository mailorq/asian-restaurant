"""text messages to a customer's phone, through the backend SMS_BACKEND names"""

import logging

from django.conf import settings
from django.utils.module_loading import import_string

log = logging.getLogger(__name__)


class SmsUnavailable(Exception):
    pass


class ConsoleSender:
    # development and ci: the message goes to the log, where whoever runs the stack reads the code
    available = True

    def send(self, phone: str, text: str) -> None:
        log.warning("sms to %s: %s", phone, text)


class DisabledSender:
    # production until a provider is chosen: nothing can be sent, so nothing can be confirmed
    available = False

    def send(self, phone: str, text: str) -> None:
        raise SmsUnavailable("no sms provider is configured")


def sender():
    return import_string(settings.SMS_BACKEND)()
