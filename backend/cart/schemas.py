from ninja import Field, Schema


class AddItemIn(Schema):
    product_id: int
    quantity: int = Field(default=1, ge=1, le=50)
    expected_version: int | None = None


class SetQtyIn(Schema):
    quantity: int = Field(ge=0, le=50)
    expected_version: int | None = None


class ReviewIn(Schema):
    expected_version: int


class CartLineOut(Schema):
    product_id: int
    name: str
    category: str
    price: float
    quantity: int
    image: str | None = None
    available: bool


class CartAdjustment(Schema):
    product_id: int
    name: str
    from_qty: int
    to_qty: int
    reason: str  # "stock"


class CartRemoved(Schema):
    product_id: int
    name: str
    reason: str  # "unavailable" | "out_of_stock"


class CartOut(Schema):
    version: int
    items: list[CartLineOut]
    total: float
    count: int
    adjustments: list[CartAdjustment] = []
    removed_items: list[CartRemoved] = []
    # an order of the previous release this cart may still hold lines of, until its owner confirms it
    review_order: int | None = None


class CartConflictOut(Schema):
    code: str  # "cart_version_conflict"
    cart: CartOut
