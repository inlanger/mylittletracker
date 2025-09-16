import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import httpx

from ..models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from ..utils import parse_dt_iso, get_with_retries, async_get_with_retries

# Base URLs from the provided GLS OpenAPI spec
SERVERS = {
    "sandbox": "https://api-sandbox.gls-group.net/track-and-trace-v1/",
    "prod": "https://api.gls-group.net/track-and-trace-v1/",
    "qas": "https://api-qas.gls-group.net/track-and-trace-v1/",
}

TOKEN_URL = "https://api.gls-group.net/oauth2/v1/token"


def _server_base(server: Optional[str]) -> str:
    if not server:
        server = os.getenv("GLS_SERVER", "prod")
    key = server.lower()
    if key in ("production", "prod", "live"):
        return SERVERS["prod"]
    if key in ("sb", "sandbox", "test"):
        return SERVERS["sandbox"]
    if key in ("qas", "qa"):
        return SERVERS["qas"]
    # default
    return SERVERS["prod"]


def _get_oauth_token(client_id: str, client_secret: str) -> str:
    """Obtain OAuth2 client_credentials token for GLS APIs."""
    data = {"grant_type": "client_credentials"}
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    # Many OAuth servers accept client auth via Basic and/or form fields. Prefer Basic.
    auth = (client_id, client_secret)
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(TOKEN_URL, data=data, headers=headers, auth=auth)
        resp.raise_for_status()
        payload = resp.json()
        token = payload.get("access_token")
        if not token:
            raise RuntimeError("Failed to obtain GLS access_token")
        return token


def track(
    reference: str,
    *,
    language: str = "EN",
    server: Optional[str] = None,
    show_links: bool = False,
    show_events: bool = True,
) -> TrackingResponse:
    """Fetch tracking info for GLS by reference or unitno (parcel number).

    Requires GLS_CLIENT_ID/GLS_CLIENT_SECRET to be set in the environment.
    Uses /tracking/simple/references/{references} with up to 10 references.
    """
    client_id = os.getenv("GLS_CLIENT_ID")
    client_secret = os.getenv("GLS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GLS_CLIENT_ID/GLS_CLIENT_SECRET are not set. Add them to your environment or .env file."
        )

    token = _get_oauth_token(client_id, client_secret)
    base = _server_base(server)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        # GLS spec uses a two-letter language code like EN, DE, IT, etc.
        "Accept-Language": (language or "EN").upper(),
    }

    references = reference
    url = f"{base}tracking/simple/references/{references}"
    params = {
        "showLinks": str(show_links).lower(),
        "showEvents": str(show_events).lower(),
    }

    resp = get_with_retries(url, headers=headers, params=params, timeout=20.0)
    raw = resp.json()

    return normalize_gls_parcels_response(raw)


async def track_async(
    reference: str,
    *,
    language: str = "EN",
    server: Optional[str] = None,
    show_links: bool = False,
    show_events: bool = True,
    client: Optional[httpx.AsyncClient] = None,
) -> TrackingResponse:
    """Async version for GLS tracking by reference or unitno."""
    client_id = os.getenv("GLS_CLIENT_ID")
    client_secret = os.getenv("GLS_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise RuntimeError(
            "GLS_CLIENT_ID/GLS_CLIENT_SECRET are not set. Add them to your environment or .env file."
        )

    token = _get_oauth_token(client_id, client_secret)
    base = _server_base(server)

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "mylittletracker/0.1 (+https://example.com)",
        "Accept-Language": (language or "EN").upper(),
    }

    references = reference
    url = f"{base}tracking/simple/references/{references}"
    params = {
        "showLinks": str(show_links).lower(),
        "showEvents": str(show_events).lower(),
    }

    if client is None:
        # Reuse async helper without persistent client
        resp = await async_get_with_retries(url, headers=headers, params=params, timeout=20.0)
    else:
        resp = await async_get_with_retries(url, headers=headers, params=params, timeout=20.0, client=client)
    raw = resp.json()

    return normalize_gls_parcels_response(raw)


def normalize_gls_parcels_response(raw: Dict[str, Any]) -> TrackingResponse:
    """Normalize GLS ParcelsResponseDTO into TrackingResponse.

    Expected shape:
    {
      "parcels": [
        {
          "requested": "...",
          "unitno": "...",
          "status": "PREADVICE" | "INTRANSIT" | ...,
          "statusDateTime": "2024-10-11T15:24:57+0200",
          "events": [ { "code": "...", "description": "...", "eventDateTime": "...", ... } ],
          "errorCode": "E_404_01",
          "errorMessage": "Resource Not Found"
        },
        ...
      ]
    }
    """
    shipments: List[Shipment] = []
    parcels = (raw or {}).get("parcels", []) or []
    for p in parcels:
        unitno = p.get("unitno")
        error_code = p.get("errorCode")
        error_message = p.get("errorMessage")
        if not unitno:
            # Skip error-only entries (no parcel data)
            # Could also collect these into a special shipment with EXCEPTION, but keep consistent with others.
            continue

        # Map status
        status_enum = _map_gls_status((p.get("status") or "").upper())

        # Build events
        events: List[TrackingEvent] = []
        for ev in p.get("events", []) or []:
            ts = parse_dt_iso(ev.get("eventDateTime")) or datetime.now()
            desc = ev.get("description") or ev.get("code") or ""
            loc = _compose_location(ev.get("city"), ev.get("postalCode"), ev.get("country"))
            events.append(
                TrackingEvent(
                    timestamp=ts,
                    status=desc,
                    location=loc,
                    details=desc,
                    status_code=ev.get("code"),
                )
            )

        # Sort events for consistency
        events.sort(key=lambda e: e.timestamp)

        shipment = Shipment(
            tracking_number=unitno,
            carrier="gls",
            status=status_enum,
            events=events,
        )
        shipments.append(shipment)

    return TrackingResponse(shipments=shipments, provider="gls")


def _compose_location(city: Optional[str], postal: Optional[str], country: Optional[str]) -> Optional[str]:
    parts: List[str] = []
    city = (city or "").strip()
    postal = (postal or "").strip()
    country = (country or "").strip()

    left = " ".join(x for x in [city, postal] if x)
    right = country
    if left and right:
        return f"{left}, {right}"
    if left:
        return left
    if right:
        return right
    return None


def _parse_gls_datetime(s: Optional[str]) -> Optional[datetime]:
    # Deprecated in favor of utils.parse_dt_iso
    return parse_dt_iso(s)


def _map_gls_status(s: str) -> ShipmentStatus:
    mapping = {
        "PLANNEDPICKUP": ShipmentStatus.INFORMATION_RECEIVED,
        "INPICKUP": ShipmentStatus.INFORMATION_RECEIVED,
        "NOTPICKEDUP": ShipmentStatus.EXCEPTION,
        "PREADVICE": ShipmentStatus.INFORMATION_RECEIVED,
        "INTRANSIT": ShipmentStatus.IN_TRANSIT,
        "INDELIVERY": ShipmentStatus.OUT_FOR_DELIVERY,
        "DELIVEREDPS": ShipmentStatus.DELIVERED,
        "DELIVERED": ShipmentStatus.DELIVERED,
        "INWAREHOUSE": ShipmentStatus.IN_TRANSIT,
        "NOTDELIVERED": ShipmentStatus.EXCEPTION,
        "CANCELED": ShipmentStatus.CANCELLED,
        "FINAL": ShipmentStatus.UNKNOWN,
    }
    return mapping.get(s, ShipmentStatus.UNKNOWN)
