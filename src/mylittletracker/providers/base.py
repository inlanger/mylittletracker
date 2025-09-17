from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

import httpx

from ..models import TrackingResponse
from ..utils import get_with_retries, async_get_with_retries


class MissingCredentialsError(RuntimeError):
    """Raised when required provider credentials are not configured."""


class ProviderHTTPError(RuntimeError):
    """Raised for unexpected HTTP status/content from a provider endpoint."""


class ProviderParseError(RuntimeError):
    """Raised when provider response cannot be parsed or normalized."""


class ProviderBase(ABC):
    """
    Minimal base class for shipment tracking providers.

    Subclasses should implement track/track_async and can use helpers to
    standardize headers, retries, credentials, and language/locale handling.
    """

    # Machine-readable provider key (e.g., "dhl", "dpd"). Override in subclass.
    provider: str = "unknown"

    # Shared defaults
    timeout: float = 20.0
    user_agent: str = "mylittletracker/0.1 (+https://example.com)"
    default_language: str = "en"

    # Optional public tracking website base. Subclasses may override and
    # implement build_tracking_url accordingly.
    website_base: Optional[str] = None

    # --- Core contract ---
    @abstractmethod
    def track(
        self,
        tracking_number: str,
        *,
        language: str = "en",
        **kwargs: Any,
    ) -> TrackingResponse:
        """Fetch and normalize tracking details (synchronous)."""
        raise NotImplementedError

    @abstractmethod
    async def track_async(
        self,
        tracking_number: str,
        *,
        language: str = "en",
        client: Optional[httpx.AsyncClient] = None,
        **kwargs: Any,
    ) -> TrackingResponse:
        """Fetch and normalize tracking details (asynchronous)."""
        raise NotImplementedError

    # --- Human-facing tracking link ---
    def build_tracking_url(
        self,
        tracking_number: str,
        *,
        language: Optional[str] = None,
        **kwargs: Any,
    ) -> Optional[str]:
        """
        Return a human-facing tracking URL for this provider, or None if not supported.

        Subclasses should override where a stable/shareable URL exists. The kwargs
        may include provider-specific fields (e.g., recipient_postal_code) if needed.
        """
        return None

    # --- Helpers ---
    def build_headers(
        self,
        *,
        accept: str = "application/json",
        extra: Optional[Dict[str, str]] = None,
    ) -> Dict[str, str]:
        """Construct default headers with optional extra fields."""
        headers = {"User-Agent": self.user_agent, "Accept": accept}
        if extra:
            headers.update(extra)
        return headers

    def get(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
    ) -> httpx.Response:
        """HTTP GET with retries/timeouts via shared utility."""
        return get_with_retries(
            url, params=params, headers=headers, timeout=self.timeout
        )

    async def aget(
        self,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        headers: Optional[Dict[str, str]] = None,
        client: Optional[httpx.AsyncClient] = None,
    ) -> httpx.Response:
        """Async HTTP GET with retries/timeouts via shared utility."""
        return await async_get_with_retries(
            url, params=params, headers=headers, timeout=self.timeout, client=client
        )

    def ensure_credential(self, env_var: str) -> str:
        """Fetch a required credential from environment or raise a helpful error."""
        val = os.getenv(env_var)
        if not val:
            raise MissingCredentialsError(
                f"{env_var} is not set. Add it to your environment or .env file."
            )
        return val

    # --- Language/locale helpers ---
    def normalize_language(self, language: Optional[str]) -> str:
        """Normalize to a lowercase BCP-47-ish language code (best effort)."""
        return (
            language or self.default_language
        ).strip().lower() or self.default_language

    def lang2(self, language: Optional[str]) -> str:
        """Two-letter language code (best effort)."""
        return self.normalize_language(language)[:2]

    def language_to_locale(self, language: Optional[str]) -> str:
        """Map language to a provider-style locale identifier. Override per provider."""
        mapping = {
            "en": "en_US",
            "es": "es_ES",
            "de": "de_DE",
            "fr": "fr_FR",
            "it": "it_IT",
            "nl": "nl_NL",
        }
        return mapping.get(self.lang2(language), "en_US")
