"""DPD tracking provider using the public PLC (Parcel Life Cycle) API.

DPD provides a REST API for tracking parcels without authentication.
The API supports multiple locales and returns detailed tracking events.
"""

from datetime import datetime
from typing import Any, Dict, Optional, Tuple

import httpx
from urllib.parse import quote

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import get_with_retries, async_get_with_retries
from .base import ProviderBase


# DPD Public PLC (Parcel Life Cycle) API endpoint
# No authentication required - public tracking endpoint
REST_BASE = "https://tracking.dpd.de/rest/plc"

# Supported PLC locales (discovered via testing)
# The API accepts locale codes in format: language_COUNTRY
# Invalid/unsupported locales fallback to English (en_US)
# Note: Locale codes are case-sensitive (must be lowercase_UPPERCASE)
_SUPPORTED_LOCALES = {
    "en_US",  # English (fallback for invalid locales)
    "de_DE",  # German
    "fr_FR",  # French
    "es_ES",  # Spanish
    "it_IT",  # Italian (returns English content)
    "nl_NL",  # Dutch
    "pl_PL",  # Polish
    "cs_CZ",  # Czech
}

# Simple language code to locale mapping
# Used when user provides 2-letter language codes
_LANG_TO_LOCALE = {
    "en": "en_US",
    "de": "de_DE",
    "fr": "fr_FR",
    "es": "es_ES",
    "it": "it_IT",
    "nl": "nl_NL",
    "pl": "pl_PL",
    "cs": "cs_CZ",
}


def track(parcel_number: str, *, language: str = "EN") -> TrackingResponse:
    """Retrieve DPD tracking using the public PLC JSON endpoint.

    API URL Format: https://tracking.dpd.de/rest/plc/{locale}/{parcelNumber} (GET)

    API Requirements (discovered via testing):
    - No authentication required
    - URL parameters:
      * locale: Required, format must be language_COUNTRY (e.g., en_US)
      * parcelNumber: Required, the tracking number

    Parameter behavior:
    - Locale: Must be in format lowercase_UPPERCASE (e.g., en_US, de_DE)
    - Invalid case (EN_US, en_us) returns 500 or 429 errors
    - Unsupported locales (xx_XX, en_GB, etc.) fallback to English
    - Missing locale in URL causes 302 redirect
    - Short forms (en, EN) cause 429 Too Many Requests errors
    - Tracking number: Invalid format returns 302 redirect (not JSON)
    - Non-existent but valid format returns empty scan events
    - All zeros (00000000000000) returns valid JSON structure
    - Missing tracking number causes 302 redirect

    Headers:
    - Accept: Optional, API always returns JSON regardless
    - User-Agent: Optional, any value accepted
    - No authentication headers required

    Response format:
    - Success: JSON with parcellifecycleResponse.parcelLifeCycleData
    - Invalid tracking: 302 redirect (HTML page)
    - Server errors: 500 for invalid locale case, 429 for rate limiting

    Response structure:
    - parcellifecycleResponse.parcelLifeCycleData contains:
      * shipmentInfo: Parcel metadata
      * statusInfo: Array of status milestones
      * scanInfo.scan: Array of detailed scan events
      * contactInfo: Contact information

    Error handling:
    - 302: Redirect for invalid tracking or missing parameters
    - 429: Too Many Requests for rate limiting
    - 500: Internal Server Error for invalid locale format
    - Non-JSON response: Invalid tracking format

    Server selection:
    - Not applicable - single endpoint only

    Args:
        parcel_number: The DPD tracking number
        language: Language code (2-letter) or locale (language_COUNTRY)

    Returns:
        TrackingResponse with normalized tracking data
    """
    lang_code_raw = language or "EN"
    lang_code = lang_code_raw.strip()
    locale, normalized_from = _resolve_locale(lang_code)

    # Headers (optional but recommended for clarity)
    # The API returns JSON regardless of Accept header
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",  # Optional
        "Accept": "application/json",  # Optional, API always returns JSON or redirects
    }

    # Build API URL with locale and tracking number
    rest_url = f"{REST_BASE}/{locale}/{parcel_number}"

    # Make API request
    resp = get_with_retries(rest_url, headers=headers, timeout=20.0)

    # Check response type
    # Invalid tracking numbers cause 302 redirects to HTML pages
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" not in ctype.lower():
        # Not JSON = invalid tracking or redirect
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
    """Normalize JSON from /rest/plc/{locale}/{parcelLabelNumber} to our model.

    PLC Response Structure:
    - parcellifecycleResponse.parcelLifeCycleData contains:
      * shipmentInfo: Metadata (parcelLabelNumber, productName, sortingCode, etc.)
      * statusInfo: Array of status milestones with descriptions
      * scanInfo.scan: Array of detailed scan events with timestamps
      * contactInfo: Contact details (usually empty)

    StatusInfo fields:
    - status: Status code (ACCEPTED, ON_THE_ROAD, DELIVERED, etc.)
    - label: Brief status label
    - description.content: Array with detailed description text
    - statusHasBeenReached: Boolean indicating if status was reached
    - isCurrentStatus: Boolean for current status
    - location: Location name
    - depot: {businessUnit, number} depot information
    - date: Date/time string in "DD.MM.YYYY, HH:MM" format

    ScanInfo.scan fields:
    - date: ISO timestamp (YYYY-MM-DDTHH:MM:SS)
    - scanData.location: Scan location
    - scanDescription.content: Array with scan description text
    - scanType.name: Type of scan event
    """
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
    """Async version using the public PLC JSON endpoint.

    See track() for detailed API requirements and behavior.
    """
    lang_code_raw = language or "EN"
    lang_code = lang_code_raw.strip()
    locale, normalized_from = _resolve_locale(lang_code)

    # Headers (same as sync version)
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",  # Optional
        "Accept": "application/json",  # Optional, API always returns JSON or redirects
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


