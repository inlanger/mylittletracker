from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import httpx

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import get_with_retries, async_get_with_retries


REST_BASE = "https://tracking.dpd.de/rest/plc"

# Supported PLC locales and simple mapping from language -> locale
_SUPPORTED_LOCALES = {
    "en_US",
    "nl_NL",
    "de_DE",
    "fr_FR",
    "it_IT",
    "es_ES",
}
_LANG_TO_LOCALE = {
    "en": "en_US",
    "nl": "nl_NL",
    "de": "de_DE",
    "fr": "fr_FR",
    "it": "it_IT",
    "es": "es_ES",
}


def track(parcel_number: str, *, language: str = "EN") -> TrackingResponse:
    """Retrieve DPD tracking using the public PLC JSON endpoint only."""
    lang_code_raw = language or "EN"
    lang_code = lang_code_raw.strip()
    locale, normalized_from = _resolve_locale(lang_code)

    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
    }

    rest_url = f"{REST_BASE}/{locale}/{parcel_number}"
    resp = get_with_retries(rest_url, headers=headers, timeout=20.0)
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" not in ctype.lower():
        # Not a JSON response; treat as no data
        return TrackingResponse(shipments=[], provider="dpd")

    plc_data = resp.json()
    try:
        shipment = _normalize_dpd_plc_json(
            plc_data,
            parcel_number,
            locale=locale,
            language_input=lang_code,
            normalized_from=normalized_from,
        )
        return TrackingResponse(shipments=[shipment], provider="dpd")
    except Exception:
        try:
            shipment = _normalize_dpd_embedded(plc_data, parcel_number)
            return TrackingResponse(shipments=[shipment], provider="dpd")
        except Exception:
            return TrackingResponse(shipments=[], provider="dpd")


def _looks_like_dpd_payload(obj: Dict[str, Any]) -> bool:
    text = str(obj).lower()
    hints = ["parcel", "events", "status", "shipment", "tracking"]
    return any(h in text for h in hints)


