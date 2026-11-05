import re

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.middleware.csrf import get_token
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts import jwt_service, logins
from accounts import service as accounts_service
from accounts.models import User
from accounts.phone import to_e164
from accounts.schemas import LoginIn, MessageOut, RegisterIn, UserOut
from common.ratelimit import rate_limit

router = Router(tags=["auth"])

# what a customer of the phone era types as their login, in any of the formats a number is written in
PHONE_TYPED = re.compile(r"\+?[\d\s()-]+")


def _login_of(raw: str) -> str:
    typed = raw.strip()
    if PHONE_TYPED.fullmatch(typed):
        return to_e164(typed) or typed
    # logins are unique whatever their case, so a differently typed case still finds the one
    found = User.objects.filter(username__iexact=typed).values_list("username", flat=True).first()
    return found or typed


@router.post("/register", response=UserOut, auth=None)
@rate_limit("register", limit=5, window=600)
def register(request, data: RegisterIn):
    try:
        username = logins.new_login(data.login)
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages)) from exc
    name = data.name.strip()
    if not name:
        raise HttpError(400, "Укажите имя")
    # the number is only where the courier calls: nobody confirms it, so it gives no claim to an account
    phone = to_e164(data.phone)
    if phone is None:
        raise HttpError(400, "Некорректный номер телефона")
    try:
        validate_password(data.password, User(username=username, phone=phone, first_name=name))
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages)) from exc
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=username, phone=phone, password=data.password, first_name=name
            )
            accounts_service.emit_customer_created(user)
    except IntegrityError as exc:
        # the same login registered in between: the case-insensitive unique index refused this one
        raise HttpError(400, logins.TAKEN) from exc
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return user


@router.post("/login", response=UserOut, auth=None)
@rate_limit("login", limit=5, window=10)
def login_view(request, data: LoginIn):
    user = authenticate(request, username=_login_of(data.login), password=data.password)
    if user is None:
        raise HttpError(401, "Неверный логин или пароль")
    login(request, user)
    return user


@router.get("/me", response=UserOut, auth=django_auth)
def me(request):
    return request.user


@router.post("/logout", response=MessageOut, auth=django_auth)
def logout_view(request):
    logout(request)
    return {"detail": "ok"}


@router.get("/csrf", response=MessageOut, auth=None)
def csrf(request):
    get_token(request)
    return {"detail": "ok"}


@router.get("/jwks", auth=None)
def jwks(request):
    return jwt_service.public_jwks()