def build_tracking_url(parcel_number: str, *, language: str = "EN") -> Optional[str]:
    """Return a human-facing DPD tracking URL for this shipment.

    Pattern: https://tracking.dpd.de/status/{locale}/parcel/{parcel_number}
    Locale is resolved using the same mapping as the PLC API.
    """
    lang_code_raw = language or "EN"
    lang_code = lang_code_raw.strip()
    locale, _ = _resolve_locale(lang_code)
    return f"https://tracking.dpd.de/status/{locale}/parcel/{quote(parcel_number)}"


def _resolve_locale(lang_code: str) -> Tuple[str, Optional[str]]:
    """Resolve an input language or locale to a supported PLC locale.

    The DPD API is strict about locale format:
    - Must be exactly language_COUNTRY format (e.g., en_US)
    - Case sensitive: lowercase language, UPPERCASE country
    - Invalid formats cause 500 or 429 errors
    - Unsupported but valid formats fallback to English

    Resolution strategy:
    1. Check if input is already a supported locale
    2. Try to parse as language_COUNTRY and validate
    3. Map 2-letter language codes to default locales
    4. Fallback to en_US for anything unrecognized

    Returns:
        Tuple of (locale, normalized_from)
        normalized_from is the original input if normalization occurred, else None
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


class DPDProvider(ProviderBase):
    """Thin wrapper around module-level DPD functions with URL builder."""

    provider = "dpd"

    def language_to_locale(self, language: Optional[str]) -> str:
        # Leverage existing resolver for consistency with API behavior
        loc, _ = _resolve_locale((language or "EN").strip())
        return loc

    def build_tracking_url(
        self, tracking_number: str, *, language: Optional[str] = None, **kwargs: Any
    ) -> Optional[str]:
        lang = (language or "EN").strip()
        return build_tracking_url(tracking_number, language=lang)

    def track(
        self, tracking_number: str, *, language: str = "EN", **kwargs: Any
    ) -> TrackingResponse:
        return track(parcel_number=tracking_number, language=language)

    async def track_async(
        self,
        tracking_number: str,
        *,
        language: str = "EN",
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> TrackingResponse:
        return await track_async(
            parcel_number=tracking_number, language=language, client=client
        )


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
    """Parse DPD status date format.

    DPD date format in statusInfo: "DD.MM.YYYY, HH:MM"
    Example: "09.09.2025, 11:19"

    Note: DPD timestamps don't include timezone information.
    Times appear to be in local depot timezone.
    """
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
