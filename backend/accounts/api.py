from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.middleware.csrf import get_token
from ninja import Router
from ninja.errors import HttpError
from ninja.security import django_auth

from accounts import challenges, jwt_service, sms
from accounts import service as accounts_service
from accounts.models import PhoneChallenge, User
from accounts.phone import to_e164
from accounts.schemas import LoginIn, MessageOut, PhoneIn, RegisterIn, ResetIn, UserOut
from common.ratelimit import rate_limit

router = Router(tags=["auth"])

# the same answer whatever the number is: it never tells whether an account holds it
CODE_SENT = "Код отправлен в SMS"
WRONG_CODE = "Неверный или просроченный код"
ALREADY_REGISTERED = "Этот номер уже зарегистрирован в Asian Restaurant. Войдите или восстановите пароль."


def _phone(raw: str) -> str:
    phone = to_e164(raw)
    if phone is None:
        raise HttpError(400, "Некорректный номер телефона")
    return phone


def _password(password: str, phone: str, name: str = "") -> None:
    # judged against what the request carries, never against an account, so it answers the same either way
    try:
        validate_password(password, User(username=phone, phone=phone, first_name=name))
    except ValidationError as exc:
        raise HttpError(400, " ".join(exc.messages)) from exc


def _send_code(phone: str, purpose: str, text) -> dict:
    try:
        challenges.start(phone, purpose, text)
    except challenges.TooSoon as exc:
        raise HttpError(429, f"Код уже отправлен, новый можно запросить через {exc.retry_after} с") from exc
    except challenges.TooMany as exc:
        raise HttpError(429, "Слишком много кодов на этот номер, попробуйте завтра") from exc
    except sms.SmsUnavailable as exc:
        raise HttpError(503, "Отправка SMS временно недоступна") from exc
    return {"detail": CODE_SENT}


@router.post("/register/code", response=MessageOut, auth=None)
@rate_limit("register_code", limit=5, window=600)
def register_code(request, data: PhoneIn):
    phone = _phone(data.phone)
    if accounts_service.phone_owner(phone) is not None:
        # its owner learns of the attempt, the caller gets the answer a free number gets
        return _send_code(phone, PhoneChallenge.Purpose.REGISTER, lambda code: ALREADY_REGISTERED)
    return _send_code(
        phone,
        PhoneChallenge.Purpose.REGISTER,
        lambda code: f"Код регистрации в Asian Restaurant: {code}. Никому его не сообщайте.",
    )


@router.post("/register", response=UserOut, auth=None)
@rate_limit("register", limit=10, window=60)
def register(request, data: RegisterIn):
    phone = _phone(data.phone)
    name = data.name.strip()
    if not name:
        raise HttpError(400, "Укажите имя")
    _password(data.password, phone, name)
    if not challenges.consume(phone, PhoneChallenge.Purpose.REGISTER, data.code):
        raise HttpError(400, WRONG_CODE)
    if accounts_service.phone_owner(phone) is not None:
        raise HttpError(400, WRONG_CODE)
    try:
        with transaction.atomic():
            user = User.objects.create_user(
                username=phone, phone=phone, password=data.password, first_name=name
            )
            accounts_service.emit_customer_created(user)
    except IntegrityError as exc:
        raise HttpError(400, WRONG_CODE) from exc
    login(request, user, backend="django.contrib.auth.backends.ModelBackend")
    return user


@router.post("/password/code", response=MessageOut, auth=None)
@rate_limit("password_code", limit=5, window=600)
def password_code(request, data: PhoneIn):
    phone = _phone(data.phone)
    if not accounts_service.recoverable_by_sms(accounts_service.phone_owner(phone)):
        return _send_code(phone, PhoneChallenge.Purpose.RESET, lambda code: None)
    return _send_code(
        phone,
        PhoneChallenge.Purpose.RESET,
        lambda code: f"Код восстановления доступа в Asian Restaurant: {code}. Никому его не сообщайте.",
    )


@router.post("/password/reset", response=UserOut, auth=None)
@rate_limit("password_reset", limit=10, window=600)
def password_reset(request, data: ResetIn):
    phone = _phone(data.phone)
    _password(data.password, phone)
    user = accounts_service.reset_password_with_code(phone, data.code, data.password)
    if user is None:
        raise HttpError(400, WRONG_CODE)
    # the new password changed the session hash, so every other session of the account has ended
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
