import os
import httpx
from datetime import datetime
from typing import Any, Dict, Optional

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import (
    parse_dt_iso,
    get_with_retries,
    async_get_with_retries,
    map_status_from_text,
)

TEST_BASE = "https://api-test.dhl.com/track/shipments"
PROD_BASE = "https://api-eu.dhl.com/track/shipments"


def _base_url(server: str) -> str:
    return TEST_BASE if server.lower() == "test" else PROD_BASE


def _map_utapi_status_code(code: Optional[str]) -> ShipmentStatus:
    """Map DHL UTAPI statusCode to unified ShipmentStatus per spec."""
    if not code:
        return ShipmentStatus.UNKNOWN
    c = code.lower()
    if c == "delivered":
        return ShipmentStatus.DELIVERED
    if c == "failure":
        return ShipmentStatus.EXCEPTION
    if c == "pre-transit":
        return ShipmentStatus.INFORMATION_RECEIVED
    if c == "transit":
        return ShipmentStatus.IN_TRANSIT
    return ShipmentStatus.UNKNOWN


def _looks_like_short_code(s: Optional[str]) -> bool:
    v = s or ""
    return bool(s) and len(v) <= 3 and v.isupper()


def _select_event_text(e: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """Choose human-friendly event status and details using UTAPI fields.

    Prefer descriptive text when the provided `status` is a short code (e.g., ZN, PO, EE).
    Compose details from description/statusDetailed/remark/nextSteps as available.
    """
    status_text = (e.get("status") or "").strip()
    desc = (e.get("description") or e.get("statusDetailed") or "").strip()
    next_steps = (e.get("nextSteps") or "").strip()
    remark = (e.get("remark") or "").strip()

    if _looks_like_short_code(status_text) and desc:
        status_out = desc
    else:
        status_out = status_text or desc

    details_parts: list[str] = []
    # Only add desc if it's not already used as status
    if desc and status_out != desc:
        details_parts.append(desc)
    if next_steps:
        details_parts.append(f"Next: {next_steps}")
    if remark:
        details_parts.append(f"Remark: {remark}")

    details_out = " | ".join(details_parts) if details_parts else None
    return status_out, details_out


def track(
    tracking_number: str,
    *,
    language: str = "en",
    service: Optional[str] = None,
    requester_country_code: Optional[str] = None,
    origin_country_code: Optional[str] = None,
    recipient_postal_code: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    server: Optional[str] = None,
) -> TrackingResponse:
    """Fetch tracking info for a DHL shipment using Unified Shipment Tracking API.

    Returns normalized tracking data as a TrackingResponse model.
    """
    api_key = os.getenv("DHL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DHL_API_KEY is not set. Add it to your environment or .env file."
        )

    server = server or os.getenv("DHL_SERVER", "prod") or "prod"
    params: Dict[str, Any] = {
        "trackingNumber": tracking_number,
        "language": language,
    }
    if service:
        params["service"] = service
    if requester_country_code:
        params["requesterCountryCode"] = requester_country_code
    if origin_country_code:
        params["originCountryCode"] = origin_country_code
    if recipient_postal_code:
        params["recipientPostalCode"] = recipient_postal_code
    if offset is not None:
        params["offset"] = offset
    # UTAPI default limit is 5; request more history unless caller overrides
    params["limit"] = 50 if limit is None else limit

    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
        "DHL-API-Key": api_key,
    }

    response = get_with_retries(
        _base_url(server), params=params, headers=headers, timeout=20.0
    )
    raw_data = response.json()

    return normalize_dhl_response(raw_data, tracking_number)


async def track_async(
    tracking_number: str,
    *,
    language: str = "en",
    service: Optional[str] = None,
    requester_country_code: Optional[str] = None,
    origin_country_code: Optional[str] = None,
    recipient_postal_code: Optional[str] = None,
    offset: Optional[int] = None,
    limit: Optional[int] = None,
    server: Optional[str] = None,
    client: Optional[httpx.AsyncClient] = None,
) -> TrackingResponse:
    """Async version of DHL tracking."""
    api_key = os.getenv("DHL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DHL_API_KEY is not set. Add it to your environment or .env file."
        )

    server = server or os.getenv("DHL_SERVER", "prod") or "prod"
    params: Dict[str, Any] = {
        "trackingNumber": tracking_number,
        "language": language,
    }
    if service:
        params["service"] = service
    if requester_country_code:
        params["requesterCountryCode"] = requester_country_code
    if origin_country_code:
        params["originCountryCode"] = origin_country_code
    if recipient_postal_code:
        params["recipientPostalCode"] = recipient_postal_code
    if offset is not None:
        params["offset"] = offset
    # UTAPI default limit is 5; request more history unless caller overrides
    params["limit"] = 50 if limit is None else limit

    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
        "DHL-API-Key": api_key,
    }

    response = await async_get_with_retries(
        _base_url(server), params=params, headers=headers, timeout=20.0, client=client
    )
    raw_data = response.json()

    return normalize_dhl_response(raw_data, tracking_number)


