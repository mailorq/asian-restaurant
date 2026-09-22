from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.db import transaction
from django.db.models import Q

from accounts.models import EmployeeRoleAudit, has_operations_role
from accounts.roles import LEGACY_GROUP, StaffRole, roles_of
from orders.models import OrderOutbox

AUTHZ_EVENT = "identity.authz_changed"
STAFF_GROUPS = [*StaffRole.values, LEGACY_GROUP]


def emit_authz(user) -> None:
    OrderOutbox.objects.create(
        aggregate_type=OrderOutbox.AggregateType.AUTHZ,
        aggregate_id=str(user.id),
        aggregate_version=user.authz_version,
        event_type=AUTHZ_EVENT,
        routing_key=AUTHZ_EVENT,
        # role_active stays for consumers that predate roles; the contract ignores unknown fields, so the two can travel together and no second event type is needed
        payload={"subject_id": user.id, "authz_version": user.authz_version,
                 "role_active": bool(roles_of(user)), "roles": roles_of(user),
                 "user_active": user.is_active},
    )


def _lock_privileged(actor, target_pk):
    """
    locks every row a privileged change may touch, in one query, by ascending pk

    the actor, the target and the whole set of active superusers are taken together: locking the target first and the rest afterwards is the reverse order for a second caller and deadlocks.
    the actor is re read here because an ORM object outlives the rights it was loaded with
    """
    from accounts.roles import NotAuthorized

    actor_pk = getattr(actor, "pk", None)
    wanted = [pk for pk in (actor_pk, target_pk) if pk is not None]
    locked = {
        user.pk: user
        for user in get_user_model().objects.select_for_update().order_by("pk").filter(
            Q(pk__in=wanted) | Q(is_superuser=True, is_active=True)
        )
    }
    fresh_actor = locked.get(actor_pk)
    if not (fresh_actor and fresh_actor.is_superuser and fresh_actor.is_active):
        raise NotAuthorized("only an active superuser may change privileged state")
    return locked


def _other_active_superusers(locked, exclude_pk) -> list:
    return [u for pk, u in locked.items() if u.is_superuser and u.is_active and pk != exclude_pk]


@transaction.atomic
def set_superuser(actor, target, is_superuser: bool):
    locked = _lock_privileged(actor, target.pk)
    user = locked[target.pk]
    if is_superuser:
        user.groups.remove(*Group.objects.filter(name__in=STAFF_GROUPS))
    if user.is_superuser == is_superuser:
        return user
    if not is_superuser and not _other_active_superusers(locked, user.pk):
        raise ValueError("нельзя снять права у последнего активного суперпользователя")
    user.is_superuser = is_superuser
    user.authz_version += 1
    user.save(update_fields=["is_superuser", "authz_version"])
    EmployeeRoleAudit.objects.create(
        actor=actor,
        target=user,
        action=EmployeeRoleAudit.Action.GRANT if is_superuser else EmployeeRoleAudit.Action.REVOKE,
    )
    emit_authz(user)
    return user


@transaction.atomic
def set_active(actor, target, active: bool):
    # is_active is authorization state: deactivating an operations-capable user must revoke
    # their access, so bump the version and emit — customers carry no operations access
    locked = _lock_privileged(actor, target.pk)
    user = locked[target.pk]
    if user.is_active == active:
        return user
    if not active and user.is_superuser and not _other_active_superusers(locked, user.pk):
        raise ValueError("нельзя деактивировать последнего активного суперпользователя")
    user.is_active = active
    if has_operations_role(user):
        user.authz_version += 1
        user.save(update_fields=["is_active", "authz_version"])
        emit_authz(user)
    else:
        user.save(update_fields=["is_active"])
    return user


def emit_authz_state(user) -> None:
    # re-publish current authorization state (idempotent at the same version downstream)
    emit_authz(user)
