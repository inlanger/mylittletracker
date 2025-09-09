import os
import pytest
import httpx

from mylittletracker.models import TrackingResponse, Shipment
from mylittletracker.providers import correos


@pytest.mark.integration
@pytest.mark.asyncio
async def test_correos_async_normalized_response():
    code = os.getenv("CORREOS_TRACKING_CODE", "TEST123456")
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await correos.track_async(code, language="EN", client=client)
        except httpx.HTTPError as exc:
            pytest.skip(f"Correos HTTP error: {exc}")
        except ValueError as exc:
            # httpx.Response.json() may raise ValueError/JSONDecodeError
            pytest.skip(f"Correos returned non-JSON or empty response: {exc}")

    assert isinstance(resp, TrackingResponse)
    assert resp.provider == "correos"
    assert isinstance(resp.shipments, list)

    if resp.shipments:
        s: Shipment = resp.shipments[0]
        assert isinstance(s.tracking_number, str)
        assert s.carrier == "correos"
        assert isinstance(s.events, list)
        for ev in s.events:
            assert hasattr(ev, "timestamp") and ev.timestamp is not None
            assert isinstance(ev.status, str)
