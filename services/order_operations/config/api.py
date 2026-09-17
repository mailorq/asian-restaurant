import datetime as dt
import uuid
from decimal import Decimal

from django.conf import settings
from django.http import HttpResponse
from event_contracts import NOTE_MAX, OrderStatus
from ninja import NinjaAPI, Path, Schema, Status
from ninja.errors import HttpError
from pydantic import Field, ValidationError

from operations.auth import EmployeeJWTAuth
from operations.commands import CommandConflict, create_transition_command
from operations.models import OperationCommand, OperationOrder
from operations.pagination import DEFAULT_PAGE_SIZE, paginate

IDEMPOTENCY_HEADER = "Idempotency-Key"

def _schema_url(path: str) -> str | None:
    # the schema names every staff endpoint and its fields; production serves neither it nor the page that renders it
    return None if settings.PRODUCTION else path


# staff-only by default; only /health and, outside production, the schema are open
api = NinjaAPI(title="Order Operations API", version="0.1.0", docs_url=_schema_url("/docs"),
               openapi_url=_schema_url("/openapi.json"), auth=EmployeeJWTAuth())


class HealthOut(Schema):
    status: str


class OpsOrderOut(Schema):
    source_order_id: int
    status: str
    aggregate_version: int
    total: Decimal
    recipient_name: str
    phone: str
    address: str
    address_verified: bool
    payment_method: str


class PagedOrders(Schema):
    items: list[OpsOrderOut]
    total: int
    page: int
    page_size: int


@api.get("/health", response=HealthOut, tags=["ops"], auth=None)
def health(request) -> dict:
    return {"status": "ok"}


@api.get("/orders", response=PagedOrders, tags=["orders"])
def list_orders(request, status: str | None = None, page: int = 1, page_size: int = DEFAULT_PAGE_SIZE):
    # source_order_id breaks ties so a page boundary cannot repeat or skip a row
    qs = OperationOrder.objects.all().order_by("-created_at", "-source_order_id")
    if status:
        qs = qs.filter(status=status)
    return paginate(qs, page, page_size)


@api.get("/orders/{source_order_id}", response=OpsOrderOut, tags=["orders"])
def get_order(request, source_order_id: int):
    order = OperationOrder.objects.filter(source_order_id=source_order_id).first()
    if order is None:
        raise HttpError(404, "not found")
    return order


class TransitionCommandIn(Schema):
    expected_status: OrderStatus
    target_status: OrderStatus
    reason: str = Field("", max_length=NOTE_MAX)


class CommandOut(Schema):
    command_id: uuid.UUID
    status: str
    result_code: str
    result_detail: str
    deadline_at: dt.datetime | None
    created_at: dt.datetime


@api.post("/orders/{order_id}/transition-commands", response={202: CommandOut, 200: CommandOut},
          tags=["commands"])
def request_transition(request, payload: TransitionCommandIn, response: HttpResponse,
                       order_id: int = Path(..., gt=0)):
    """asks the storefront to move an order; the command is applied there, never here"""
    try:
        command, created = create_transition_command(
            actor_id=request.actor_id,
            actor_authz_version=request.actor_authz_version,
            # the projection is a read model, so a missing order is the storefront's verdict
            order_id=order_id,
            expected_status=payload.expected_status,
            target_status=payload.target_status,
            idempotency_key=request.headers.get(IDEMPOTENCY_HEADER, ""),
            reason=payload.reason,
        )
    except CommandConflict:
        raise HttpError(409, f"{IDEMPOTENCY_HEADER} reused with a different request") from None
    except ValidationError:
        # ValidationError subclasses ValueError, so it has to be caught first; its text carries
        # the contract model and the minted ids and must not reach the client
        raise HttpError(422, "expected_status and target_status must differ") from None
    except ValueError as exc:
        raise HttpError(422, str(exc)) from None

    response["Location"] = f"/ops-api/commands/{command.command_id}"
    return Status(202 if created else 200, command)


@api.get("/commands/{command_id}", response=CommandOut, tags=["commands"])
def get_command(request, command_id: uuid.UUID):
    # scoped to its author: another employee gets 404 rather than proof it exists
    command = OperationCommand.objects.filter(
        command_id=command_id, actor_id=request.actor_id
    ).first()
    if command is None:
        raise HttpError(404, "not found")
    return command
