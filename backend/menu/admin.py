from django import forms
from django.contrib import admin

from common.forms import RenderedValuesForm
from menu import inventory
from menu.models import Ingredient, Product, StockAdjustment

_ROW_FIELDS = frozenset(f.name for f in Product._meta.concrete_fields)


class ProductAdminForm(RenderedValuesForm, forms.ModelForm):
    # the version the form was rendered at: name and stock are written only against it, so a sale or edit made while it was open is reported, not overwritten
    expected_version = forms.IntegerField(widget=forms.HiddenInput, required=False)

    class Meta:
        model = Product
        fields = (
            "code",
            "category",
            "name",
            "description",
            "allergens",
            "price",
            "image",
            "ingredients",
            "stock_quantity",
            "is_active",
            "is_featured",
        )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and not self.is_bound:
            self.fields["expected_version"].initial = self.instance.version

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk and inventory.PROJECTED_FIELDS.intersection(self.changed_data):
            # the admin view runs in one transaction, so this lock is held until save_model has written
            current = (
                Product.objects.select_for_update()
                .filter(pk=self.instance.pk)
                .values_list("version", flat=True)
            )
            if cleaned.get("expected_version") != current.first():
                raise forms.ValidationError(
                    "Товар изменился, пока форма была открыта: остаток или название уже другие. "
                    "Обновите страницу и внесите правку заново."
                )
        return cleaned


class ProductChangelistForm(RenderedValuesForm, forms.ModelForm):
    class Meta:
        model = Product
        fields = ("price", "is_active", "is_featured")


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    list_display = ("code", "name", "category", "price", "stock_quantity", "is_active", "is_featured")
    list_filter = ("category", "is_active", "is_featured")
    list_editable = ("price", "is_active", "is_featured")
    search_fields = ("code", "name", "description")
    filter_horizontal = ("ingredients",)
    readonly_fields = ("version",)

    def get_readonly_fields(self, request, obj=None):
        # operations knows a product by its code, and no event carries a rename
        return ("version", "code") if obj else ("version",)

    def save_model(self, request, obj, form, change):
        if not change:
            super().save_model(request, obj, form, change)
            # a new product must reach operations as a live event, not only via bootstrap
            inventory.emit_state(obj)
            return
        # obj may be older than the row, so only what the admin edited leaves it
        changes = {name: getattr(obj, name) for name in form.changed_data if name in _ROW_FIELDS}
        expected = (
            form.cleaned_data.get("expected_version")
            if inventory.PROJECTED_FIELDS & set(changes)
            else None
        )
        inventory.update_product(
            obj.pk, changes, expected_version=expected, reason="admin edit", staff=request.user
        )
        obj.refresh_from_db()

    def get_changelist_form(self, request, **kwargs):
        kwargs.setdefault("form", ProductChangelistForm)
        return super().get_changelist_form(request, **kwargs)

    def save_related(self, request, form, formsets, change):
        if change:
            form.save_edited_m2m()
        else:
            form.save_m2m()
        for formset in formsets:
            self.save_formset(request, form, formset, change=change)

    def has_delete_permission(self, request, obj=None):
        # hard delete would leave an obsolete projection with no deletion event; deactivate instead
        return False


@admin.register(Ingredient)
class IngredientAdmin(admin.ModelAdmin):
    search_fields = ("name",)


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = ("product", "old_quantity", "new_quantity", "reason", "staff", "created_at")
    list_filter = ("created_at",)
    search_fields = ("product__code", "product__name", "reason")

    # audit log only: entries are written by the inventory service, never by hand
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
