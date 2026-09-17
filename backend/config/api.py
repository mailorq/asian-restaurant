from django.conf import settings
from ninja import NinjaAPI, Schema

from accounts.api import router as auth_router
from cart.api import router as cart_router
from employee.api import router as employee_router
from menu.api import router as menu_router
from orders.api import router as orders_router

# swagger ui served at /api/docs, openapi schema at /api/openapi.json (fastapi-style)
api = NinjaAPI(
    title="Asian Restaurant API",
    version="1.0.0",
    description="OpenAPI. Auth, Cart ...",
    # the schema names every endpoint and its fields, the staff ones included; production serves neither it nor the page that renders it
    docs_url=None if settings.DJANGO_PRODUCTION else "/docs",
    openapi_url=None if settings.DJANGO_PRODUCTION else "/openapi.json",
)


class HealthOut(Schema):
    status: str


@api.get("/health", response=HealthOut, auth=None, tags=["ops"])
def health(request) -> dict:
    return {"status": "ok"}


api.add_router("/auth", auth_router)
api.add_router("/menu", menu_router)
api.add_router("/cart", cart_router)
api.add_router("/orders", orders_router)
api.add_router("/employee", employee_router)
