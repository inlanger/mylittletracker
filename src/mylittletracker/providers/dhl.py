import os
import httpx
from datetime import datetime
from typing import Any, Dict, Optional

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus

TEST_BASE = "https://api-test.dhl.com/track/shipments"
PROD_BASE = "https://api-eu.dhl.com/track/shipments"


def _base_url(server: str) -> str:
    return TEST_BASE if server.lower() == "test" else PROD_BASE


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

    server = server or os.getenv("DHL_SERVER", "prod")
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
    if limit is not None:
        params["limit"] = limit

    headers = {
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept": "application/json",
        "DHL-API-Key": api_key,
    }

    with httpx.Client(timeout=20.0) as client:
        response = client.get(_base_url(server), params=params, headers=headers)
        response.raise_for_status()
        raw_data = response.json()
        
    return normalize_dhl_response(raw_data, tracking_number)


def normalize_dhl_response(raw_data: Dict[str, Any], tracking_number: str) -> TrackingResponse:
    """Normalize DHL API response to universal TrackingResponse model."""
    shipments = []
    
    # Extract shipment data from DHL format
    shipment_list = raw_data.get("shipments", [])
    if not shipment_list:
        # No shipments found
        return TrackingResponse(
            shipments=shipments,
            provider="dhl"
        )
    
    dhl_shipment = shipment_list[0]  # Take first shipment
    events = []
    
    # Convert events
    for event in dhl_shipment.get("events", []):
        # Parse timestamp
        timestamp = _parse_dhl_timestamp(event.get("timestamp", ""))
        
        # Get status description
        status = (event.get("status") or 
                 event.get("description") or 
                 event.get("statusDetailed") or "")
        
        tracking_event = TrackingEvent(
            timestamp=timestamp,
            status=status,
            location=event.get("location", {}).get("address", {}).get("addressLocality"),
            details=event.get("description"),
            status_code=event.get("statusCode")
        )
        events.append(tracking_event)
    
    # Determine overall shipment status
    status = _infer_dhl_status(dhl_shipment, events)
    
    # Extract additional shipment details
    details = dhl_shipment.get("details", {})
    service_type = details.get("product", {}).get("productName")
    
    # Extract origin and destination
    origin = None
    destination = None
    if "origin" in details:
        origin_addr = details["origin"].get("address", {})
        origin = f"{origin_addr.get('addressLocality', '')}, {origin_addr.get('countryCode', '')}".strip(", ")
    
    if "destination" in details:
        dest_addr = details["destination"].get("address", {})
        destination = f"{dest_addr.get('addressLocality', '')}, {dest_addr.get('countryCode', '')}".strip(", ")
    
    shipment = Shipment(
        tracking_number=dhl_shipment.get("id", tracking_number),
        carrier="dhl",
        status=status,
        events=events,
        service_type=service_type,
        origin=origin,
        destination=destination
    )
    
    shipments.append(shipment)
    
    return TrackingResponse(
        shipments=shipments,
        provider="dhl"
    )


def _parse_dhl_timestamp(timestamp_str: str) -> datetime:
    """Parse DHL timestamp string into datetime object."""
    try:
        # DHL uses ISO format like "2023-12-01T10:30:00"
        if timestamp_str:
            # Handle timezone info if present
            if "+" in timestamp_str or timestamp_str.endswith("Z"):
                # Remove timezone for simple parsing
                timestamp_str = timestamp_str.split("+")[0].rstrip("Z")
            return datetime.fromisoformat(timestamp_str)
        else:
            return datetime.now()
    except ValueError:
        # Fallback if parsing fails
        return datetime.now()


def _infer_dhl_status(dhl_shipment: Dict[str, Any], events: list[TrackingEvent]) -> ShipmentStatus:
    """Infer shipment status from DHL shipment data and events."""
    # Check shipment status first
    shipment_status = dhl_shipment.get("status", {}).get("status", "").lower()
    
    if "delivered" in shipment_status:
        return ShipmentStatus.DELIVERED
    elif "transit" in shipment_status:
        return ShipmentStatus.IN_TRANSIT
    elif "exception" in shipment_status:
        return ShipmentStatus.EXCEPTION
    
    # Check latest event if shipment status is not clear
    if events:
        latest_status = events[-1].status.lower()
        
        if "delivered" in latest_status:
            return ShipmentStatus.DELIVERED
        elif "out for delivery" in latest_status or "delivery" in latest_status:
            return ShipmentStatus.OUT_FOR_DELIVERY
        elif "transit" in latest_status or "departed" in latest_status:
            return ShipmentStatus.IN_TRANSIT
        elif "received" in latest_status or "processed" in latest_status:
            return ShipmentStatus.INFORMATION_RECEIVED
    
    return ShipmentStatus.UNKNOWN

