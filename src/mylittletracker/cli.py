import argparse
import json
from typing import Callable, Optional
from datetime import datetime

import httpx
from dotenv import load_dotenv
from .providers import correos, dhl, gls, ctt
from .models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus

# Load environment variables from .env if present
load_dotenv()

# Map carrier name to its tracking function
PROVIDERS: dict[str, Callable[..., TrackingResponse]] = {
    "correos": correos.track,
    "ctt": ctt.track,
    "dhl": dhl.track,
    "dpd": __import__("mylittletracker.providers.dpd", fromlist=["track"]).track,
    "gls": gls.track,
}


def cmd_track(args: argparse.Namespace) -> int:
    tracker = PROVIDERS[args.carrier]
    try:
        tracking_response = tracker(args.code, language=args.language)
    except Exception as exc:  # Fallback to normalized UNKNOWN response on errors
        tracking_response = _fallback_response(args.carrier, args.code, exc)
    if args.json:
        # Convert Pydantic model to JSON (compat across Pydantic v1/v2)
        try:
            print(tracking_response.model_dump_json(indent=2))  # Pydantic v2
        except AttributeError:
            print(tracking_response.json(indent=2))  # Pydantic v1
    else:
        print_human(tracking_response)
    return 0


def cmd_providers(_args: argparse.Namespace) -> int:
    for name in sorted(PROVIDERS):
        print(name)
    return 0


def _fallback_response(carrier: str, code: str, exc: Exception) -> TrackingResponse:
    # Build a minimal normalized response with an explanatory event
    status_code: Optional[str] = None
    status_text = "Error during tracking"
    details = f"{exc.__class__.__name__}: {exc}"
    ev_extras: dict[str, object] = {}

    if isinstance(exc, httpx.HTTPStatusError):
        resp = exc.response
        status_code = str(resp.status_code)
        status_text = f"HTTP {resp.status_code} while fetching"

        url_text: Optional[str] = None
        try:
            url_text = str(exc.request.url)
        except Exception:
            url_text = None

        # Attempt to extract provider error code/description from response body
        provider_error_code: Optional[str] = None
        provider_error_desc: Optional[str] = None
        body_snippet: Optional[str] = None
        try:
            ctype = (resp.headers.get("Content-Type") or resp.headers.get("content-type") or "").lower()
            if "json" in ctype:
                body = resp.json()
                # Try common fields
                provider_error_code = str(
                    (body.get("code")
                     or body.get("error_code")
                     or body.get("error")
                     or "")
                ).strip() or None
                provider_error_desc = (
                    body.get("message")
                    or body.get("error_description")
                    or body.get("description")
                    or body.get("detail")
                    or None
                )
                if not provider_error_desc:
                    try:
                        body_snippet = json.dumps(body)[:500]
                    except Exception:
                        body_snippet = str(body)[:500]
            else:
                txt = resp.text or ""
                body_snippet = txt[:500] if txt else None
        except Exception:
            pass

        parts: list[str] = []
        if url_text:
            parts.append(f"URL: {url_text}")
        if provider_error_code:
            parts.append(f"Provider error code: {provider_error_code}")
        if provider_error_desc:
            parts.append(f"Provider error: {provider_error_desc}")
        if body_snippet and not provider_error_desc:
            parts.append(f"Body: {body_snippet}")
        if parts:
            details = " | ".join(parts)

        ev_extras = {
            "url": url_text,
            "provider_error_code": provider_error_code,
            "provider_error_description": provider_error_desc,
        }
        if body_snippet:
            ev_extras["body_snippet"] = body_snippet
    elif isinstance(exc, httpx.HTTPError):
        status_text = "HTTP error during tracking"
        try:
            ev_extras["url"] = str(exc.request.url)
        except Exception:
            pass

    shipment = Shipment(
        tracking_number=code,
        carrier=carrier,
        status=ShipmentStatus.UNKNOWN,
        events=[
            TrackingEvent(
                timestamp=datetime.now(),
                status=status_text,
                details=details,
                status_code=status_code,
                extras=ev_extras or None,
            )
        ],
    )
    return TrackingResponse(shipments=[shipment], provider=carrier)


def print_human(tracking_response: TrackingResponse) -> None:
    print(f"Provider: {tracking_response.provider}")
    
    if not tracking_response.has_shipments:
        print("No shipments found")
        return
    
    for shipment in tracking_response.shipments:
        print(f"\nShipment: {shipment.tracking_number}")
        print(f"Carrier: {shipment.carrier}")
        print(f"Status: {shipment.status.value}")
        
        # Show additional info if available
        if shipment.service_type:
            print(f"Service: {shipment.service_type}")
        if shipment.origin:
            print(f"Origin: {shipment.origin}")
        if shipment.destination:
            print(f"Destination: {shipment.destination}")
        
        # Show events
        if not shipment.events:
            print("No tracking events")
        else:
            print("\nTracking Events:")
            for event in shipment.events:
                timestamp = event.timestamp.strftime("%Y-%m-%d %H:%M")
                print(f"- {timestamp}: {event.status}")
                if event.location:
                    print(f"  Location: {event.location}")
                if event.details:
                    print(f"  Details: {event.details}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mylittletracker",
        description="Personal parcels tracking CLI."
    )
    subparsers = parser.add_subparsers(dest="command")

    p_track = subparsers.add_parser("track", help="Track a parcel")
    p_track.add_argument("carrier", choices=sorted(PROVIDERS.keys()), help="Carrier name")
    p_track.add_argument("code", help="Parcel/shipment code")
    p_track.add_argument("--language", "-l", default="EN", help="Language (provider-specific)")
    p_track.add_argument("--json", action="store_true", help="Output raw JSON payload")
    p_track.set_defaults(func=cmd_track)

    p_prov = subparsers.add_parser("providers", help="List supported carriers")
    p_prov.set_defaults(func=cmd_providers)

    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        parser.print_help()
        return 1
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

