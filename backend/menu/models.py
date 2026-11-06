from django.conf import settings
from django.db import models


class Ingredient(models.Model):
    name = models.CharField(max_length=80, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class Product(models.Model):
    class Category(models.TextChoices):
        DISH = "dish", "Блюдо"
        DRINK = "drink", "Напиток"
        DESSERT = "dessert", "Десерт"

    # stable public identifier (dish_1, drink_4…)
    code = models.SlugField(max_length=40, unique=True, default="")
    category = models.CharField(max_length=10, choices=Category.choices, db_index=True)
    name = models.CharField(max_length=120)
    description = models.TextField(blank=True)
    allergens = models.CharField(max_length=255, blank=True, default="")
    price = models.DecimalField(max_digits=8, decimal_places=2)
    image = models.ImageField(upload_to="products/", blank=True)
    ingredients = models.ManyToManyField(Ingredient, related_name="products", blank=True)
    stock_quantity = models.PositiveIntegerField(default=0)
    # initial state is v1; bumped on every stock change so projections can version-fence
    version = models.PositiveIntegerField(default=1)
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["category", "name"]
        indexes = [models.Index(fields=["category", "is_active"])]
        constraints = [
            models.CheckConstraint(condition=models.Q(price__gte=0), name="product_price_gte_0"),
            models.CheckConstraint(
                condition=models.Q(stock_quantity__gte=0), name="product_stock_gte_0"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} · {self.name}"

    @property
    def available(self) -> bool:
        return self.is_active and self.stock_quantity > 0


class StockAdjustment(models.Model):
    class Source(models.TextChoices):
        STAFF = "staff", "Сотрудник"
        SALE = "sale", "Продажа"
        CANCELLATION = "cancellation", "Отмена заказа"
        SYSTEM = "system", "Система"

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="stock_adjustments")
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    # what moved the stock; only a staff change is signed by a person. empty is a row the release before wrote
    # while this one migrated, which the database fills in
    source = models.CharField(max_length=16, choices=Source.choices, blank=True, db_default="")
    old_quantity = models.PositiveIntegerField()
    new_quantity = models.PositiveIntegerField()
    reason = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return f"{self.product.code}: {self.old_quantity} → {self.new_quantity}"


class ProductChange(models.Model):
    """a card field other than the stock, as an edit left it; the stock keeps its own log in StockAdjustment"""

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name="changes")
    field = models.CharField(max_length=32)
    old_value = models.TextField(blank=True)
    new_value = models.TextField(blank=True)
    # null when no person made the edit
    staff = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reason = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at", "-id"]
        indexes = [models.Index(fields=["product", "field", "-created_at"])]

    def __str__(self) -> str:
        return f"{self.product.code}.{self.field}"
