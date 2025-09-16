"""Provider registry for mylittletracker.

Expose a simple mapping from provider name to its track() callable, so the CLI
and other clients can register providers in one place.
"""
from __future__ import annotations

from typing import Callable

from ..models import TrackingResponse

# Import provider modules to populate the registry
from . import correos as _correos
from . import dhl as _dhl
from . import dpd as _dpd
from . import gls as _gls
from . import ctt as _ctt

REGISTRY: dict[str, Callable[..., TrackingResponse]] = {
    "correos": _correos.track,
    "ctt": _ctt.track,
    "dhl": _dhl.track,
    "dpd": _dpd.track,
    "gls": _gls.track,
}


def get_provider_names() -> list[str]:
    return sorted(REGISTRY.keys())