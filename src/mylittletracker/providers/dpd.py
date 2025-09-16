import re
from datetime import datetime
from typing import Any, Dict, Optional

import httpx
from bs4 import BeautifulSoup

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus


BASE_PAGE = (
    "https://www.dpdgroup.com/nl/mydpd/my-parcels/incoming"
)


def track(parcel_number: str, *, language: str = "EN", lang: Optional[str] = None) -> TrackingResponse:
    """Attempt to retrieve DPD tracking by scraping the public page.

    NOTE: This page is protected by anti-bot (e.g., Cloudflare). If we detect a
    challenge page or cannot find embedded JSON, we'll return an empty
    TrackingResponse and advise using an official API instead.
    """
    # prefer 'language' but allow legacy 'lang'
    lang_code = (lang or language or "EN").lower()
    url = f"{BASE_PAGE}?parcelNumber={parcel_number}&lang={lang_code}"
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    with httpx.Client(timeout=20.0, follow_redirects=True) as client:
        # 1) Try calling the JSON REST endpoint as the SPA would
        rest_base = "https://tracking.dpd.de/rest/plc"
        # Resolve locale for PLC endpoint (e.g., en_US, nl_NL, de_DE)
        locale = _resolve_locale(lang_code)
        try:
            json_headers = {
                "User-Agent": headers["User-Agent"],
                "Accept": "application/json",
            }
            rest_url = f"{rest_base}/{locale}/{parcel_number}"
            rest_resp = client.get(rest_url, headers=json_headers)
            ctype = rest_resp.headers.get("Content-Type", "")
            if rest_resp.status_code == 200 and "application/json" in ctype.lower():
                data = rest_resp.json()
                # Normalize JSON payload (PLC structure)
                try:
                    shipment = _normalize_dpd_plc_json(data, parcel_number)
                    return TrackingResponse(shipments=[shipment], provider="dpd")
                except Exception:
                    # Fallback to generic embedded normalizer
                    try:
                        shipment = _normalize_dpd_embedded(data, parcel_number)
                        return TrackingResponse(shipments=[shipment], provider="dpd")
                    except Exception:
                        pass
        except Exception:
            # ignore and fallback to HTML scraping
            pass

        # 2) Fallback to HTML page scraping
        resp = client.get(url, headers=headers)
        html = resp.text or ""

    # Detect Cloudflare/anti-bot interstitial
    if "Just a moment" in html or "challenge-platform" in html:
        # Return an empty normalized payload, but with a helpful message via status.
        shipment = Shipment(
            tracking_number=parcel_number,
            carrier="dpd",
            status=ShipmentStatus.UNKNOWN,
            events=[
                TrackingEvent(
                    timestamp=datetime.now(),
                    status="Anti-bot protection encountered. Unable to scrape page.",
                    details=(
                        "DPD public page is protected. Consider using an official DPD API "
                        "(with key) or a trusted proxy."
                    ),
                )
            ],
        )
        return TrackingResponse(shipments=[shipment], provider="dpd")

    # Try to extract embedded JSON from script tags
    soup = BeautifulSoup(html, "html.parser")

    # Heuristics: look for JSON in <script type="application/json"> blocks
    json_candidates: list[str] = []
    for script in soup.find_all("script"):
        t = script.get("type")
        if t and "json" in t.lower():
            content = script.string or script.text or ""
            if content and "parcel" in content.lower():
                json_candidates.append(content)
        else:
            # Fallback: inline scripts that assign to window.__* style variables
            content = script.string or script.text or ""
            if content and ("__APOLLO_STATE__" in content or "__NUXT__" in content or "__NEXT_DATA__" in content):
                json_candidates.append(content)

    data: Optional[Dict[str, Any]] = None
    for raw in json_candidates:
        # Try to locate a JSON object within the string
        obj = _extract_first_json_object(raw)
        if obj and isinstance(obj, dict):
            # Heuristic: look for keys that suggest parcel tracking
            if _looks_like_dpd_payload(obj):
                data = obj
                break

    if not data:
        # As a last resort, try to find any JSON snippet containing parcelNumber
        for raw in json_candidates:
            obj = _extract_first_json_object(raw)
            if obj and parcel_number in raw:
                data = obj
                break

    # Convert to normalized model if we found something meaningful
    if data:
        try:
            shipment = _normalize_dpd_embedded(data, parcel_number)
            return TrackingResponse(shipments=[shipment], provider="dpd")
        except Exception:
            pass

    # Fallback: no usable data found
    shipment = Shipment(
        tracking_number=parcel_number,
        carrier="dpd",
        status=ShipmentStatus.UNKNOWN,
        events=[
            TrackingEvent(
                timestamp=datetime.now(),
                status="No embedded tracking data found",
                details=(
                    "The public page may be a JS SPA or protected. Consider using an "
                    "official DPD API or provide a custom endpoint."
                ),
            )
        ],
    )
    return TrackingResponse(shipments=[shipment], provider="dpd")


