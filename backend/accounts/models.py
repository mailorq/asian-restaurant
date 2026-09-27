from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

# staff who may access the /employee area
EMPLOYEE_GROUP = "restaurant_employee"


def has_operations_role(user) -> bool:
    # shared by token issuance, authz events and the backfill
    from accounts.roles import is_staff_member

    return is_staff_member(user)


class User(AbstractUser):
    # phone stored normalized to E.164, e.g. +380671234567
    phone = models.CharField(max_length=16, unique=True, blank=True, null=True)
    # initial state is v1; bumped on name/phone change so the projection can version-fence
    customer_version = models.PositiveIntegerField(default=1)
    # bumped to revoke outstanding staff tokens ahead of their TTL
    authz_version = models.PositiveIntegerField(default=1)
    # this employee changes order status only through operations commands; the direct endpoint and the admin actions refuse them
    transitions_via_commands = models.BooleanField(default=False)

    def __str__(self) -> str:
        return self.username


class EmployeeRoleAudit(models.Model):
    class Action(models.TextChoices):
        GRANT = "grant", "Выдана"
        REVOKE = "revoke", "Отозвана"
        CHANGE = "change", "Изменена"

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, related_name="+"
    )
    target = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="role_audits"
    )
    action = models.CharField(max_length=8, choices=Action.choices)
    from_role = models.CharField(max_length=32, blank=True)
    to_role = models.CharField(max_length=32, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.action} → {self.target_id}"
