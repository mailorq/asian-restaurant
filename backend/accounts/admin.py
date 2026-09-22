from django import forms
from django.contrib import admin, messages
from django.contrib.auth.admin import UserAdmin
from django.contrib.auth.forms import UserChangeForm
from django.contrib.auth.models import Group

from accounts import service as accounts_service
from accounts.models import User
from accounts.phone import to_e164
from accounts.roles import LEGACY_GROUP, NotAuthorized, StaffRole
from common.forms import RenderedValuesForm
from employee import service as employee_service

_PROFILE_FIELDS = {"first_name", "phone"}
_ACCESS_FIELDS = {"is_active", "is_superuser"}
_ROW_FIELDS = frozenset(f.name for f in User._meta.concrete_fields)
_STAFF_GROUPS = [*StaffRole.values, LEGACY_GROUP]


class CustomerChangeForm(RenderedValuesForm, UserChangeForm):
    # the authorization version the form was rendered at: access is changed only against it, so a
    # revocation or grant made while the form was open is reported, not undone
    expected_authz_version = forms.IntegerField(widget=forms.HiddenInput, required=False)
    actor = None

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            self.fields["expected_authz_version"].initial = self.instance.authz_version

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and _ACCESS_FIELDS.intersection(self.changed_data):
            # the same rows in the same order as the access services take them, and the admin view runs in one
            # transaction, so the check holds until save_model has applied the change
            try:
                locked = employee_service.lock_privileged(self.actor, self.instance.pk)
            except NotAuthorized as exc:
                raise forms.ValidationError(
                    "Доступ пользователя меняет только активный суперпользователь"
                ) from exc
            if locked[self.instance.pk].authz_version != cleaned.get("expected_authz_version"):
                raise forms.ValidationError(
                    "Пользователь изменился, пока форма была открыта: права или роль уже другие. "
                    "Обновите страницу и внесите правку заново."
                )
        return cleaned

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

    def get_readonly_fields(self, request, obj=None):
        fields = super().get_readonly_fields(request, obj)
        # an account whose login is its number moves the login only together with the phone
        if obj is not None and obj.username == obj.phone:
            return (*fields, "username")
        return fields

    def formfield_for_manytomany(self, db_field, request, **kwargs):
        # staff roles are not editable here; they change only through set_staff_role
        if db_field.name == "groups":
            kwargs["queryset"] = Group.objects.exclude(name__in=_STAFF_GROUPS)
        return super().formfield_for_manytomany(db_field, request, **kwargs)

    def get_form(self, request, obj=None, **kwargs):
        form = super().get_form(request, obj, **kwargs)
        form.actor = request.user
        return form

    def save_model(self, request, obj, form, change):
        if not change:
            super().save_model(request, obj, form, change)
            accounts_service.emit_state(obj)
            return
        # obj may be older than the row, so only what the admin edited leaves it. profile and access go
        # through their services, which version the change and emit it
        edited = set(form.changed_data)
        user = User.objects.select_for_update().get(pk=obj.pk)
        plain = edited & (_ROW_FIELDS - _PROFILE_FIELDS - _ACCESS_FIELDS)
        if user.username == user.phone:
            plain.discard("username")
        if plain:
            for name in plain:
                setattr(user, name, getattr(obj, name))
            user.save(update_fields=sorted(plain))
        if _PROFILE_FIELDS & edited:
            accounts_service.set_customer_profile(
                obj.pk,
                name=obj.first_name if "first_name" in edited else None,
                phone=obj.phone if "phone" in edited else None,
            )
        if "is_superuser" in edited:
            employee_service.set_superuser(
                actor=request.user, target=user, is_superuser=obj.is_superuser
            )
        if "is_active" in edited:
            employee_service.set_active(actor=request.user, target=user, active=obj.is_active)
        obj.refresh_from_db()

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
        if change:
            form.save_edited_m2m()
        else:
            form.save_m2m()
        for formset in formsets:
            self.save_formset(request, form, formset, change=change)
        after = set(user.groups.filter(name__in=_STAFF_GROUPS).values_list("name", flat=True))
        if after != before:
            user.groups.remove(*Group.objects.filter(name__in=after - before))
            user.groups.add(*Group.objects.filter(name__in=before - after))
            messages.error(request, "Роль сотрудника меняется только через employee API, не в админке")

    def has_delete_permission(self, request, obj=None):
        # hard delete would leave an obsolete projection with no deletion event; deactivate instead
        return False
