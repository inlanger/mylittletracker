"""DHL tracking provider using the Unified Tracking API (UTAPI).

DHL provides a comprehensive REST API for tracking shipments across all DHL services.
The API requires authentication via API key and supports multiple DHL divisions.

API Documentation: https://developer.dhl.com/api-reference/shipment-tracking
OpenAPI Spec Version: 1.5.6
"""

import os
import httpx
from datetime import datetime
from typing import Any, Dict, Optional
from urllib.parse import quote

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import (
    parse_dt_iso,
    get_with_retries,
    async_get_with_retries,
    map_status_from_text,
)
from .base import ProviderBase

# DHL Unified Tracking API endpoints
# Test server for development/testing (requires test API key)
TEST_BASE = "https://api-test.dhl.com/track/shipments"
# Production server for live tracking (requires production API key)
PROD_BASE = "https://api-eu.dhl.com/track/shipments"

# Supported DHL services/divisions (from OpenAPI spec)
# Each service represents a different DHL business unit:
# - express: DHL Express (time-definite international)
# - freight: DHL Freight (road freight)
# - parcel-de/nl/pl/uk: DHL Parcel regional services
# - ecommerce: DHL eCommerce
# - dgf: DHL Global Forwarding
# - post-de: Deutsche Post (German postal service)
DHL_SERVICES = [
    "dgf",
    "dsc",
    "ecommerce",
    "ecommerce-apac",
    "ecommerce-europe",
    "ecommerce-ppl",
    "ecommerce-iberia",
    "express",
    "freight",
    "parcel-de",
    "parcel-nl",
    "parcel-pl",
    "parcel-uk",
    "post-de",
    "post-international",
    "sameday",
    "svb",
]


def _base_url(server: str) -> str:
    return TEST_BASE if server.lower() == "test" else PROD_BASE


def _map_utapi_status_code(code: Optional[str]) -> ShipmentStatus:
    """Map DHL UTAPI statusCode to unified ShipmentStatus.

    DHL UTAPI defines 5 status codes (from OpenAPI spec):
    - delivered: Successfully delivered to recipient
    - failure: Delivery failed or exception occurred
    - pre-transit: Label created, awaiting pickup
    - transit: In transit between facilities
    - unknown: Status cannot be determined
    """
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


def build_tracking_url(tracking_number: str, *, language: str = "en") -> Optional[str]:
    """Return a human-facing DHL tracking URL for this shipment.

    Global tracker pattern:
    https://www.dhl.com/track?tracking-id={id}&language={lang2}
    """
    lang2 = (language or "en").strip().lower()[:2]
    return f"https://www.dhl.com/track?tracking-id={quote(tracking_number)}&language={lang2}"


