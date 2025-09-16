from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from .models import ShipmentStatus


def to_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware UTC.

    If the datetime is naive (no tzinfo), assume it is UTC and attach tzinfo=UTC.
    If it is aware, convert to UTC.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def serialize_dt(dt: datetime) -> str:
    """Serialize datetime as ISO-8601 with trailing 'Z' for UTC."""
    return to_utc(dt).isoformat().replace("+00:00", "Z")


def map_status_from_text(text: Optional[str]) -> ShipmentStatus:
    if not text:
        return ShipmentStatus.UNKNOWN
    t = text.lower()
    # Available for pickup should be checked before generic "pickup"
    if (
        "available for pickup" in t
        or "ready for pickup" in t
        or "disponible para recoger" in t
        or "para recoger" in t
        or "pickup point" in t
        or "collection point" in t
    ):
        return ShipmentStatus.AVAILABLE_FOR_PICKUP
    if "delivered" in t or "entregado" in t:
        return ShipmentStatus.DELIVERED
    if "out for delivery" in t or "in delivery" in t or "reparto" in t:
        return ShipmentStatus.OUT_FOR_DELIVERY
    if "in transit" in t or "transit" in t or "depot" in t or "sorted" in t or "on the way" in t:
        return ShipmentStatus.IN_TRANSIT
    if "pickup" in t or "accepted" in t or "admitido" in t or "pre-registered" in t or "pre registered" in t:
        return ShipmentStatus.INFORMATION_RECEIVED
    if "exception" in t or "failed" in t or "undeliverable" in t:
        return ShipmentStatus.EXCEPTION
    return ShipmentStatus.UNKNOWN

