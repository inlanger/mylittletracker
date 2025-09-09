import os
import pytest
import httpx
from dotenv import load_dotenv

from mylittletracker.providers import dhl
from mylittletracker.models import TrackingResponse, Shipment

# Load variables from .env if present
load_dotenv()


@pytest.mark.integration
def test_dhl_integration_normalized_response():
    api_key = os.getenv("DHL_API_KEY")

    # Skip if the user doesn't want to run this integration
    if not api_key:
        pytest.skip("DHL_API_KEY not set; skipping DHL integration test")

    server = os.getenv("DHL_SERVER")  # optional: "prod" (default) or "test"

    # Use a sample tracking number from DHL docs; authentication is the primary
    # concern here. The shipment may or may not exist, and that's fine.
    tracking_number = "7777777770"

    try:
        resp = dhl.track(tracking_number, server=server)
        # Validate unified model basics when we get a 2xx
        assert isinstance(resp, TrackingResponse)
        assert resp.provider == "dhl"
        assert isinstance(resp.shipments, list)
        if resp.shipments:
            s: Shipment = resp.shipments[0]
            assert isinstance(s.tracking_number, str)
            assert s.carrier == "dhl"
            assert isinstance(s.events, list)
            for ev in s.events:
                assert hasattr(ev, "timestamp") and ev.timestamp is not None
                assert isinstance(ev.status, str)
    except httpx.HTTPStatusError as exc:
        # If credentials are present but invalid, surface a clear failure.
        if exc.response.status_code in (401, 403):
            pytest.fail(
                f"DHL integration failed with status {exc.response.status_code}. "
                f"Authentication likely invalid. URL: {exc.request.url}"
            )
        # Non-auth errors (e.g., 404 for unknown tracking) are acceptable for this test.
        pytest.skip(
            f"DHL responded with {exc.response.status_code} for sample tracking; "
            f"auth appears configured. Skipping model assertions."
        )