def _normalize_dpd_plc_json(
    obj: Dict[str, Any],
    parcel_number: str,
    *,
    locale: Optional[str] = None,
    language_input: Optional[str] = None,
    normalized_from: Optional[str] = None,
) -> Shipment:
    """Normalize JSON from /rest/plc/{locale}/{parcelLabelNumber} to our model."""
    plc = obj.get("parcellifecycleResponse", {}).get("parcelLifeCycleData", {})
    shipment_info = plc.get("shipmentInfo", {})
    status_info = plc.get("statusInfo", []) or []
    scan_info = (plc.get("scanInfo", {}) or {}).get("scan", []) or []

    events: list[TrackingEvent] = []

    # Prefer scan events for detailed timeline
    if scan_info:
        for ev in scan_info:
            ts = _parse_iso_date(ev.get("date")) or _coerce_timestamp(ev)
            desc = (ev.get("scanDescription", {}) or {}).get("content", [])
            status_text = (desc[0] if desc else None) or (
                ev.get("scanDescription", {}) or {}
            ).get("label")
            location = (ev.get("scanData", {}) or {}).get("location")
            events.append(
                TrackingEvent(
                    timestamp=ts or datetime.now(),
                    status=status_text or "",
                    location=location,
                    details=status_text,
                    status_code=None,
                    extras=None,
                )
            )
    # Fallback to statusInfo milestones
    elif status_info:
        for st in status_info:
            # Only include reached statuses to avoid future milestones
            if not st.get("statusHasBeenReached"):
                continue
            ts = _parse_dpd_status_date(st.get("date"))
            desc = (st.get("description", {}) or {}).get("content", [])
            status_text = (
                (desc[0] if desc else None) or st.get("label") or st.get("status")
            )
            location = st.get("location")
            events.append(
                TrackingEvent(
                    timestamp=ts or datetime.now(),
                    status=status_text or "",
                    location=location,
                    details=status_text,
                    status_code=None,
                    extras=None,
                )
            )

    # Sort events by timestamp for consistency
    events.sort(key=lambda e: e.timestamp)

    # Determine shipment status using current status or latest event
    status_enum = ShipmentStatus.UNKNOWN
    current = next((s for s in status_info if s.get("isCurrentStatus")), None)
    if current:
        cur = (current.get("status") or "").upper()
        if "DELIVERED" in cur:
            status_enum = ShipmentStatus.DELIVERED
        elif "OUT_FOR_DELIVERY" in cur:
            status_enum = ShipmentStatus.OUT_FOR_DELIVERY
        elif "ON_THE_ROAD" in cur or "AT_DELIVERY_DEPOT" in cur or "IN_TRANSIT" in cur:
            status_enum = ShipmentStatus.IN_TRANSIT
        elif "PICKUP" in cur:
            status_enum = ShipmentStatus.INFORMATION_RECEIVED
    elif events:
        last = (events[-1].status or "").lower()
        if "delivered" in last:
            status_enum = ShipmentStatus.DELIVERED
        elif "out for delivery" in last or "delivery" in last:
            status_enum = ShipmentStatus.OUT_FOR_DELIVERY
        elif "transit" in last or "depot" in last or "on the way" in last:
            status_enum = ShipmentStatus.IN_TRANSIT

    tracking_number = shipment_info.get("parcelLabelNumber") or parcel_number

    extras: Dict[str, Any] = {}
    if locale:
        extras["dpd_locale"] = locale
    # Record normalization if the input language was not directly a supported locale
    if normalized_from and isinstance(language_input, str) and locale:
        canon = language_input.strip()
        try:
            if canon and canon.lower() != locale.lower():
                extras["language_normalized_from"] = canon
        except Exception:
            pass

    return Shipment(
        tracking_number=tracking_number,
        carrier="dpd",
        status=status_enum,
        events=events,
        service_type=None,
        origin=None,
        destination=None,
        estimated_delivery=None,
        actual_delivery=None,
        extras=extras or None,
    )


def _normalize_dpd_embedded(obj: Dict[str, Any], parcel_number: str) -> Shipment:
    """Generic fallback normalizer for unknown embedded JSON structures."""
    events = _find_first_events_list(obj)

    tracking_events: list[TrackingEvent] = []
    for ev in events:
        ts = _coerce_timestamp(ev)
        status = _coerce_status_text(ev)
        details = _coerce_details(ev)
        tracking_events.append(
            TrackingEvent(
                timestamp=ts or datetime.now(),
                status=status or "",
                location=None,
                details=details,
                status_code=None,
                extras=None,
            )
        )

    status_enum = ShipmentStatus.UNKNOWN
    if tracking_events:
        last = tracking_events[-1].status.lower()
        if "delivered" in last:
            status_enum = ShipmentStatus.DELIVERED
        elif "delivery" in last:
            status_enum = ShipmentStatus.OUT_FOR_DELIVERY
        elif "transit" in last or "sorted" in last or "processed" in last:
            status_enum = ShipmentStatus.IN_TRANSIT

    return Shipment(
        tracking_number=parcel_number,
        carrier="dpd",
        status=status_enum,
        events=tracking_events,
        service_type=None,
        origin=None,
        destination=None,
        estimated_delivery=None,
        actual_delivery=None,
        extras=None,
    )


def _find_first_events_list(obj: Dict[str, Any]) -> list[Dict[str, Any]]:
    # BFS through nested structures to locate a list of event-like dicts
    queue: list[Any] = [obj]
    while queue:
        item = queue.pop(0)
        if isinstance(item, dict):
            for v in item.values():
                if isinstance(v, list) and v and all(isinstance(x, dict) for x in v):
                    # Check if dicts look like events
                    keys = set().union(*(x.keys() for x in v))
                    if any(
                        k.lower() in ("status", "description", "state") for k in keys
                    ):
                        return v  # best guess
                queue.append(v)
        elif isinstance(item, list):
            queue.extend(item)
    return []


