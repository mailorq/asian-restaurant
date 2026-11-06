from django import forms
from django.contrib import admin
from django.core.files.uploadedfile import UploadedFile
from django.db import router, transaction
from django.utils import timezone
from django.utils.text import Truncator

from common.forms import RenderedValuesForm
from menu import inventory
from menu.models import Ingredient, Product, ProductChange, StockAdjustment

_ROW_FIELDS = frozenset(f.name for f in Product._meta.concrete_fields)
# the admin location in frontend/nginx/default.conf takes a larger body, so an oversized photo gets this form's message and not a bare 413
PRODUCT_IMAGE_MAX_BYTES = 5 * 1024 * 1024
KEEP, MINE = "keep", "mine"


def _display(field: str, value) -> str:
    if field == "image":
        return getattr(value, "name", value) or "нет фото"
    if isinstance(value, bool):
        return "да" if value else "нет"
    if field == "category":
        return Product.Category(value).label if value else "пусто"
    text = "" if value is None else str(value)
    return Truncator(text).chars(60) if text else "пусто"


def _when(conflict) -> str:
    return timezone.localtime(conflict.at).strftime("%d.%m.%Y %H:%M") if conflict.at else ""


class ProductWriteForm(RenderedValuesForm):
    """
    a product form that writes only the fields its admin edited, each while the row still holds the value the form
    showed: a field someone else changed meanwhile comes back with what it holds now and who left it there
    """

    conflicts: tuple | list = ()
    # what save_model writes and the values the admin saw of those fields; None until the form is clean
    accepted: list | None = None
    seen: dict | None = None

    def edited(self) -> list[str]:
        names = [name for name in self.changed_data if name in _ROW_FIELDS]
        # a file input posts nothing back, so the photo is edited only when one is sent or cleared
        image = self.cleaned_data.get("image")
        if "image" in names and not (isinstance(image, UploadedFile) or image is False):
            names.remove("image")
        return names

    def rendered(self, name: str):
        """the value this form was shown for the field, as the page posted it back"""
        widget = self.fields[name].hidden_widget()
        return widget.value_from_datadict(self.data, self.files, self.add_initial_prefix(name))

    def resolved(self, names: list[str], seen: dict) -> tuple[list[str], dict]:
        return names, seen

    def current(self) -> Product:
        return Product.objects.get(pk=self.instance.pk)

    def refuse(self) -> None:
        raise NotImplementedError

    def clean(self):
        cleaned = super().clean()
        if not self.instance.pk or self._errors:
            return cleaned
        names = self.edited()
        names, seen = self.resolved(names, {name: self.rendered(name) for name in names})
        self.conflicts = inventory.conflicts(self.current(), seen)
        if self.conflicts:
            self.refuse()
        self.accepted, self.seen = names, seen
        return cleaned


class ProductAdminForm(ProductWriteForm, forms.ModelForm):
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

    def clean_image(self):
        image = self.cleaned_data.get("image")
        if isinstance(image, UploadedFile) and image.size > PRODUCT_IMAGE_MAX_BYTES:
            raise forms.ValidationError("Фото больше 5 МБ")
        return image

    def resolved(self, names, seen):
        # a conflict the admin saw is settled field by field: keep what the row holds, or write over the value
        # shown then, which the writer checks again under the row lock
        names = list(names)
        for name in list(names):
            choice = self.data.get(f"resolve-{name}")
            if choice == KEEP:
                names.remove(name)
                seen.pop(name)
            elif choice == MINE:
                seen[name] = self.data.get(f"shown-{name}")
        return names, seen

    def current(self) -> Product:
        # the admin view runs in one transaction, so this lock holds until save_model has written
        return Product.objects.select_for_update().get(pk=self.instance.pk)

    def refuse(self) -> None:
        raise forms.ValidationError(
            "Пока форма была открыта, эти поля изменили. Выберите для каждого, оставить текущее значение "
            "или записать свое, и сохраните еще раз."
        )

    @property
    def conflict_rows(self) -> list[dict]:
        return [
            {
                "field": c.field,
                "label": self.fields[c.field].label,
                "current": _display(c.field, c.current),
                "author": c.author or "автор неизвестен",
                "at": _when(c),
                "mine": (
                    "новое фото, загрузите его еще раз"
                    if c.field == "image"
                    else _display(c.field, self.cleaned_data.get(c.field))
                ),
                "shown": inventory.shown(c.field, c.current),
            }
            for c in self.conflicts
        ]


class ProductChangelistForm(ProductWriteForm, forms.ModelForm):
    class Meta:
        model = Product
        fields = ("price", "is_active", "is_featured")

    def current(self) -> Product:
        # ProductAdmin.changelist_view holds one transaction over the check and the save, as the change view does
        return Product.objects.select_for_update().get(pk=self.instance.pk)

    def refuse(self) -> None:
        # the list has no room to settle a conflict; the card of the product does
        for conflict in self.conflicts:
            self.add_error(
                conflict.field,
                f"Изменено, пока список был открыт: {self.describe(conflict)}. Решите в карточке товара.",
            )

    @staticmethod
    def describe(conflict) -> str:
        when = _when(conflict)
        source = conflict.author or "автор неизвестен"
        return f"сейчас «{_display(conflict.field, conflict.current)}» ({source}{f', {when}' if when else ''})"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    form = ProductAdminForm
    list_display = (
        "code",
        "name",
        "category",
        "price",
        "stock_quantity",
        "is_active",
        "is_featured",
    )
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
        names = form.changed_data if getattr(form, "accepted", None) is None else form.accepted
        changes = {name: getattr(obj, name) for name in names if name in _ROW_FIELDS}
        inventory.update_product(
            obj.pk,
            changes,
            reason="admin edit",
            staff=request.user,
            seen=getattr(form, "seen", None),
        )
        obj.refresh_from_db()

    def changelist_view(self, request, extra_context=None):
        # django validates the list before it opens the transaction it saves in, so a row could change between its
        # check and its write and still be reported as saved. one transaction over both, with the rows locked in id
        # order as checkout and stock corrections take them, leaves no such gap
        with transaction.atomic(using=router.db_for_write(Product)):
            if request.method == "POST" and "_save" in request.POST:
                ids = sorted(
                    int(v) for k, v in request.POST.items() if k.endswith("-id") and v.isdigit()
                )
                list(
                    Product.objects.select_for_update()
                    .filter(pk__in=ids)
                    .order_by("id")
                    .values_list("pk")
                )
            return super().changelist_view(request, extra_context)

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


class _AuditAdmin(admin.ModelAdmin):
    # audit log only: entries are written by the inventory service, never by hand
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(_AuditAdmin):
    list_display = (
        "product",
        "old_quantity",
        "new_quantity",
        "source",
        "reason",
        "staff",
        "created_at",
    )
    list_filter = ("source", "created_at")
    search_fields = ("product__code", "product__name", "reason")


@admin.register(ProductChange)
class ProductChangeAdmin(_AuditAdmin):
    list_display = ("product", "field", "old_value", "new_value", "staff", "reason", "created_at")
    list_filter = ("field", "created_at")
    search_fields = ("product__code", "product__name")
