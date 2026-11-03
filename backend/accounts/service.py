from django.db import transaction

from accounts.models import User
from accounts.phone import to_e164
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
        # an empty value clears the contact
        number = to_e164(phone) if phone else ""
        if number is None:
            raise InvalidPhone(phone)
        # a contact for the courier: the login stays where it is, even one that was this number
        if number != user.phone:
            user.phone = number
            changed.add("phone")
    if changed:
        user.customer_version += 1
        user.save(update_fields=[*changed, "customer_version"])
        _emit(user)
    return user


def emit_state(user: User, *, snapshot: bool = False, run_id: str = "") -> None:
    _emit(user, snapshot=snapshot, run_id=run_id)
