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

    if isinstance(exc, httpx.HTTPStatusError):
        status_code = str(exc.response.status_code)
        status_text = f"HTTP {exc.response.status_code} while fetching"
        try:
            details = f"URL: {exc.request.url}"
        except Exception:
            pass
    elif isinstance(exc, httpx.HTTPError):
        status_text = "HTTP error during tracking"

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