class DHLProvider(ProviderBase):
    """Thin wrapper around module-level DHL functions with URL builder."""

    provider = "dhl"

    def build_tracking_url(
        self, tracking_number: str, *, language: Optional[str] = None, **kwargs: Any
    ) -> Optional[str]:
        return build_tracking_url(tracking_number, language=language or "en")

    def track(
        self, tracking_number: str, *, language: str = "en", **kwargs: Any
    ) -> TrackingResponse:
        return track(tracking_number, language=language, **kwargs)

    async def track_async(
        self,
        tracking_number: str,
        *,
        language: str = "en",
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> TrackingResponse:
        return await track_async(
            tracking_number, language=language, client=client, **kwargs
        )


def _looks_like_short_code(s: Optional[str]) -> bool:
    """Check if status text looks like a short code.

    DHL sometimes returns cryptic 2-3 letter codes (e.g., ZN, PO, EE)
    instead of human-readable status descriptions. When this happens,
    we prefer using the description field instead.
    """
    v = s or ""
    return bool(s) and len(v) <= 3 and v.isupper()


def _select_event_text(e: Dict[str, Any]) -> tuple[str, Optional[str]]:
    """Choose human-friendly event status and details from UTAPI event fields.

    UTAPI event fields (from OpenAPI spec):
    - status: Short status description/title (sometimes just a code)
    - description: Human-readable detailed description
    - statusDetailed: Detailed status of the shipment
    - remark: Additional remark regarding status
    - nextSteps: Description of next steps

    Strategy:
    1. Use description as status if status is a short code
    2. Combine remaining fields into details
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
    """Fetch tracking info for a DHL shipment using Unified Tracking API.

    API URL Format: https://api-eu.dhl.com/track/shipments (GET) or test server

    API Requirements (from OpenAPI spec v1.5.6):
    - Requires DHL-API-Key header authentication
    - API key must be set in DHL_API_KEY environment variable
    - Different keys needed for test vs production servers

    Parameter behavior:
    - trackingNumber: Required, the shipment tracking number
    - service: Optional hint for DHL service (express, parcel-de, etc.)
    - language: ISO 639-1 2-char language code (default: en)
    - requesterCountryCode: ISO 3166-1 alpha-2 country of API consumer
    - originCountryCode: ISO 3166-1 alpha-2 shipment origin country
    - recipientPostalCode: Postal code for additional qualification
      * Required for parcel-nl and parcel-de to get full data
    - offset: Pagination offset (default: 0)
    - limit: Max results to retrieve (default: 5, we override to 50)

    Headers:
    - DHL-API-Key: Required, authentication key
    - Accept: Optional, recommended application/json
    - User-Agent: Optional, client identification

    Response format:
    - Success: JSON with shipments array
    - 404: Shipment not found
    - 401: Unauthorized (invalid/missing API key)

    Response structure:
    - shipments array with matching shipments
    - Each shipment contains events array (chronological)
    - Status codes: delivered, failure, pre-transit, transit, unknown
    - Includes origin/destination, service details, and references

    Error handling:
    - 404: Shipment not found
    - 401: Invalid or missing API key
    - Returns empty shipments array on error

    Server selection:
    - Production: https://api-eu.dhl.com (default)
    - Test: https://api-test.dhl.com (for development)
    - Controlled via DHL_SERVER env var or server parameter

    Args:
        tracking_number: DHL tracking number
        language: Response language (en, de, etc.)
        service: DHL service hint (express, parcel-de, etc.)
        requester_country_code: Country code of API consumer
        origin_country_code: Shipment origin country
        recipient_postal_code: Recipient postal code
        offset: Pagination offset
        limit: Max events to retrieve
        server: 'test' or 'prod' (default: prod)

    Returns:
        TrackingResponse with normalized tracking data

    Raises:
        RuntimeError: If DHL_API_KEY environment variable not set
    """
    # API key is required for authentication
    # Get from environment variable (can be set in .env file)
    api_key = os.getenv("DHL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DHL_API_KEY is not set. Add it to your environment or .env file."
        )

    # Select server: test or production
    # Can be overridden via DHL_SERVER env var or server parameter
    server = server or os.getenv("DHL_SERVER", "prod") or "prod"

    # Build query parameters according to UTAPI spec
    params: Dict[str, Any] = {
        "trackingNumber": tracking_number,  # Required
        "language": language,  # Default: en
    }

    # Optional parameters to refine search
    if service:
        params["service"] = service  # Hint which DHL division
    if requester_country_code:
        params["requesterCountryCode"] = requester_country_code  # Optimize response
    if origin_country_code:
        params["originCountryCode"] = origin_country_code  # Qualify tracking number
    if recipient_postal_code:
        params["recipientPostalCode"] = (
            recipient_postal_code  # Required for some services
        )
    if offset is not None:
        params["offset"] = offset  # Pagination

    # UTAPI default limit is 5; request more history unless caller overrides
    # This ensures we get complete tracking history
    params["limit"] = 50 if limit is None else limit

    # Required headers for UTAPI
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",  # Identify client
        "Accept": "application/json",  # Request JSON response
        "DHL-API-Key": api_key,  # Required authentication
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
    """Async version of DHL tracking.

    See track() for detailed API documentation and parameter descriptions.
    """
    # API key is required for authentication
    # Get from environment variable (can be set in .env file)
    api_key = os.getenv("DHL_API_KEY")
    if not api_key:
        raise RuntimeError(
            "DHL_API_KEY is not set. Add it to your environment or .env file."
        )

    # Select server: test or production
    # Can be overridden via DHL_SERVER env var or server parameter
    server = server or os.getenv("DHL_SERVER", "prod") or "prod"

    # Build query parameters according to UTAPI spec
    params: Dict[str, Any] = {
        "trackingNumber": tracking_number,  # Required
        "language": language,  # Default: en
    }

    # Optional parameters to refine search
    if service:
        params["service"] = service  # Hint which DHL division
    if requester_country_code:
        params["requesterCountryCode"] = requester_country_code  # Optimize response
    if origin_country_code:
        params["originCountryCode"] = origin_country_code  # Qualify tracking number
    if recipient_postal_code:
        params["recipientPostalCode"] = (
            recipient_postal_code  # Required for some services
        )
    if offset is not None:
        params["offset"] = offset  # Pagination

    # UTAPI default limit is 5; request more history unless caller overrides
    # This ensures we get complete tracking history
    params["limit"] = 50 if limit is None else limit

    # Required headers for UTAPI
    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",  # Identify client
        "Accept": "application/json",  # Request JSON response
        "DHL-API-Key": api_key,  # Required authentication
    }

    response = await async_get_with_retries(
        _base_url(server), params=params, headers=headers, timeout=20.0, client=client
    )
    raw_data = response.json()

    return normalize_dhl_response(raw_data, tracking_number)


def normalize_dhl_response(
    raw_data: Dict[str, Any], tracking_number: str
) -> TrackingResponse:
    """Normalize DHL UTAPI response to universal TrackingResponse model.

    UTAPI Response Structure (from OpenAPI spec):
    - shipments: Array of matching shipments
    - possibleAdditionalShipmentsUrl: Alternative service URLs

    Shipment Object:
    - id: Tracking number
    - service: DHL division (express, parcel-de, etc.)
    - status: Current status with timestamp, location, statusCode
    - events: Array of tracking events
    - details: Additional shipment details
      * product: Service product information
      * references: Array of reference numbers
      * weight/dimensions: Physical attributes
    - origin/destination: Address information

    Event Object:
    - timestamp: ISO 8601 datetime
    - location: Address with country, postal code, locality
    - statusCode: One of: delivered, failure, pre-transit, transit, unknown
    - status: Short description/title
    - description: Detailed human-readable description
    - statusDetailed: Additional status details
    - remark: Status remarks
    - nextSteps: What happens next
    """
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
    """Infer shipment status from DHL shipment data and events.

    Status Priority (per UTAPI spec):
    1. Use shipment.status.statusCode if present (canonical)
    2. Fallback to latest event.statusCode
    3. Apply heuristics for out-for-delivery detection
    4. Use text-based inference as last resort

    Special handling:
    - UTAPI often returns 'transit' even when out for delivery
    - Check for phrases like "delivery vehicle" to detect out-for-delivery
    """
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
