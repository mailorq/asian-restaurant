from django.contrib import admin
from django.contrib.admin.forms import AdminAuthenticationForm
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError


def _superuser(user) -> bool:
    return bool(user.is_active and user.is_superuser)


class SuperuserLoginForm(AdminAuthenticationForm):
    def confirm_login_allowed(self, user):
        AuthenticationForm.confirm_login_allowed(self, user)
        if not _superuser(user):
            raise ValidationError(
                self.error_messages["invalid_login"],
                code="invalid_login",
                params={"username": self.username_field.verbose_name},
            )


class SuperuserAdminSite(admin.AdminSite):
    """
    the admin belongs to active superusers only

    staff work in their panel, where every action checks the role it needs. model permissions and is_staff
    would be a second way to the same orders, stock and accounts that no role, audit or authorization
    version follows, so none of them opens the admin, its pages, actions or direct urls
    """

    login_form = SuperuserLoginForm

    def has_permission(self, request):
        return _superuser(request.user)
