"""The shop the eval tasks run in: customers, orders, stock, refunds and notes, rebuilt from the
same data for every task, and the tools the agent reaches it with. Pure Python: no network."""

from dataclasses import dataclass, field
from decimal import Decimal
from typing import ClassVar, Literal

from pydantic import BaseModel, Field

from lite_agent.tools import Tool, ToolContext, tool

Status = Literal["processing", "shipped", "delivered", "cancelled"]

SYSTEM = (
    "You are a support agent for a small shop. Use the tools to look things up and to act; "
    "never guess an id, an amount or a status. Answer the user briefly, in the user's language."
)


@dataclass(frozen=True)
class Customer:
    id: str
    name: str
    email: str


@dataclass(frozen=True)
class Order:
    id: str
    customer_id: str
    item: str
    amount: Decimal
    status: Status
    next_order: str | None = None  # the order to look up after this one (the long task's chain)


CUSTOMERS = (
    Customer("C1", "Ana Lima", "ana@example.com"),
    Customer("C2", "Bo Chen", "bo@example.com"),
    Customer("C3", "王芳", "wang.fang@example.cn"),
    Customer("C4", "李雷", "li.lei@example.cn"),
    Customer("C5", "Sam Ito", "sam@example.com"),
)
ORDERS = (
    Order("A17", "C1", "desk lamp", Decimal("42.00"), "shipped"),
    Order("A18", "C1", "lamp bulb", Decimal("6.50"), "delivered"),
    Order("B03", "C2", "office chair", Decimal("189.00"), "delivered"),
    Order("B04", "C2", "chair mat", Decimal("35.00"), "processing"),
    Order("B05", "C2", "monitor arm", Decimal("79.90"), "delivered"),
    Order("D01", "C3", "茶具套装", Decimal("268.00"), "delivered"),
    Order("D02", "C3", "茶叶", Decimal("88.00"), "cancelled"),
    Order("D03", "C4", "台灯", Decimal("120.00"), "shipped"),
    Order("D04", "C4", "笔记本", Decimal("18.00"), "delivered"),
    Order("E01", "C4", "钢笔", Decimal("56.00"), "delivered"),
    Order("E02", "C4", "墨水", Decimal("12.50"), "processing"),
    # The long task's chain: each order names the next, so they can only be looked up in turn.
    Order("F01", "C5", "notebook", Decimal("24.00"), "delivered", "F02"),
    Order("F02", "C5", "backpack", Decimal("61.50"), "delivered", "F03"),
    Order("F03", "C5", "headphones", Decimal("140.00"), "delivered", "F04"),
    Order("F04", "C5", "pencils", Decimal("9.90"), "delivered", "F05"),
    Order("F05", "C5", "desk mat", Decimal("33.00"), "shipped", "F06"),
    Order("F06", "C5", "lamp shade", Decimal("77.25"), "delivered", "F07"),
    Order("F07", "C5", "stapler", Decimal("15.00"), "processing", "F08"),
    Order("F08", "C5", "folder set", Decimal("48.35"), "delivered"),
)
STOCK = {"LAMP-1": 3, "CHAIR-2": 0, "MAT-3": 12, "ARM-4": 5}


@dataclass(frozen=True)
class Refund:
    order_id: str
    amount: Decimal


@dataclass(frozen=True)
class Note:
    customer_id: str
    text: str


# The tools' arguments: their JSON Schemas are what the model is shown.


class ByEmail(BaseModel):
    email: str = Field(description="The customer's email address")


class ByCustomer(BaseModel):
    customer_id: str = Field(description="A customer id such as C9")


class ByOrder(BaseModel):
    order_id: str = Field(description="An order id such as Z01")


class BySku(BaseModel):
    sku: str = Field(description="A stock keeping unit such as SKU-0")


class RefundArgs(BaseModel):
    order_id: str = Field(description="An order id such as Z01")
    amount: Decimal = Field(gt=0, description="The amount to refund, in the shop's currency")


class NoteArgs(BaseModel):
    customer_id: str = Field(description="A customer id such as C9")
    text: str = Field(min_length=1, description="What the customer will read")


@dataclass
class World:
    """One task's shop. What the tools changed is here for the task's check to read."""

    system: ClassVar[str] = SYSTEM  # the agent's instructions in the shop
    customers: dict[str, Customer] = field(default_factory=lambda: {c.id: c for c in CUSTOMERS})
    orders: dict[str, Order] = field(default_factory=lambda: {o.id: o for o in ORDERS})
    stock: dict[str, int] = field(default_factory=lambda: dict(STOCK))
    refunds: list[Refund] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    def refunded(self, order_id: str) -> Decimal:
        return sum((r.amount for r in self.refunds if r.order_id == order_id), Decimal(0))

    def tools(self) -> tuple[Tool, ...]:
        world = self

        @tool(description="Find a customer by email address.", idempotent=True)
        async def find_customer(args: ByEmail, ctx: ToolContext) -> str:
            for c in world.customers.values():
                if c.email == args.email.strip().lower():
                    return f"customer {c.id}: {c.name} <{c.email}>"
            return f"no customer with email {args.email}"

        @tool(description="List a customer's orders: id, item, amount and status.", idempotent=True)
        async def list_orders(args: ByCustomer, ctx: ToolContext) -> str:
            if args.customer_id not in world.customers:
                return f"no customer {args.customer_id}"
            rows = [o for o in world.orders.values() if o.customer_id == args.customer_id]
            return "\n".join(_row(o) for o in rows) or "no orders"

        @tool(description="Look up one order: its item, amount and status.", idempotent=True)
        async def get_order(args: ByOrder, ctx: ToolContext) -> str:
            order = world.orders.get(args.order_id.strip().upper())
            return _row(order) if order else f"no order {args.order_id}"

        @tool(description="How many units of a product are in stock.", idempotent=True)
        async def check_stock(args: BySku, ctx: ToolContext) -> str:
            units = world.stock.get(args.sku.strip().upper())
            return (
                f"{args.sku}: {units} in stock" if units is not None else f"no product {args.sku}"
            )

        @tool(description="Refund an amount of an order to the customer. It cannot be undone.")
        async def refund(args: RefundArgs, ctx: ToolContext) -> str:
            order = world.orders.get(args.order_id.strip().upper())
            if order is None:
                return f"refused: no order {args.order_id}"
            if order.status not in ("shipped", "delivered"):
                return f"refused: order {order.id} is {order.status}"
            left = order.amount - world.refunded(order.id)
            if args.amount > left:
                return f"refused: only {left:.2f} of order {order.id} can still be refunded"
            world.refunds.append(Refund(order.id, args.amount))
            return f"refunded {args.amount:.2f} of order {order.id}"

        @tool(description="Leave a note the customer will read.")
        async def send_note(args: NoteArgs, ctx: ToolContext) -> str:
            if args.customer_id not in world.customers:
                return f"refused: no customer {args.customer_id}"
            world.notes.append(Note(args.customer_id, args.text))
            return f"note left for {args.customer_id}"

        return (find_customer, list_orders, get_order, check_stock, refund, send_note)


def _row(o: Order) -> str:
    row = f"order {o.id}: {o.item}, {o.amount:.2f}, {o.status} (customer {o.customer_id})"
    return f"{row}; next order to look up: {o.next_order}" if o.next_order else row