async def track_async(
    parcel_number: str,
    *,
    language: str = "EN",
    client: Optional[httpx.AsyncClient] = None,
) -> TrackingResponse:
    """Async version using the public PLC JSON endpoint only."""
    lang_code_raw = language or "EN"
    lang_code = lang_code_raw.strip()
    locale, normalized_from = _resolve_locale(lang_code)

    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
    }
    rest_url = f"{REST_BASE}/{locale}/{parcel_number}"

    resp = await async_get_with_retries(
        rest_url, headers=headers, timeout=20.0, client=client
    )
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" not in ctype.lower():
        return TrackingResponse(shipments=[], provider="dpd")

    data = resp.json()
    try:
        shipment = _normalize_dpd_plc_json(
            data,
            parcel_number,
            locale=locale,
            language_input=lang_code,
            normalized_from=normalized_from,
        )
    except Exception:
        shipment = _normalize_dpd_embedded(data, parcel_number)
    return TrackingResponse(shipments=[shipment], provider="dpd")


def _resolve_locale(lang_code: str) -> Tuple[str, Optional[str]]:
    """Resolve an input language or locale to a supported PLC locale.

    Returns (locale, normalized_from). normalized_from is the original input if a
    normalization or fallback was applied; otherwise None.
    """
    if not lang_code:
        return ("en_US", None)

    code = lang_code.strip()
    # Accept hyphen or underscore, canonicalize to lang_REGION
    c = code.replace("-", "_")
    if len(c) == 5 and c[2] == "_":
        lang = c[:2].lower()
        region = c[3:].upper()
        loc = f"{lang}_{region}"
        if loc in _SUPPORTED_LOCALES:
            return (loc, None)
        # Fallback to mapping by language only
        if lang in _LANG_TO_LOCALE:
            return (_LANG_TO_LOCALE[lang], code)
        return ("en_US", code)

    # Two-letter language code
    lc = c.lower()
    if lc in _LANG_TO_LOCALE:
        return (_LANG_TO_LOCALE[lc], None)

    # Unknown -> fallback to en_US
    return ("en_US", code)


def _parse_iso_date(s: Optional[str]) -> Optional[datetime]:
    if not s:
        return None
    try:
        # 2025-09-08T17:01:42
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except Exception:
        try:
            # 2025-09-08T17:01:42Z or with timezone
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        except Exception:
            return None


def _parse_dpd_status_date(s: Optional[str]) -> Optional[datetime]:
    # Example: "09.09.2025, 11:19"
    if not s:
        return None
    for fmt in ("%d.%m.%Y, %H:%M", "%d.%m.%Y %H:%M"):
        try:
            return datetime.strptime(s, fmt)
        except Exception:
            continue
    return None


def _coerce_timestamp(ev: Dict[str, Any]) -> Optional[datetime]:
    for k in ("timestamp", "date", "eventDate", "time", "dateTime"):
        val = ev.get(k)
        if not val:
            continue
        if isinstance(val, (int, float)):
            # Assume epoch seconds/ms
            try:
                if val > 10_000_000_000:  # ms
                    return datetime.fromtimestamp(val / 1000)
                return datetime.fromtimestamp(val)
            except Exception:
                continue
        if isinstance(val, str):
            for fmt in (
                "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S",
                "%d/%m/%Y %H:%M",
                "%d/%m/%Y",
            ):
                try:
                    return datetime.strptime(val, fmt)
                except Exception:
                    continue
    return None


def _coerce_status_text(ev: Dict[str, Any]) -> Optional[str]:
    for k in ("status", "description", "state", "statusText"):
        v = ev.get(k)
        if isinstance(v, str):
            return v
    return None


def _coerce_details(ev: Dict[str, Any]) -> Optional[str]:
    for k in ("details", "comment", "message", "extra"):
        v = ev.get(k)
        if isinstance(v, str):
            return v
    return None
