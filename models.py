"""
models.py — Dataclasses Order / OrderItem
"""
from dataclasses import dataclass, field
from typing import List, Dict, Any
from datetime import datetime, timezone
import time


def _now():
    return datetime.now(timezone.utc)


def _utc_to_local(dt: datetime) -> datetime:
    """Convertit un datetime UTC en heure locale (compatible sans pytz)."""
    timestamp = dt.timestamp()
    local_dt = datetime.fromtimestamp(timestamp)
    return local_dt


@dataclass
class OrderItem:
    name: str
    price: float
    quantity: int = 1
    tva_rate: float = 10.0
    category: str = "alimentation"

    @property
    def total(self) -> float:
        return round(self.price * self.quantity, 2)

    @property
    def tva_amount(self) -> float:
        return round(self.total - (self.total / (1 + self.tva_rate / 100)), 2)

    def to_dict(self):
        return {"name": self.name, "price": self.price, "quantity": self.quantity,
                "tva_rate": self.tva_rate, "category": self.category}

@dataclass
class Order:
    table: str = "Table 1"
    items: List[OrderItem] = field(default_factory=list)
    created_at: datetime = field(default_factory=_now)
    payment_method: str = ""
    payments: List[Dict[str, Any]] = field(default_factory=list)
    is_paid: bool = False

    def add_item(self, item: "OrderItem") -> None:
        for existing in self.items:
            if existing.name == item.name:
                existing.quantity += item.quantity
                return
        self.items.append(item)

    def remove_item(self, name: str) -> None:
        self.items = [i for i in self.items if i.name != name]

    def update_quantity(self, name: str, delta: int) -> None:
        for item in self.items:
            if item.name == name:
                item.quantity += delta
                if item.quantity <= 0:
                    self.remove_item(name)
                return

    @property
    def total(self) -> float:
        return round(sum(i.total for i in self.items), 2)

    @property
    def tva_summary(self) -> Dict[float, Dict[str, float]]:
        s: Dict[float, Dict[str, float]] = {}
        for item in self.items:
            if item.tva_rate not in s:
                s[item.tva_rate] = {"ht": 0.0, "tva": 0.0, "ttc": 0.0}
            ht  = round(item.total / (1 + item.tva_rate / 100), 2)
            tva = round(item.total - ht, 2)
            s[item.tva_rate]["ht"]  = round(s[item.tva_rate]["ht"]  + ht,  2)
            s[item.tva_rate]["tva"] = round(s[item.tva_rate]["tva"] + tva, 2)
            s[item.tva_rate]["ttc"] = round(s[item.tva_rate]["ttc"] + item.total, 2)
        return s

    def to_dict(self) -> Dict[str, Any]:
        return {
            "table": self.table,
            "items": [i.to_dict() for i in self.items],
            "total": self.total,
            "payment_method": self.payment_method,
            "payments": self.payments,
            "is_paid": self.is_paid,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Order":
        o = cls(table=data.get("table", "Table 1"),
                payment_method=data.get("payment_method", ""),
                payments=data.get("payments", []),
                is_paid=data.get("is_paid", False))
        if "created_at" in data:
            dt = datetime.fromisoformat(data["created_at"])
            o.created_at = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        for d in data.get("items", []):
            o.add_item(OrderItem(name=d["name"], price=d["price"],
                                 quantity=d["quantity"],
                                 tva_rate=d.get("tva_rate", 10.0),
                                 category=d.get("category", "alimentation")))
        return o
