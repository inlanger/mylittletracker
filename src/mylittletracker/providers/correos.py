import httpx
from datetime import datetime
from typing import Dict, Any, Optional

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import get_with_retries, async_get_with_retries

BASE_URL = "https://api1.correos.es/digital-services/searchengines/api/v1/envios"


def track(shipment_code: str, language: str = "EN") -> TrackingResponse:
    """Fetch tracking info for a Correos shipment.

    Returns normalized tracking data as a TrackingResponse model.
    """
    params = {"text": shipment_code, "language": language}
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
    }

    response = get_with_retries(BASE_URL, params=params, headers=headers, timeout=20.0)
    raw_data = response.json()

    return normalize_correos_response(raw_data, shipment_code)


async def track_async(
    shipment_code: str,
    language: str = "EN",
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> TrackingResponse:
    """Async version of Correos tracking."""
    params = {"text": shipment_code, "language": language}
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
    }
    if client is None:
        resp = await async_get_with_retries(
            BASE_URL, params=params, headers=headers, timeout=20.0
        )
    else:
        resp = await async_get_with_retries(
            BASE_URL, params=params, headers=headers, timeout=20.0, client=client
        )
    raw_data = resp.json()
    return normalize_correos_response(raw_data, shipment_code)


def normalize_correos_response(
    raw_data: Dict[str, Any], tracking_number: str
) -> TrackingResponse:
    """Normalize Correos API response to universal TrackingResponse model."""
    shipments: list[Shipment] = []

    # Extract shipment data from Correos format
    shipment_list = raw_data.get("shipment", [])
    if not shipment_list:
        # No shipments found
        return TrackingResponse(shipments=shipments, provider="correos")

    correos_shipment = shipment_list[0]  # Take first shipment
    events = []

    # Convert events
    for event in correos_shipment.get("events", []):
        # Parse date and time
        event_date = event.get("eventDate", "")
        event_time = event.get("eventTime", "")

        # Combine date and time into datetime
        timestamp = _parse_correos_datetime(event_date, event_time)

        tracking_event = TrackingEvent(
            timestamp=timestamp,
            status=event.get("summaryText", ""),
            details=event.get("extendedText"),
            location=None,  # Correos doesn't seem to provide location separately
            status_code=str(event.get("eventCode", "")) or None,
            extras=None,
        )
        events.append(tracking_event)

    # Sort events chronologically for consistency
    events.sort(key=lambda e: e.timestamp)

    # Determine overall shipment status from latest event
    status = _infer_correos_status(events)

    shipment = Shipment(
        tracking_number=correos_shipment.get("shipmentCode", tracking_number),
        carrier="correos",
        status=status,
        events=events,
        service_type=None,
        origin=None,
        destination=None,
        estimated_delivery=None,
        actual_delivery=None,
        extras=None,
    )

    shipments.append(shipment)

    return TrackingResponse(shipments=shipments, provider="correos")


def _parse_correos_datetime(date_str: str, time_str: str) -> datetime:
    """Parse Correos date and time strings into datetime object."""
    try:
        # Assume format like "DD/MM/YYYY" and "HH:MM"
        if date_str and time_str:
            datetime_str = f"{date_str} {time_str}"
            return datetime.strptime(datetime_str, "%d/%m/%Y %H:%M")
        elif date_str:
            return datetime.strptime(date_str, "%d/%m/%Y")
        else:
            return datetime.now()
    except ValueError:
        # Fallback if parsing fails
        return datetime.now()


def _infer_correos_status(events: list[TrackingEvent]) -> ShipmentStatus:
    """Infer shipment status from Correos events."""
    if not events:
        return ShipmentStatus.UNKNOWN

    # Check latest event for delivery-related keywords
    latest_status = events[-1].status.lower()

    if "entregado" in latest_status or "delivered" in latest_status:
        return ShipmentStatus.DELIVERED
    elif "reparto" in latest_status or "delivery" in latest_status:
        return ShipmentStatus.OUT_FOR_DELIVERY
    elif "transito" in latest_status or "transit" in latest_status:
        return ShipmentStatus.IN_TRANSIT
    elif "admitido" in latest_status or "received" in latest_status:
        return ShipmentStatus.INFORMATION_RECEIVED
    else:
        return ShipmentStatus.UNKNOWN  # Default fallback for unmapped statuses
