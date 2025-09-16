from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Iterable
import time
import asyncio

import httpx

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


def parse_dt_iso(s: Optional[str]) -> Optional[datetime]:
    """Robust ISO datetime parser that preserves timezone when present.

    Supports:
    - ...Z (UTC)
    - ...+HH:MM or ...+HHMM (inserts colon)
    - date-only (YYYY-MM-DD) -> midnight
    Returns None if parsing fails.
    """
    if not s:
        return None
    try:
        t = s.strip()
        # Replace trailing Z with +00:00
        if t.endswith("Z"):
            t = t[:-1] + "+00:00"
        # Insert colon into timezone if missing (e.g., +0200 -> +02:00)
        if len(t) >= 5 and (t[-5] in ["+", "-"] and t[-3] != ":"):
            t = t[:-2] + ":" + t[-2:]
        return datetime.fromisoformat(t)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
    return None


def get_with_retries(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 20.0,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    status_forcelist: Iterable[int] = (500, 502, 503, 504),
) -> httpx.Response:
    """HTTP GET with simple retries for transient errors."""
    attempt = 0
    last_exc: Optional[Exception] = None
    while attempt < max_attempts:
        attempt += 1
        try:
            with httpx.Client(timeout=timeout) as client:
                resp = client.get(url, params=params, headers=headers)
                if resp.status_code in status_forcelist and attempt < max_attempts:
                    time.sleep(backoff_base * (2 ** (attempt - 1)))
                    continue
                resp.raise_for_status()
                return resp
        except httpx.HTTPStatusError as e:
            if e.response.status_code in status_forcelist and attempt < max_attempts:
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            raise
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < max_attempts:
                time.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            raise
    assert last_exc is not None
    raise last_exc


async def async_get_with_retries(
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = 20.0,
    max_attempts: int = 3,
    backoff_base: float = 0.5,
    status_forcelist: Iterable[int] = (500, 502, 503, 504),
    client: Optional[httpx.AsyncClient] = None,
) -> httpx.Response:
    """Async HTTP GET with simple retries for transient errors."""
    attempt = 0
    last_exc: Optional[Exception] = None
    while attempt < max_attempts:
        attempt += 1
        try:
            if client is None:
                async with httpx.AsyncClient(timeout=timeout) as ac:
                    resp = await ac.get(url, params=params, headers=headers)
            else:
                resp = await client.get(url, params=params, headers=headers)
            if resp.status_code in status_forcelist and attempt < max_attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            resp.raise_for_status()
            return resp
        except httpx.HTTPStatusError as e:
            if e.response.status_code in status_forcelist and attempt < max_attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            raise
        except httpx.HTTPError as e:
            last_exc = e
            if attempt < max_attempts:
                await asyncio.sleep(backoff_base * (2 ** (attempt - 1)))
                continue
            raise
    assert last_exc is not None
    raise last_exc


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

