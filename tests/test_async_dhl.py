import os
import pytest
import httpx

from mylittletracker.models import TrackingResponse, Shipment
from mylittletracker.providers import dhl


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dhl_async_normalized_response():
    api_key = os.getenv("DHL_API_KEY")
    if not api_key:
        pytest.skip("DHL_API_KEY not set; skipping DHL async integration test")

    tracking_number = os.getenv("DHL_TRACKING_NUMBER", "CH515858672DE")
    server = os.getenv("DHL_SERVER")

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await dhl.track_async(tracking_number, server=server, client=client)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in (401, 403):
                pytest.fail(
                    f"DHL async integration failed with status {exc.response.status_code}. "
                    f"Check DHL_API_KEY and tracking number. URL: {exc.request.url}"
                )
            pytest.skip(
                f"DHL responded with {exc.response.status_code}; skipping strict assertions"
            )

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
