import argparse
import json
from typing import Callable, Optional
from datetime import datetime

import httpx
from dotenv import load_dotenv
from .models import TrackingResponse, Shipment, TrackingEvent, ShipmentStatus
from .providers import REGISTRY as PROVIDER_REGISTRY, get_provider_names
from .utils import normalize_language

# Load environment variables from .env if present
load_dotenv()

# Map carrier name to its tracking function
PROVIDERS: dict[str, Callable[..., TrackingResponse]] = PROVIDER_REGISTRY


def cmd_track(args: argparse.Namespace) -> int:
    tracker = PROVIDERS[args.carrier]
    # Normalize language globally per provider
    lang_norm, lang_from = normalize_language(args.language, args.carrier)
    error_occurred = False
    if args.strict:
        # In strict mode, propagate errors
        tracking_response = tracker(args.code, language=lang_norm)
    else:
        try:
            tracking_response = tracker(args.code, language=lang_norm)
        except Exception as exc:  # Fallback to normalized UNKNOWN response on errors
            tracking_response = _fallback_response(args.carrier, args.code, exc)
            error_occurred = True
    # If language normalization happened and not JSON output, emit a small note when verbose
    if args.verbose and (not args.json) and lang_from and (lang_from != lang_norm):
        print(
            f"Note: normalized language '{lang_from}' -> '{lang_norm}' for {args.carrier}"
        )
    if args.json:
        # Convert Pydantic model to JSON (compat across Pydantic v1/v2)
        try:
            print(tracking_response.model_dump_json(indent=2))  # Pydantic v2
        except AttributeError:
            print(tracking_response.json(indent=2))  # Pydantic v1
    else:
        print_human(tracking_response)
    return 1 if (not args.strict and error_occurred) else 0


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
            ctype = (
                resp.headers.get("Content-Type")
                or resp.headers.get("content-type")
                or ""
            ).lower()
            if "json" in ctype:
                body = resp.json()
                # Try common fields
                provider_error_code = (
                    str(
                        (
                            body.get("code")
                            or body.get("error_code")
                            or body.get("error")
                            or ""
                        )
                    ).strip()
                    or None
                )
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
                location=None,
                details=details,
                status_code=status_code,
                extras=ev_extras or None,
            )
        ],
        service_type=None,
        origin=None,
        destination=None,
        estimated_delivery=None,
        actual_delivery=None,
        extras=None,
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
        # Status line; if unknown, show latest raw status/code for clarity
        status_line = f"Status: {shipment.status.value}"
        if shipment.status.name == "UNKNOWN" and shipment.events:
            latest = shipment.events[-1]
            extra_bits = []
            if latest.status:
                extra_bits.append(latest.status)
            if latest.status_code:
                extra_bits.append(f"code={latest.status_code}")
            if extra_bits:
                status_line += f" (latest: {'; '.join(extra_bits)})"
        print(status_line)

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
        prog="mylittletracker", description="Personal parcels tracking CLI."
    )
    subparsers = parser.add_subparsers(dest="command")

    p_track = subparsers.add_parser("track", help="Track a parcel")
    p_track.add_argument("carrier", choices=get_provider_names(), help="Carrier name")
    p_track.add_argument("code", help="Parcel/shipment code")
    p_track.add_argument(
        "--language",
        "-l",
        default=None,
        help=(
            "Language (two-letter code like en, es, de, fr, it, nl). "
            "If omitted, defaults to $MLT_DEFAULT_LANGUAGE or system locale (fallback en). "
            "Other forms (e.g., en-US) are accepted and normalized per provider."
        ),
    )
    p_track.add_argument("--json", action="store_true", help="Output raw JSON payload")
    p_track.add_argument(
        "--strict",
        action="store_true",
        help="Propagate errors (non-zero exit) instead of returning fallback",
    )
    p_track.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Print normalization notes and extra info",
    )
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