def _extract_first_json_object(s: str) -> Optional[Dict[str, Any]]:
    """Extract and parse the first JSON object-like substring from s.

    This is a heuristic: it scans for the first '{' and attempts to parse up to
    a matching '}'. We keep it conservative to avoid heavy deps.
    """
    import json

    start = s.find("{")
    if start == -1:
        return None
    # Try progressively shorter tails to find a valid JSON
    for end in range(len(s), start + 1, -1):
        chunk = s[start:end]
        try:
            return json.loads(chunk)
        except Exception:
            continue
    return None


def _looks_like_dpd_payload(obj: Dict[str, Any]) -> bool:
    text = str(obj).lower()
    hints = [
        "parcel", "events", "status", "shipment", "tracking"
    ]
    return any(h in text for h in hints)


def _normalize_dpd_plc_json(obj: Dict[str, Any], parcel_number: str) -> Shipment:
    """Normalize JSON from /rest/plc/{locale}/{parcelLabelNumber} to our model."""
    plc = (
        obj.get("parcellifecycleResponse", {})
        .get("parcelLifeCycleData", {})
    )
    shipment_info = plc.get("shipmentInfo", {})
    status_info = plc.get("statusInfo", []) or []
    scan_info = (plc.get("scanInfo", {}) or {}).get("scan", []) or []

    events: list[TrackingEvent] = []

    # Prefer scan events for detailed timeline
    if scan_info:
        for ev in scan_info:
            ts = _parse_iso_date(ev.get("date")) or _coerce_timestamp(ev)
            desc = (ev.get("scanDescription", {}) or {}).get("content", [])
            status_text = (desc[0] if desc else None) or (ev.get("scanDescription", {}) or {}).get("label")
            location = (ev.get("scanData", {}) or {}).get("location")
            events.append(
                TrackingEvent(
                    timestamp=ts or datetime.now(),
                    status=status_text or "",
                    location=location,
                    details=status_text,
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
            status_text = (desc[0] if desc else None) or st.get("label") or st.get("status")
            location = st.get("location")
            events.append(
                TrackingEvent(
                    timestamp=ts or datetime.now(),
                    status=status_text or "",
                    location=location,
                    details=status_text,
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

    return Shipment(
        tracking_number=tracking_number,
        carrier="dpd",
        status=status_enum,
        events=events,
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
                details=details,
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
                    if any(k.lower() in ("status", "description", "state") for k in keys):
                        return v  # best guess
                queue.append(v)
        elif isinstance(item, list):
            queue.extend(item)
    return []


async def track_async(
    parcel_number: str,
    *,
    language: str = "EN",
    lang: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> TrackingResponse:
    """Async version of DPD tracking (PLC JSON first, fallback to HTML)."""
    lang_code = (lang or language or "EN").lower()
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    rest_base = "https://tracking.dpd.de/rest/plc"
    locale = _resolve_locale(lang_code)

    async def _request_plc(ac: httpx.AsyncClient) -> Optional[TrackingResponse]:
        try:
            resp = await ac.get(
                f"{rest_base}/{locale}/{parcel_number}",
                headers={"User-Agent": headers["User-Agent"], "Accept": "application/json"},
                timeout=20.0,
            )
            ctype = resp.headers.get("Content-Type", "")
            if resp.status_code == 200 and "application/json" in ctype.lower():
                data = resp.json()
                try:
                    shipment = _normalize_dpd_plc_json(data, parcel_number)
                except Exception:
                    shipment = _normalize_dpd_embedded(data, parcel_number)
                return TrackingResponse(shipments=[shipment], provider="dpd")
        except Exception:
            return None
        return None

    if client is None:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as ac:
            plc = await _request_plc(ac)
            if plc is not None:
                return plc
            # Fallback to HTML scrape path
            return track(parcel_number, language=language, lang=lang)
    else:
        plc = await _request_plc(client)
        if plc is not None:
            return plc
        # Fallback to sync HTML scrape as the last resort
        return track(parcel_number, language=language, lang=lang)


def _resolve_locale(lang_code: str) -> str:
    # Normalize to language_REGION (e.g., en_US)
    code = lang_code.strip()
    if "_" in code:
        parts = code.split("_", 1)
        lang = parts[0].lower()
        region = parts[1].upper()
        return f"{lang}_{region}"
    lc = code.lower()
    mapping = {
        "en": "en_US",
        "nl": "nl_NL",
        "de": "de_DE",
        "fr": "fr_FR",
        "it": "it_IT",
        "es": "es_ES",
    }
    return mapping.get(lc, f"{lc}_{lc.upper()}")


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
            for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S", "%d/%m/%Y %H:%M", "%d/%m/%Y"):
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
