"""
Ecoscooting tracking provider using the Cainiao API.

Ecoscooting uses Cainiao's logistics network for package tracking.
This provider directly calls the Cainiao API to get real-time tracking data.
"""

import re
import json
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import quote

import httpx

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import get_with_retries, async_get_with_retries
from .base import ProviderBase

# Ecoscooting/Cainiao API Constants (discovered via reverse engineering)
CAINIAO_API_URL = "https://de-link.cainiao.com/gateway/link.do"
ECOSCOOTING_MSG_TYPE = "CN_OVERSEA_LOGISTICS_INQUIRY_TRACKING"
ECOSCOOTING_PROVIDER_ID = "DISTRIBUTOR_30250031"  # Fixed for Ecoscooting
ECOSCOOTING_TO_CODE = "CNL_EU"  # Europe routing only
DEFAULT_DATA_DIGEST = "suibianxie"  # Placeholder value (any string works)
DEFAULT_LOCALE = "en_US"  # Default locale for API calls


def _parse_ecoscooting_date(date_str: str) -> datetime:
    """Parse Ecoscooting date format: '2025-09-12 12:54:13 UTC+1'."""
    # Extract date, time, and timezone offset
    match = re.match(r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC([+-]\d+)", date_str)
    if not match:
        # Fallback to current time if parsing fails
        return datetime.now(timezone.utc)

    dt_str, tz_offset = match.groups()
    dt = datetime.strptime(dt_str, "%Y-%m-%d %H:%M:%S")

    # Apply timezone offset
    offset_hours = int(tz_offset)
    if offset_hours != 0:
        dt = dt.replace(tzinfo=timezone(timedelta(hours=offset_hours)))
    else:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt


def _map_ecoscooting_status(status_group: str) -> ShipmentStatus:
    """Map Ecoscooting/Cainiao status group to unified ShipmentStatus."""
    if not status_group:
        return ShipmentStatus.UNKNOWN

    status_lower = status_group.lower()

    if status_lower == "delivered":
        return ShipmentStatus.DELIVERED
    elif status_lower == "ready_for_collection":
        return ShipmentStatus.AVAILABLE_FOR_PICKUP
    elif status_lower == "delivering":
        return ShipmentStatus.OUT_FOR_DELIVERY
    elif status_lower == "in_transit":
        return ShipmentStatus.IN_TRANSIT
    else:
        return ShipmentStatus.UNKNOWN


def _call_cainiao_api(tracking_number: str) -> Dict[str, Any]:
    """Call the Cainiao API directly to get tracking data.

    API Requirements (discovered via reverse engineering):
    - All 5 parameters are REQUIRED for successful API calls
    - logistics_interface: JSON with mailNo, locale, role (locale/role are flexible)
    - msg_type: Must be exactly "CN_OVERSEA_LOGISTICS_INQUIRY_TRACKING"
    - data_digest: Any string value works (acts as placeholder/signature field)
    - logistic_provider_id: Must be exactly "DISTRIBUTOR_30250031" (Ecoscooting's ID)
    - to_code: Must be exactly "CNL_EU" (Europe routing, other codes unauthorized)

    Error progression if parameters missing:
    - Missing msg_type: "request param api can not be null"
    - Missing data_digest: "request param DataDigest can not be null"
    - Missing logistic_provider_id: "request param fromCode can not be null"
    - Wrong to_code: "toCode XX is not authorized"

    Args:
        tracking_number: The tracking number to look up

    Returns:
        Dict containing API response with success, packageParam, statuses, etc.

    Raises:
        RuntimeError: If API returns non-200 status or other errors
    """
    # Build required API parameters using constants
    data = {
        "logistics_interface": json.dumps(
            {
                "mailNo": tracking_number,
                "locale": DEFAULT_LOCALE,
                "role": "endUser",  # Role is flexible (endUser, admin, etc. all work)
            }
        ),
        "msg_type": ECOSCOOTING_MSG_TYPE,
        "logistic_provider_id": ECOSCOOTING_PROVIDER_ID,
        "data_digest": DEFAULT_DATA_DIGEST,
        "to_code": ECOSCOOTING_TO_CODE,
    }

    # Headers to mimic browser request (not strictly required but recommended)
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": "https://ecoscooting.com",
        "Referer": f"https://ecoscooting.com/tracking/{tracking_number}",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        response = get_with_retries(
            CAINIAO_API_URL, method="POST", data=data, headers=headers
        )

        if response.status_code == 200:
            return response.json()
        else:
            raise RuntimeError(f"API returned status {response.status_code}")
    except Exception as e:
        raise RuntimeError(f"Failed to call Cainiao API: {e}")


def track(tracking_number: str, *, language: str = "en") -> TrackingResponse:
    """Fetch tracking info for an Ecoscooting shipment.

    API URL Format: https://de-link.cainiao.com/gateway/link.do (POST)

    API Requirements (discovered via reverse engineering):
    - All 5 parameters are REQUIRED for successful API calls
    - No authentication required

    Parameter behavior:
    - logistics_interface: JSON with mailNo, locale, role (locale/role are flexible)
    - msg_type: Must be exactly "CN_OVERSEA_LOGISTICS_INQUIRY_TRACKING"
    - data_digest: Any string value works (acts as placeholder/signature field)
    - logistic_provider_id: Must be exactly "DISTRIBUTOR_30250031" (Ecoscooting's ID)
    - to_code: Must be exactly "CNL_EU" (Europe routing, other codes unauthorized)

    Headers:
    - Content-Type: application/x-www-form-urlencoded (required)
    - User-Agent: Optional, any value accepted
    - Accept: Optional, API always returns JSON
    - Origin/Referer: Optional, can mimic browser request

    Response format:
    - Success: JSON with success="true" and shipment data
    - Error without msg_type: XML response with errorCode S12
    - Error without data_digest: XML response with errorCode S12
    - Error with wrong to_code: JSON with success=false and errorCode S23
    - Other errors: Mixed XML/JSON responses

    Error handling:
    - Missing msg_type: "request param api can not be null"
    - Missing data_digest: "request param DataDigest can not be null"
    - Missing logistic_provider_id: "request param fromCode can not be null"
    - Wrong to_code: "toCode XX is not authorized"

    Server selection:
    - Not applicable - single endpoint only

    Args:
        tracking_number: The tracking number to look up
        language: Not used (API always returns based on mailNo)

    Returns:
        TrackingResponse with normalized tracking data
    """
    try:
        # Call the Cainiao API
        data = _call_cainiao_api(tracking_number)

        # Check if the API call was successful
        if data.get("success") != "true":
            raise RuntimeError("API returned success=false")

        # Parse events from API response
        events: List[TrackingEvent] = []
        for event_data in data.get("statuses", []):
            event = TrackingEvent(
                timestamp=_parse_ecoscooting_date(event_data["datetime"]),
                status=event_data["statusName"],
                location=None,
                details=event_data.get("description"),
                status_code=None,
                extras=None,
            )
            events.append(event)

        # Get package info
        package_info = data.get("packageParam", {})
        destination_city = package_info.get("toCity")
        destination_zip = package_info.get("toZipcode")

        # Get PUDO station info if available
        pop_station = data.get("popStationParam", {})
        station_name = pop_station.get("stationName")
        station_address = pop_station.get("detailAddress")

        # Build destination string
        destination_parts = []
        if destination_city:
            destination_parts.append(destination_city)
        if destination_zip:
            destination_parts.append(destination_zip)
        if station_name:
            destination_parts.append(f"PUDO: {station_name}")
        if station_address:
            destination_parts.append(station_address)

        destination = ", ".join(destination_parts) if destination_parts else None

        # Determine overall shipment status from the most recent event
        shipment_status = ShipmentStatus.UNKNOWN
        if events:
            # Get status from the most recent event's statusGroup
            latest_event = data.get("statuses", [])[0]
            shipment_status = _map_ecoscooting_status(
                latest_event.get("statusGroup", "")
            )

        # Create shipment
        shipment = Shipment(
            tracking_number=tracking_number,
            carrier="ecoscooting",
            status=shipment_status,
            events=events,
            service_type=None,
            origin=None,
            destination=destination,
            estimated_delivery=None,
            actual_delivery=None,
            extras=None,
        )

        # Check if actually delivered
        if events and shipment_status == ShipmentStatus.DELIVERED:
            shipment.actual_delivery = events[0].timestamp

        return TrackingResponse(shipments=[shipment], provider="ecoscooting")

    except Exception as e:
        # Return error response
        return TrackingResponse(
            shipments=[
                Shipment(
                    tracking_number=tracking_number,
                    carrier="ecoscooting",
                    status=ShipmentStatus.UNKNOWN,
                    events=[
                        TrackingEvent(
                            timestamp=datetime.now(timezone.utc),
                            status="Error fetching tracking data",
                            location=None,
                            details=str(e),
                            status_code=None,
                            extras=None,
                        )
                    ],
                    service_type=None,
                    origin=None,
                    destination=None,
                    estimated_delivery=None,
                    actual_delivery=None,
                    extras=None,
                )
            ],
            provider="ecoscooting",
        )


async def atrack(tracking_number: str) -> TrackingResponse:
    """
    Async version of track() for Ecoscooting shipments.

    Args:
        tracking_number: The tracking number to look up

    Returns:
        TrackingResponse with normalized tracking data
    """
    try:
        # For async version, we'll need to use sync API for now
        # TODO: Implement proper async version with httpx.AsyncClient
        import json

        url = "https://de-link.cainiao.com/gateway/link.do"

        # The exact parameters from the browser request
        data = {
            "logistics_interface": json.dumps(
                {"mailNo": tracking_number, "locale": "en_US", "role": "endUser"}
            ),
            "msg_type": "CN_OVERSEA_LOGISTICS_INQUIRY_TRACKING",
            "logistic_provider_id": "DISTRIBUTOR_30250031",
            "data_digest": "suibianxie",
            "to_code": "CNL_EU",
        }

        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": "https://ecoscooting.com",
            "Referer": f"https://ecoscooting.com/tracking/{tracking_number}",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            "Accept": "*/*",
            "Accept-Language": "en-US,en;q=0.9",
        }

        response = await async_get_with_retries(
            url, method="POST", data=data, headers=headers
        )

        if response.status_code != 200:
            raise RuntimeError(f"API returned status {response.status_code}")

        api_data = response.json()

        # Check if the API call was successful
        if api_data.get("success") != "true":
            raise RuntimeError("API returned success=false")

        # Parse events from API response
        events: List[TrackingEvent] = []
        for event_data in api_data.get("statuses", []):
            event = TrackingEvent(
                timestamp=_parse_ecoscooting_date(event_data["datetime"]),
                status=event_data["statusName"],
                location=None,
                details=event_data.get("description"),
                status_code=None,
                extras=None,
            )
            events.append(event)

        # Get package info
        package_info = api_data.get("packageParam", {})
        destination_city = package_info.get("toCity")
        destination_zip = package_info.get("toZipcode")

        # Get PUDO station info if available
        pop_station = api_data.get("popStationParam", {})
        station_name = pop_station.get("stationName")
        station_address = pop_station.get("detailAddress")

        # Build destination string
        destination_parts = []
        if destination_city:
            destination_parts.append(destination_city)
        if destination_zip:
            destination_parts.append(destination_zip)
        if station_name:
            destination_parts.append(f"PUDO: {station_name}")
        if station_address:
            destination_parts.append(station_address)

        destination = ", ".join(destination_parts) if destination_parts else None

        # Determine overall shipment status from the most recent event
        shipment_status = ShipmentStatus.UNKNOWN
        if events:
            # Get status from the most recent event's statusGroup
            latest_event = api_data.get("statuses", [])[0]
            shipment_status = _map_ecoscooting_status(
                latest_event.get("statusGroup", "")
            )

        # Create shipment
        shipment = Shipment(
            tracking_number=tracking_number,
            carrier="ecoscooting",
            status=shipment_status,
            events=events,
            service_type=None,
            origin=None,
            destination=destination,
            estimated_delivery=None,
            actual_delivery=None,
            extras=None,
        )

        # Check if actually delivered
        if events and shipment_status == ShipmentStatus.DELIVERED:
            shipment.actual_delivery = events[0].timestamp

        return TrackingResponse(shipments=[shipment], provider="ecoscooting")

    except Exception as e:
        # Return error response
        return TrackingResponse(
            shipments=[
                Shipment(
                    tracking_number=tracking_number,
                    carrier="ecoscooting",
                    status=ShipmentStatus.UNKNOWN,
                    events=[
                        TrackingEvent(
                            timestamp=datetime.now(timezone.utc),
                            status="Error fetching tracking data",
                            location=None,
                            details=str(e),
                            status_code=None,
                            extras=None,
                        )
                    ],
                    service_type=None,
                    origin=None,
                    destination=None,
                    estimated_delivery=None,
                    actual_delivery=None,
                    extras=None,
                )
            ],
            provider="ecoscooting",
        )


def build_tracking_url(tracking_number: str, *, language: str = "es") -> Optional[str]:
    """Return a human-facing Ecoscooting tracking URL for this shipment."""
    return f"https://ecoscooting.com/tracking/{quote(tracking_number)}"


class EcoscootingProvider(ProviderBase):
    """Thin wrapper around module-level Ecoscooting functions with URL builder."""

    provider = "ecoscooting"

    def build_tracking_url(
        self, tracking_number: str, *, language: Optional[str] = None, **kwargs: Any
    ) -> Optional[str]:
        return build_tracking_url(tracking_number, language=language or "es")

    def track(
        self, tracking_number: str, *, language: str = "en", **kwargs: Any
    ) -> TrackingResponse:
        return track(tracking_number, language=language)

    async def track_async(
        self,
        tracking_number: str,
        *,
        language: str = "en",
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> TrackingResponse:
        # Delegate to existing async function name (atrack)
        return await atrack(tracking_number)
