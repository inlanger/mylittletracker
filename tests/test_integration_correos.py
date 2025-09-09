import os
import json
import pytest
import httpx

from mylittletracker.providers import correos
from mylittletracker.models import TrackingResponse, Shipment


@pytest.mark.integration
def test_correos_integration_normalized_response():
    # Correos does not require an API key. Use a sample tracking number; the
    # API should respond with a JSON payload even if the number is not found.
    code = os.getenv("CORREOS_TRACKING_CODE", "TEST123456")
    language = os.getenv("CORREOS_LANGUAGE", "EN")

    try:
        resp = correos.track(code, language=language)
    except httpx.HTTPError as exc:
        pytest.skip(f"Correos HTTP error: {exc}")
    except json.JSONDecodeError as exc:
        pytest.skip(f"Correos returned non-JSON or empty response: {exc}")

    # Validate unified model basics
    assert isinstance(resp, TrackingResponse)
    assert resp.provider == "correos"
    assert isinstance(resp.shipments, list)

    # If shipments are present, validate structure
    if resp.shipments:
        s: Shipment = resp.shipments[0]
        assert isinstance(s.tracking_number, str)
        assert s.carrier == "correos"
        assert isinstance(s.events, list)
        for ev in s.events:
            assert hasattr(ev, "timestamp") and ev.timestamp is not None
            assert isinstance(ev.status, str)
