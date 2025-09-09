import pytest
import httpx

from mylittletracker.models import TrackingResponse, Shipment
from mylittletracker.providers import dpd


@pytest.mark.integration
@pytest.mark.asyncio
async def test_dpd_async_plc_normalized_response():
    # Public PLC endpoint should work without auth; use a known example format
    parcel_number = "05162815323093"

    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await dpd.track_async(parcel_number, language="en_US", client=client)

    assert isinstance(resp, TrackingResponse)
    assert resp.provider == "dpd"
    assert isinstance(resp.shipments, list)

    if resp.shipments:
        s: Shipment = resp.shipments[0]
        assert isinstance(s.tracking_number, str)
        assert s.carrier == "dpd"
        assert isinstance(s.events, list)
        for ev in s.events:
            assert hasattr(ev, "timestamp") and ev.timestamp is not None
            assert isinstance(ev.status, str)
