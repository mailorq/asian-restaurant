from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.middleware.csrf import get_token
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts import jwt_service
from accounts import service as accounts_service
from accounts.models import User
from accounts.phone import to_e164
from accounts.schemas import LoginIn, MessageOut, RegisterIn, UserOut
from common.ratelimit import rate_limit

router = Router(tags=["auth"])


@router.post("/register", response=UserOut, auth=None)
@rate_limit("register", limit=10, window=60)
def register(request, data: RegisterIn):
    phone = to_e164(data.phone)
    if phone is None:
        raise HttpError(400, "Некорректный номер телефона")
    name = data.name.strip()
    if not name:
        raise HttpError(400, "Укажите имя")
    try:
        validate_password(data.password, User(username=phone, phone=phone, first_name=name))
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages)) from exc
    if User.objects.filter(phone=phone).exists():
        raise HttpError(400, "Этот номер уже зарегистрирован")
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=phone, phone=phone, password=data.password, first_name=name
            )
            accounts_service.emit_customer_created(user)
    except IntegrityError as exc:
        raise HttpError(400, "Этот номер уже зарегистрирован") from exc
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return user


@router.post("/login", response=UserOut, auth=None)
@rate_limit("login", limit=5, window=10)
def login_view(request, data: LoginIn):
    phone = to_e164(data.phone)
    user = authenticate(request, username=phone, password=data.password) if phone else None
    if user is None:
        raise HttpError(401, "Неверный телефон или пароль")
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


@router.post("/employee-token", auth=django_auth)
def employee_token(request):
    # a signed-in active staff member exchanges the session for a short-lived operations JWT
    try:
        token, ttl = jwt_service.issue_employee_token(request.user)
    except jwt_service.NotAuthorized as exc:
        raise HttpError(403, "Недостаточно прав") from exc
    return {"token": token, "token_type": "Bearer", "expires_in": ttl}
