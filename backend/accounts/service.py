from django.db import transaction
from django.db.models import Q

from accounts import challenges
from accounts.models import PhoneChallenge, User
from accounts.phone import to_e164
from accounts.roles import is_staff_member
from orders.models import OrderOutbox

CUSTOMER_EVENT = "identity.customer_changed"


def _emit(user: User, *, snapshot: bool = False, run_id: str = "") -> None:
    OrderOutbox.objects.create(
        aggregate_type=OrderOutbox.AggregateType.CUSTOMER,
        aggregate_id=str(user.id),
        aggregate_version=user.customer_version,
        event_type=CUSTOMER_EVENT,
        routing_key=CUSTOMER_EVENT,
        snapshot=snapshot,
        snapshot_run_id=run_id,
        payload={"customer_id": user.id, "name": user.first_name or "", "phone": user.phone or ""},
    )


def emit_customer_created(user: User) -> None:
    # called inside the registration transaction; initial customer state is v1
    _emit(user)


class InvalidPhone(ValueError):
    pass


class PhoneTaken(ValueError):
    pass


def phone_owner(number: str, *, excluding: int | None = None) -> User | None:
    # a number belongs to whoever has it as a phone or logs in with it
    return User.objects.exclude(pk=excluding).filter(Q(phone=number) | Q(username=number)).first()


def recoverable_by_sms(user: User | None) -> bool:
    # a staff account is not handed over to whoever holds its phone: an administrator resets it
    return bool(
        user and user.is_active and not (user.is_staff or user.is_superuser or is_staff_member(user))
    )


def reset_password_with_code(number: str, code: str, password: str) -> User | None:
    """the customer holding the number gets the password, once per live recovery code, else None"""
    owner = phone_owner(number)
    with transaction.atomic():
        # the row a role grant, a deactivation and a phone change lock first: whichever of them committed
        # before this is what the account is judged by, and none of them lands between the judgement and the write
        user = User.objects.select_for_update().filter(pk=owner.pk).first() if owner else None
        spent = challenges.consume(number, PhoneChallenge.Purpose.RESET, code)
        if not (spent and recoverable_by_sms(user) and number in (user.phone, user.username)):
            # returned, not raised: the try just counted commits with the rest
            return None
        user.set_password(password)
        user.save(update_fields=["password"])
    return user


@transaction.atomic
def set_customer_profile(user_id: int, *, name: str | None = None, phone: str | None = None) -> User | None:
    user = User.objects.select_for_update().filter(id=user_id).first()
    if user is None:
        return None
    changed = set()
    if name is not None and name != user.first_name:
        user.first_name = name
        changed.add("first_name")
    if phone is not None:
        number = to_e164(phone)
        if number is None:
            raise InvalidPhone(phone)
        if number != user.phone:
            if phone_owner(number, excluding=user.pk) is not None:
                raise PhoneTaken(number)
            # a customer logs in with the number, so the login moves with it
            if user.username == user.phone:
                user.username = number
                changed.add("username")
            user.phone = number
            changed.add("phone")
    if changed:
        user.customer_version += 1
        user.save(update_fields=[*changed, "customer_version"])
        _emit(user)
    return user


def emit_state(user: User, *, snapshot: bool = False, run_id: str = "") -> None:
    _emit(user, snapshot=snapshot, run_id=run_id)
