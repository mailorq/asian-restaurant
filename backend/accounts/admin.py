from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group

from accounts import service as accounts_service
from accounts.models import User
from accounts.phone import to_e164
from accounts.roles import LEGACY_GROUP, StaffRole

_PROFILE_FIELDS = {"first_name", "phone"}
_STAFF_GROUPS = [*StaffRole.values, LEGACY_GROUP]


class CustomerChangeForm(UserChangeForm):
    def clean_phone(self):
        raw = self.cleaned_data.get("phone")
        if not raw:
            if self.instance.pk and self.instance.username == self.instance.phone:
                raise forms.ValidationError(
                    "Телефон служит логином этого аккаунта, очистить его нельзя"
                )
            return raw
        number = to_e164(raw)
        if number is None:
            raise forms.ValidationError("Некорректный номер телефона")
        if accounts_service.phone_owner(number, excluding=self.instance.pk) is not None:
            raise forms.ValidationError("Этот номер уже принадлежит другому аккаунту")
        return number


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    form = CustomerChangeForm
    fieldsets = UserAdmin.fieldsets + (("Contact", {"fields": ("phone", "customer_version", "authz_version")}),)
    list_display = ("username", "phone", "email", "is_staff", "is_active")
    search_fields = ("username", "phone", "email")
    readonly_fields = ("customer_version", "authz_version")

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        # staff roles are not editable here; they change only through set_staff_role
        if db_field.name == "groups":
            kwargs["queryset"] = Group.objects.exclude(name__in=_STAFF_GROUPS)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def save_model(self, request, obj, form, change):
        # name/phone, is_active and is_superuser are authorization/projection source state:
        # route each change through its service so a version bump + outbox event happen
        # atomically. is_superuser grants operations access, so it must revoke like a role
        changed = set(form.changed_data)
        db = User.objects.get(pk=obj.pk) if change else None
        route_profile = bool(db and (_PROFILE_FIELDS & changed))
        route_active = bool(db and "is_active" in changed)
        route_superuser = bool(db and "is_superuser" in changed)
        new_name, new_phone = obj.first_name, obj.phone
        new_active, new_superuser = obj.is_active, obj.is_superuser
        if route_profile:
            obj.first_name, obj.phone = db.first_name, db.phone
        if route_active:
            obj.is_active = db.is_active
        if route_superuser:
            obj.is_superuser = db.is_superuser
        super().save_model(request, obj, form, change)
        if route_profile:
            accounts_service.set_customer_profile(obj.pk, name=new_name, phone=new_phone)
        if route_active or route_superuser:
            from employee import service as employee_service
            if route_superuser:
                employee_service.set_superuser(actor=request.user, target=obj, is_superuser=new_superuser)
            if route_active:
                employee_service.set_active(actor=request.user, target=obj, active=new_active)
        if route_profile or route_active or route_superuser:
            obj.refresh_from_db()
        if not change:
            accounts_service.emit_state(obj)

    def save_related(self, request, form, formsets, change):
        # staff membership only changes via set_staff_role; read the pre-save membership from
        # the DB (never instance/self state) and restore it
        user = form.instance
        before = set()
        if user.pk:
            before = set(
                User.objects.get(pk=user.pk).groups.filter(name__in=_STAFF_GROUPS)
                .values_list("name", flat=True)
            )
        super().save_related(request, form, formsets, change)
        after = set(user.groups.filter(name__in=_STAFF_GROUPS).values_list("name", flat=True))
        if after != before:
            user.groups.remove(*Group.objects.filter(name__in=after - before))
            user.groups.add(*Group.objects.filter(name__in=before - after))
            messages.error(request, "Роль сотрудника меняется только через employee API, не в админке")

    def has_delete_permission(self, request, obj=None):
        # hard delete would leave an obsolete projection with no deletion event; deactivate instead
        return False
