# mylittletracker

A small, modern CLI to track parcels from multiple carriers (Correos, DHL, CTT Express, DPD, GLS). It uses httpx for HTTP calls and a unified Pydantic v2 model to normalize responses across providers.

## Features
- src/ layout, PEP 621 metadata in `pyproject.toml`
- Console script: `mylittletracker`
- Providers
  - Correos (public API)
  - DHL (Unified Shipment Tracking API)
  - DPD (public PLC JSON endpoint)
  - CTT Express (public JSON endpoint)
- Unified Pydantic v2 model (TrackingResponse → Shipment → TrackingEvent)
- httpx for robust HTTP requests
- Integration tests with pytest markers (skip when creds aren’t provided)

## Installation

Use a virtualenv and install in editable mode.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip3 install -U pip
pip3 install -e .
```

## Usage

List supported providers:

```bash
mylittletracker providers
```

Track a Correos shipment (no key required):

```bash
mylittletracker track correos PK43BG0440928440146007C --language EN
```

Track a DHL shipment (requires API key):

Track a DPD shipment (public PLC JSON endpoint):

```bash
# Defaults to an English locale (en_US)
mylittletracker track dpd 05162815323093 --json

# You can also control locale via --language
mylittletracker track dpd 05162815323093 --language en_US --json
mylittletracker track dpd 05162815323093 --language NL --json   # maps to nl_NL
```

Notes:
- No auth required for DPD PLC endpoint.
- The language argument accepts either a two-letter code (mapped to a default locale: EN→en_US, NL→nl_NL, DE→de_DE, …) or a full locale (e.g., en_US) and affects localised labels from DPD.

```bash
# Make sure DHL_API_KEY is configured (see Environment below)
mylittletracker track dhl 7777777770 --language en
```

Track a CTT Express shipment (no key required):

```bash
mylittletracker track ctt 0082800082909720118884 --json
```

Add `--json` to print normalized JSON from the unified model:

```bash
mylittletracker track correos PK43BG0440928440146007C --json
```

### Language handling

The CLI normalizes `--language` globally per provider so common inputs work consistently:

- DPD (PLC JSON): expects a locale like `en_US`, `nl_NL`, `de_DE`, `fr_FR`, `it_IT`, `es_ES`.
  - Inputs like `en`, `EN-us`, `en_us` are normalized to `en_US`.
  - Unknown values fall back to `en_US`.
  - The normalized locale is included in the JSON under `shipments[].extras.dpd_locale`.
- GLS: uses two-letter, upper-case (e.g., `EN`, `ES`) in `Accept-Language`.
  - Inputs like `en-us` normalize to `EN`.
- DHL (UTAPI): uses two-letter, lower-case (e.g., `en`, `es`) for the `language` parameter.
  - Inputs like `EN` normalize to `en`.
- Correos/CTT: two-letter, upper-case (e.g., `EN`, `ES`).

Notes:
- When not using `--json`, the CLI prints a small note if your language input was normalized (e.g., `Note: normalized language 'EN-us' -> 'en_US' for dpd`).
- Providers may also perform their own internal normalization as needed.

Examples:

```bash
# DPD: accepts EN-us, normalizes to en_US
mylittletracker track dpd 05162815323093 --language EN-us --json

# GLS: accepts en-us, normalizes to EN
mylittletracker track gls 92592437886 --language en-us --json

# DHL: accepts EN, normalizes to en
mylittletracker track dhl CH515858672DE --language EN --json

# Correos: accepts en-us, normalizes to EN
mylittletracker track correos PK43BG0440928440146007C --language en-us --json
```

## Library usage (async)

Use the async provider functions in your own code (bots/services). Reuse a single AsyncClient for multiple calls to reduce latency.

```python
import asyncio
import httpx
from mylittletracker.providers import correos, dhl, dpd

async def main():
    async with httpx.AsyncClient(timeout=20.0) as client:
        # Correos (no key)
        c = await correos.track_async("PK43BG0440928440146007C", language="EN", client=client)
        # DPD (no key)
        d = await dpd.track_async("05162815323093", language="en_US", client=client)
        # DHL (requires DHL_API_KEY in env)
        h = await dhl.track_async("CH515858672DE", language="en", client=client)

        print(c.model_dump_json(indent=2))
        print(d.model_dump_json(indent=2))
        print(h.model_dump_json(indent=2))

asyncio.run(main())
```

Notes:
- Each provider also exposes a synchronous `track()` wrapper used by the CLI, but libraries/services should prefer the async API.
- Timestamps are parsed into `datetime` objects; use `.model_dump_json()` or `.model_dump()` to serialize.

## Environment

Only DHL requires credentials. Put them in a `.env` file (already gitignored):

```
DHL_API_KEY=PasteHere_ConsumerKey
# Optional (defaults to prod):
# DHL_SERVER=prod    # or "test" for https://api-test.dhl.com
```

Notes:
- Do not commit secrets. `.env` is ignored by git.
- Correos does not require a key.
- DPD does not require a key.
- CTT Express does not require a key.

## Unified model (Pydantic v2)

The CLI normalizes each provider response into a common shape:

- TrackingResponse
  - provider: str (e.g. "correos", "dhl")
  - query_timestamp: datetime
  - shipments: list[Shipment]
- Shipment
  - tracking_number: str
  - carrier: str
  - status: enum (information_received, in_transit, out_for_delivery, available_for_pickup, delivered, exception, returned, cancelled, unknown)
  - events: list[TrackingEvent]
  - optional: service_type, origin, destination, estimated_delivery, actual_delivery
- TrackingEvent
  - timestamp: datetime (parsed from provider formats; serialized as ISO 8601 in --json)
  - status: str
  - optional: location, details, status_code

## Testing

We use pytest for both unit and integration tests. Integration tests call live carrier APIs and are designed to skip automatically if credentials aren’t configured.

Install pytest:

```bash
.venv/bin/pip3 install pytest
```

Run all tests:

```bash
.venv/bin/python3 -m pytest -q
```

Only integration tests:

```bash
.venv/bin/python3 -m pytest -m integration -q
```

Skip integration tests:

```bash
.venv/bin/python3 -m pytest -m "not integration" -q
```

DHL integration test requires DHL_API_KEY. If missing, the test is skipped with a clear message. Non-auth errors (e.g., 404 for unknown tracking) are treated as a skip; 401/403 fails the test to signal bad credentials.

## Adding a new provider

1. Create a new module under `src/mylittletracker/providers/`.
2. Implement a `track(code: str, ...) -> TrackingResponse` function.
3. Fetch the provider JSON with httpx and write a normalizer to build the unified model.
4. Register the provider in `src/mylittletracker/providers/__init__.py` (REGISTRY mapping).
5. Add integration tests under `tests/` and mark with `@pytest.mark.integration`.

## License

MIT
