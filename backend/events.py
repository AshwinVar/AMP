"""AMP domain event backbone (ADR-0001).

An in-process publish/subscribe bus. Producers publish immutable, tenant-scoped
domain events; subscribers react. Every event is also appended to the
``event_log`` — the factory's history, and the substrate for analytics, AI and
the digital twin.

The transport is deliberately behind this interface. Today it dispatches
synchronously and in-process, sharing the caller's DB session, so a subscriber's
work commits atomically with the action that produced it. It can move to an
outbox + broker (NATS / Kafka / Redis Streams) later without changing a single
producer or subscriber.
"""
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Callable, Optional

import models


@dataclass(frozen=True)
class ProductionCompleted:
    """A work order reached the Completed state."""
    tenant_code: str
    work_order_id: int
    work_order_no: str
    part_number: str
    quantity: int
    machine_id: Optional[int] = None
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    event_type: str = "ProductionCompleted"
    event_version: int = 1


@dataclass(frozen=True)
class DowntimeStarted:
    """A machine entered downtime (a downtime log was recorded)."""
    tenant_code: str
    machine_id: Optional[int]
    reason: str
    duration: str = ""
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    event_type: str = "DowntimeStarted"
    event_version: int = 1


@dataclass(frozen=True)
class InventoryLow:
    """An inventory item fell to or below its reorder level."""
    tenant_code: str
    item_id: int
    item_code: str
    item_name: str
    current_stock: int
    reorder_level: int
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    event_type: str = "InventoryLow"
    event_version: int = 1


@dataclass(frozen=True)
class QualityInspectionFailed:
    """A quality inspection recorded failed units."""
    tenant_code: str
    inspection_no: str
    failed_quantity: int
    inspected_quantity: int
    machine_id: Optional[int] = None
    work_order_id: Optional[int] = None
    defect_category: Optional[str] = None
    occurred_at: datetime = field(default_factory=datetime.utcnow)
    event_type: str = "QualityInspectionFailed"
    event_version: int = 1


class EventBus:
    """Minimal synchronous, in-process event bus."""

    def __init__(self) -> None:
        self._subscribers: dict[type, list[Callable]] = {}

    def subscribe(self, event_type: type, handler: Callable) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def publish(self, event, db=None) -> None:
        """Persist the event, then dispatch it to subscribers synchronously.

        Subscribers receive the same ``db`` session, so their writes join the
        caller's transaction and commit together. A subscriber error propagates
        by design — for now the producing action and its reactions succeed or
        fail as one unit.
        """
        self._append_to_log(event, db)
        for handler in self._subscribers.get(type(event), []):
            handler(event, db)

    @staticmethod
    def _append_to_log(event, db) -> None:
        if db is None:
            return
        payload = {
            k: (v.isoformat() if isinstance(v, datetime) else v)
            for k, v in asdict(event).items()
        }
        db.add(models.EventLog(
            tenant_code=getattr(event, "tenant_code", "DEFAULT"),
            event_type=getattr(event, "event_type", type(event).__name__),
            event_version=getattr(event, "event_version", 1),
            payload=json.dumps(payload),
            occurred_at=getattr(event, "occurred_at", datetime.utcnow()),
        ))


# Process-wide bus. Producers import this; subscribers are wired in subscribers.py.
event_bus = EventBus()
