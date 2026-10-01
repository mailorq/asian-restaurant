"""one-time codes that prove a phone, for registration and for recovery

a start is recorded and limited the same way whether or not the number has an account, so neither the
answer nor the limits tell an outsider which numbers are registered
"""

import hashlib
import hmac
import secrets
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from accounts import sms
from accounts.models import PhoneChallenge

CODE_TTL = timedelta(minutes=10)
RESEND_AFTER = timedelta(seconds=60)
DAILY_CODES = 5
MAX_ATTEMPTS = 5


class TooSoon(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = retry_after
        super().__init__(f"a new code in {retry_after}s")


class TooMany(Exception):
    pass


def _digest(phone: str, purpose: str, code: str) -> str:
    message = f"{purpose}:{phone}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), message, hashlib.sha256).hexdigest()


def start(phone: str, purpose: str, text) -> None:
    """records a new code for the phone and sends text(code); a text of None sends nothing"""
    sender = sms.sender()
    if not sender.available:
        raise sms.SmsUnavailable("no sms provider is configured")
    now = timezone.now()
    recent = list(
        PhoneChallenge.objects.filter(phone=phone, purpose=purpose, created_at__gte=now - timedelta(days=1))
        .order_by("-created_at")
        .values_list("created_at", flat=True)
    )
    if recent and now - recent[0] < RESEND_AFTER:
        raise TooSoon(int((RESEND_AFTER - (now - recent[0])).total_seconds()) + 1)
    if len(recent) >= DAILY_CODES:
        raise TooMany()

    code = f"{secrets.randbelow(10**6):06d}"
    with transaction.atomic():
        PhoneChallenge.objects.filter(phone=phone, created_at__lt=now - timedelta(days=1)).delete()
        # only the latest code of a phone works
        PhoneChallenge.objects.filter(phone=phone, purpose=purpose, used_at__isnull=True).update(used_at=now)
        challenge = PhoneChallenge.objects.create(
            phone=phone,
            purpose=purpose,
            code_hash=_digest(phone, purpose, code),
            expires_at=now + CODE_TTL,
        )
    message = text(code)
    if message is None:
        return
    try:
        # after the commit: a provider is an external call, and no transaction waits on it
        sender.send(phone, message)
    except Exception as exc:
        PhoneChallenge.objects.filter(pk=challenge.pk).delete()
        raise sms.SmsUnavailable(str(exc)) from exc


def consume(phone: str, purpose: str, code: str) -> bool:
    """true once, for the latest live code of the phone; every try counts, the failed ones too"""
    now = timezone.now()
    with transaction.atomic():
        challenge = (
            PhoneChallenge.objects.select_for_update()
            .filter(phone=phone, purpose=purpose, used_at__isnull=True, expires_at__gt=now)
            .order_by("-created_at")
            .first()
        )
        if challenge is None or challenge.attempts >= MAX_ATTEMPTS:
            return False
        challenge.attempts += 1
        matched = hmac.compare_digest(challenge.code_hash, _digest(phone, purpose, code.strip()))
        if matched:
            challenge.used_at = now
        challenge.save(update_fields=["attempts", "used_at"])
    return matched
