import argparse
import json
from typing import Callable, Optional

from dotenv import load_dotenv
from .providers import correos, dhl
from .models import TrackingResponse

# Load environment variables from .env if present
load_dotenv()

# Map carrier name to its tracking function
PROVIDERS: dict[str, Callable[..., TrackingResponse]] = {
    "correos": correos.track,
    "dhl": dhl.track,
    "dpd": __import__("mylittletracker.providers.dpd", fromlist=["track"]).track,
}


def cmd_track(args: argparse.Namespace) -> int:
    tracker = PROVIDERS[args.carrier]
    tracking_response = tracker(args.code, language=args.language)
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

