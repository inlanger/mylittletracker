import httpx
from datetime import datetime
from typing import Any, Dict, List, Optional
import unicodedata

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import parse_dt_iso, get_with_retries, async_get_with_retries

BASE_URL = "https://wct.cttexpress.com/p_track_redis.php"


def track(sc: str, *, language: Optional[str] = None) -> TrackingResponse:
    """Fetch tracking info for a CTT Express shipment by shipping code (sc).

    CTT public JSON endpoint example:
    https://wct.cttexpress.com/p_track_redis.php?sc=0082800082909720118884
    """
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
    }
    params = {"sc": sc}

    resp = get_with_retries(BASE_URL, params=params, headers=headers, timeout=20.0)
    raw = resp.json()

    return normalize_ctt_response(raw, sc)


async def track_async(
    sc: str,
    *,
    language: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> TrackingResponse:
    """Async version of CTT tracking."""
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
    }
    params = {"sc": sc}

    resp = await async_get_with_retries(
        BASE_URL, params=params, headers=headers, timeout=20.0, client=client
    )
    raw = resp.json()

    return normalize_ctt_response(raw, sc)


def normalize_ctt_response(raw: Dict[str, Any], tracking_number: str) -> TrackingResponse:
    """Normalize CTT Express JSON payload to TrackingResponse."""
    data = (raw or {}).get("data") or {}
    shipments: List[Shipment] = []

    if not data:
        return TrackingResponse(shipments=shipments, provider="ctt")

    shipping_history = (data.get("shipping_history") or {})
    raw_events = (shipping_history.get("events") or [])

    events: List[TrackingEvent] = []
    for ev in raw_events:
        # Prefer precise event datetime, fallback to event_date
        dt_str = ((ev.get("detail") or {}).get("item_event_datetime")) or ev.get("event_date")
        ts = parse_dt_iso(dt_str) or datetime.now()

        desc = ev.get("description") or ev.get("type") or ""
        status_code = ev.get("code")

        # Details: include courier code/text if present and meaningful
        det = (ev.get("detail") or {})
        details = None
        for k in ("item_event_text", "External_event_text", "event_courier_code"):
            v = det.get(k)
            if isinstance(v, str) and v.lower() != "null" and v.strip():
                details = v
                break

        events.append(
            TrackingEvent(
                timestamp=ts,
                status=desc,
                details=details,
                status_code=status_code,
                extras={
                    "type": ev.get("type"),
                    "raw_detail": det or None,
                },
            )
        )

    # Ensure chronological order
    events.sort(key=lambda e: e.timestamp)

    # Determine overall shipment status
    status = _infer_ctt_status(events)

    # Compose origin/destination (use provided names if available)
    origin = data.get("origin_name") or data.get("origin_province_name")
    destination = data.get("destin_name") or data.get("destin_province_name")

    # Estimated vs actual delivery
    est_str = data.get("committed_delivery_datetime") or data.get("reported_delivery_date") or data.get("delivery_date")
    estimated_delivery = parse_dt_iso(est_str) if est_str else None
    actual_delivery = None
    if status == ShipmentStatus.DELIVERED:
        ad_str = data.get("delivery_date") or est_str
        actual_delivery = parse_dt_iso(ad_str) if ad_str else None

    tracking = data.get("shipping_code") or tracking_number

    # Populate extras for CTT-specific data
    extras = {
        "client_reference": data.get("client_reference"),
        "declared_weight": data.get("declared_weight"),
        "final_weight": data.get("final_weight"),
        "shipping_type_code": data.get("shipping_type_code"),
        "client_center_code": data.get("client_center_code"),
        "client_code": data.get("client_code"),
        "item_count": data.get("item_count"),
        "traffic_type_code": data.get("traffic_type_code"),
        "has_custom": data.get("has_custom"),
    }

    shipment = Shipment(
        tracking_number=tracking,
        carrier="ctt",
        status=status,
        events=events,
        origin=origin,
        destination=destination,
        estimated_delivery=estimated_delivery,
        actual_delivery=actual_delivery,
        extras=extras,
    )
    shipments.append(shipment)

    return TrackingResponse(shipments=shipments, provider="ctt")


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    # Deprecated in favor of utils.parse_dt_iso
    return parse_dt_iso(s)
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
            try:
                return datetime.strptime(s, fmt)
            except Exception:
                continue
    return None


def _parse_date(s: Optional[str]) -> Optional[datetime]:
    # Deprecated in favor of utils.parse_dt_iso
    return parse_dt_iso(s)
    if not s:
        return None
    # Date-only becomes midnight
    try:
        if "T" in s:
            return _parse_dt(s)
        return datetime.strptime(s, "%Y-%m-%d")
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def _infer_ctt_status(events: List[TrackingEvent]) -> ShipmentStatus:
    if not events:
        return ShipmentStatus.UNKNOWN

    latest = events[-1]
    text = latest.status or ""

    # First try explicit mapping by code if present in latest event
    code = (latest.status_code or "").strip()
    code_map = {
        # Observed codes from sample: 0000, 1000, 1500; plus 2310 observed as available for pickup
        "0000": ShipmentStatus.INFORMATION_RECEIVED,
        "1000": ShipmentStatus.IN_TRANSIT,
        "1500": ShipmentStatus.OUT_FOR_DELIVERY,
        "2310": ShipmentStatus.AVAILABLE_FOR_PICKUP,
        # Future observations (documented placeholders):
        # "2000": ShipmentStatus.DELIVERED,
        # "2400": ShipmentStatus.EXCEPTION,
    }
    if code in code_map:
        return code_map[code]

    def _norm(t: str) -> str:
        # Remove accents and lowercase for robust matching
        return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")

    t = _norm(text)

    # Common Spanish phrases seen in CTT payloads
    if "entregado" in t or "entrega realizada" in t:
        return ShipmentStatus.DELIVERED
    if "entrega hoy" in t or "en reparto" in t or "reparto" in t or "delivery today" in t:
        return ShipmentStatus.OUT_FOR_DELIVERY
    if "disponible para recoger" in t or "para recoger" in t or "punto de recogida" in t:
        return ShipmentStatus.AVAILABLE_FOR_PICKUP
    if "transito" in t or "en transito" in t or "in transit" in t:
        return ShipmentStatus.IN_TRANSIT
    if "pendiente de recepcion" in t or "pendiente de recogida" in t or "admitido" in t:
        return ShipmentStatus.INFORMATION_RECEIVED

    # Fallback to generic mapper
    from ..utils import map_status_from_text

    return map_status_from_text(text)
