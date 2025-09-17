"""Correos (Spanish postal service) tracking provider.

Correos provides a public API for tracking shipments without authentication.
The API supports multiple languages and returns detailed tracking events.
"""

import httpx
from datetime import datetime
from typing import Dict, Any, Optional
from urllib.parse import quote

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import get_with_retries, async_get_with_retries
from .base import ProviderBase

# Correos Public API endpoint (no authentication required)
BASE_URL = "https://api1.correos.es/digital-services/searchengines/api/v1/envios"

# Supported languages (discovered via testing)
# - EN: English
# - ES: Spanish (default if language param omitted)
# - FR: French
# Other language codes (DE, IT, etc.) return HTTP 500 errors
SUPPORTED_LANGUAGES = ["EN", "ES", "FR"]


def track(shipment_code: str, language: str = "EN") -> TrackingResponse:
    """Fetch tracking info for a Correos shipment.

    API URL Format: https://api1.correos.es/digital-services/searchengines/api/v1/envios (GET)

    API Requirements (discovered via testing):
    - No authentication required
    - Required parameters:
      * text: The tracking number (required, cannot be empty)
      * language: Optional, defaults to "ES" if omitted

    Parameter behavior:
    - Missing 'text': Returns 400 Bad Request with Spanish error message
    - Empty 'text': Returns 400 "El valor código de envío introducido no es correcto: ''"
    - Invalid tracking: Returns HTML error page (not JSON)
    - Whitespace in tracking: Automatically trimmed by API
    - Language support: EN, ES, FR work; DE and others return 500 error

    Headers:
    - User-Agent: Optional, any value accepted
    - Accept: Optional, but text/html returns HTML error pages
    - No authentication headers required

    Response format:
    - Success: JSON with type, expedition, shipment[], and others fields
    - Error: Either JSON error (400) or HTML page (invalid tracking)
    - Timeout: Some invalid formats cause very long timeouts

    Error handling:
    - 400: Bad request with JSON error message
    - 500: Server error for unsupported languages
    - HTML response: Invalid tracking number format
    - Timeout: Some malformed tracking numbers cause long delays

    Server selection:
    - Not applicable - single endpoint only

    Args:
        shipment_code: The tracking number to look up
        language: Language code (EN, ES, or FR recommended)

    Returns:
        TrackingResponse with normalized tracking data
    """
    # Build request parameters
    params = {
        "text": shipment_code,  # Required: tracking number
        "language": language,  # Optional: defaults to ES if omitted
    }

    # Headers to ensure JSON response (not strictly required but recommended)
    # Without Accept: application/json, invalid tracking returns HTML errors
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",  # Optional
        "Accept": "application/json",  # Recommended to avoid HTML error pages
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
    """Async version of Correos tracking.

    See track() for detailed API requirements and behavior.
    """
    # Build request parameters (same as sync version)
    params = {
        "text": shipment_code,  # Required: tracking number
        "language": language,  # Optional: defaults to ES if omitted
    }

    # Headers to ensure JSON response
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",  # Optional
        "Accept": "application/json",  # Recommended to avoid HTML error pages
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
    """Normalize Correos API response to universal TrackingResponse model.

    API Response Structure:
    - type: "envio" for shipments
    - expedition: Usually None for single shipments
    - shipment: Array of shipment objects
    - others: Contains offices[], mailboxes[], citypaqs[] arrays

    Shipment Object Fields:
    - shipmentCode: The tracking number
    - events: Array of tracking events
    - associatedShipments: Related tracking numbers
    - pendingCustomsPay, stateCode, date_delivery_sum: Additional status info
    - error: {errorCode, errorDesc} for shipment-level errors
    - customs: Customs-related information
    - modify: Modification options

    Event Object Fields:
    - eventDate: Date in DD/MM/YYYY format
    - eventTime: Time in HH:MM:SS format
    - phase: Numeric phase (1=initial, higher=later stages)
    - colour: Status color code (V=green, etc.)
    - summaryText: Brief status description (language-dependent)
    - extendedText: Detailed status description
    - codired: Location/office code (when available)
    """
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
    """Parse Correos date and time strings into datetime object.

    Correos date format: DD/MM/YYYY (e.g., "08/09/2025")
    Correos time format: HH:MM:SS (e.g., "11:48:03")

    Note: Correos timestamps don't include timezone information,
    so we assume local Spanish time (would need pytz for proper handling).
    """
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
    """Infer shipment status from Correos events.

    Status mapping based on summaryText keywords:
    - "entregado"/"delivered": Package delivered
    - "reparto"/"delivery": Out for delivery
    - "transito"/"transit": In transit
    - "admitido"/"received"/"information": Initial information received

    Note: Status text varies by language parameter, so we check
    for both Spanish and English keywords.
    """
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


def build_tracking_url(shipment_code: str, *, language: str = "ES") -> Optional[str]:
    """Return a human-facing Correos tracking URL for this shipment.

    Correos public locator pattern:
    https://www.correos.es/es/es/herramientas/localizador/envios/detalle?tracking-number={shipment_code}
    """
    return f"https://www.correos.es/es/es/herramientas/localizador/envios/detalle?tracking-number={quote(shipment_code)}"


class CorreosProvider(ProviderBase):
    """Thin wrapper around module-level functions, preserving original docs.

    Provides a class interface compatible with ProviderBase without changing
    public module-level APIs or documentation.
    """

    provider = "correos"

    def build_tracking_url(
        self, tracking_number: str, *, language: Optional[str] = None, **kwargs: Any
    ) -> Optional[str]:
        return build_tracking_url(tracking_number, language=(language or "ES").upper())

    def track(
        self, tracking_number: str, *, language: str = "ES", **kwargs: Any
    ) -> TrackingResponse:
        return track(shipment_code=tracking_number, language=(language or "ES").upper())

    async def track_async(
        self,
        tracking_number: str,
        *,
        language: str = "ES",
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> TrackingResponse:
        return await track_async(
            shipment_code=tracking_number,
            language=(language or "ES").upper(),
            client=client,
        )