def normalize_dhl_response(
    raw_data: Dict[str, Any], tracking_number: str
) -> TrackingResponse:
    """Normalize DHL API response to universal TrackingResponse model."""
    shipments: list[Shipment] = []

    # Extract shipment data from DHL format
    shipment_list = raw_data.get("shipments", [])
    if not shipment_list:
        # No shipments found
        return TrackingResponse(shipments=shipments, provider="dhl")

    dhl_shipment = shipment_list[0]  # Take first shipment
    events: list[TrackingEvent] = []
    # Convert events
    for ev in dhl_shipment.get("events", []):
        # Parse timestamp
        timestamp = parse_dt_iso(ev.get("timestamp", "")) or datetime.now()

        # Choose human-readable status and details
        status_text, details_text = _select_event_text(ev)

        # Build location string (prefer locality,country; fallback to servicePoint label)
        location = None
        loc_addr = (ev.get("location") or {}).get("address") or {}
        locality = loc_addr.get("addressLocality")
        country = loc_addr.get("countryCode")
        if locality and country:
            location = f"{locality}, {country}"
        elif locality:
            location = locality
        else:
            sp = (ev.get("location") or {}).get("servicePoint") or {}
            label = sp.get("label")
            if label:
                location = label

        tracking_event = TrackingEvent(
            timestamp=timestamp,
            status=status_text,
            location=location,
            details=details_text,
            status_code=(ev.get("statusCode") or None),
            extras=None,
        )
        events.append(tracking_event)

    # Sort events for consistency (ascending time)
    events.sort(key=lambda e: e.timestamp)

    # Determine overall shipment status (prefer shipment.status.statusCode)
    status = _infer_dhl_status(dhl_shipment, events)

    # Extract additional shipment details
    details = dhl_shipment.get("details", {})
    service_type = (details.get("product") or {}).get("productName")
    # Extract origin and destination
    origin = None
    destination = None
    if "origin" in details:
        origin_addr = details["origin"].get("address") or {}
        origin_locality = origin_addr.get("addressLocality", "")
        origin_country = origin_addr.get("countryCode", "")
        origin = f"{origin_locality}, {origin_country}".strip(", ")

    if "destination" in details:
        dest_addr = details["destination"].get("address") or {}
        dest_locality = dest_addr.get("addressLocality", "")
        dest_country = dest_addr.get("countryCode", "")
        destination = f"{dest_locality}, {dest_country}".strip(", ")

    shipment = Shipment(
        tracking_number=dhl_shipment.get("id", tracking_number),
        carrier="dhl",
        status=status,
        events=events,
        service_type=service_type,
        origin=origin,
        destination=destination,
        estimated_delivery=None,
        actual_delivery=None,
        extras=None,
    )

    shipments.append(shipment)

    return TrackingResponse(shipments=shipments, provider="dhl")


def _parse_dhl_timestamp(timestamp_str: str) -> datetime:
    # Deprecated in favor of utils.parse_dt_iso
    return parse_dt_iso(timestamp_str) or datetime.now()


def _infer_dhl_status(
    dhl_shipment: Dict[str, Any], events: list[TrackingEvent]
) -> ShipmentStatus:
    """Infer shipment status from DHL shipment data and events per UTAPI spec."""
    # 1) Shipment-level statusCode is canonical
    shipment_status_obj = dhl_shipment.get("status") or {}
    st_code = (shipment_status_obj.get("statusCode") or "").strip()
    mapped = _map_utapi_status_code(st_code)
    if mapped != ShipmentStatus.UNKNOWN:
        # If mapped to IN_TRANSIT but latest event text clearly indicates out-for-delivery
        if mapped == ShipmentStatus.IN_TRANSIT and events:
            latest_text = (events[-1].details or events[-1].status or "").lower()
            if any(
                phrase in latest_text
                for phrase in [
                    "out for delivery",
                    "in delivery",
                    "loaded onto the delivery vehicle",
                    "delivery vehicle",
                ]
            ):
                return ShipmentStatus.OUT_FOR_DELIVERY
        return mapped

    # 2) Fallback: latest event statusCode
    if events:
        latest = events[-1]
        if latest.status_code:
            mapped = _map_utapi_status_code(latest.status_code)
            if mapped != ShipmentStatus.UNKNOWN:
                if mapped == ShipmentStatus.IN_TRANSIT:
                    text = (latest.details or latest.status or "").lower()
                    if any(
                        phrase in text
                        for phrase in [
                            "out for delivery",
                            "in delivery",
                            "loaded onto the delivery vehicle",
                            "delivery vehicle",
                        ]
                    ):
                        return ShipmentStatus.OUT_FOR_DELIVERY
                return mapped

        # 3) Heuristic based on human-readable text
        text = latest.details or latest.status or ""
        mapped = map_status_from_text(text)
        if mapped != ShipmentStatus.UNKNOWN:
            return mapped

    return ShipmentStatus.UNKNOWN
