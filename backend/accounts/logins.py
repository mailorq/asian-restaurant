import re

from django.core.exceptions import ValidationError

# a login starts with a letter, so it never reads as a phone number
LOGIN = re.compile(r"[a-z][a-z0-9_.-]{2,31}", re.IGNORECASE)
# the login of an account registered by number before logins existed: the number exactly as a typed one normalizes
PHONE_ERA = re.compile(r"\+[1-9]\d{6,14}")
RULE = "Логин: от 3 до 32 латинских букв, цифр и знаков _ . -, начинается с буквы"
TAKEN = "Этот логин уже занят, выберите другой"


def validate_login_shape(value: str) -> None:
    """what any stored login may look like; sign in finds each of these shapes one way only"""
    if not (LOGIN.fullmatch(value) or PHONE_ERA.fullmatch(value)):
        raise ValidationError(RULE, code="invalid")


def new_login(raw: str, *, excluding: int | None = None) -> str:
    """a login given now, from the site or the admin: the rule, in lower case, free whatever its case"""
    from accounts.models import User

    login = raw.strip().lower()
    if not LOGIN.fullmatch(login):
        raise ValidationError(RULE, code="invalid")
    if User.objects.exclude(pk=excluding).filter(username__iexact=login).exists():
        raise ValidationError(TAKEN, code="taken")
    return login
