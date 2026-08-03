"""Open Food Facts — the product food database (ROADMAP P12).

Free, open, no API key, no account. That matters more than it sounds: this is
the source intended to replace the MyFitnessPal override (ADR-0010), which is
explicitly personal-only and must never ship as a product feature. Open Food
Facts is the sanctioned path.

**Vendor shapes stop here** (§2.5). Everything below the adapter boundary sees
:class:`coach.normalize.foods.FoodItem`, so adding USDA FoodData Central later
is a new module beside this one and nothing else changes.

Read-only. This adapter never writes to Open Food Facts, and it identifies
itself honestly in the User-Agent because that is what their terms ask of API
consumers.
"""

from __future__ import annotations

from typing import Any

import httpx

SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"
PRODUCT_URL = "https://world.openfoodfacts.org/api/v2/product/{barcode}.json"

# They ask API users to identify themselves. A generic python-httpx UA is how a
# project gets rate-limited for everyone.
USER_AGENT = (
    "fitness-ai-coach/0.1 (personal health tracker; https://github.com/abhayKashyap03/fitness-ai)"
)

# Only the fields we normalize. Requesting everything pulls hundreds of
# attributes per product for no benefit and makes the response slow to parse.
_FIELDS = ",".join(
    [
        "code",
        "product_name",
        "brands",
        "quantity",
        "serving_size",
        "serving_quantity",
        "nutriments",
    ]
)


class FoodSearchError(RuntimeError):
    """Search or lookup failed. Never raised for 'no results' — that is empty."""


class OpenFoodFactsClient:
    """Thin HTTP client. Returns raw payloads; parsing lives in normalize/foods."""

    def __init__(self, *, client: httpx.Client | None = None, timeout: float = 15.0):
        self._client = client or httpx.Client(timeout=timeout, headers={"User-Agent": USER_AGENT})

    def search(self, query: str, *, page_size: int = 10) -> list[dict[str, Any]]:
        """Search products by name. Returns raw product dicts, newest API shape.

        An empty result is an empty list, not an error — "no such food" is a
        legitimate answer and the caller must be able to say so plainly (§2.7).
        """
        if not query.strip():
            raise ValueError("search needs a non-empty query")
        try:
            r = self._client.get(
                SEARCH_URL,
                params={
                    "search_terms": query,
                    "search_simple": 1,
                    "action": "process",
                    "json": 1,
                    "page_size": page_size,
                    "fields": _FIELDS,
                },
            )
            r.raise_for_status()
            body = r.json()
        except httpx.HTTPError as exc:
            raise FoodSearchError(f"Open Food Facts search failed: {exc}") from exc
        return list(body.get("products") or [])

    def by_barcode(self, barcode: str) -> dict[str, Any] | None:
        """One product by barcode, or None if they have never seen it.

        None rather than an exception: an unknown barcode is a fact about the
        database, not a failure of the request.
        """
        code = barcode.strip()
        if not code.isdigit():
            raise ValueError(f"a barcode is digits only, got {barcode!r}")
        try:
            r = self._client.get(PRODUCT_URL.format(barcode=code), params={"fields": _FIELDS})
            if r.status_code == 404:
                return None
            r.raise_for_status()
            body = r.json()
        except httpx.HTTPError as exc:
            raise FoodSearchError(f"Open Food Facts lookup failed: {exc}") from exc
        if body.get("status") != 1:
            return None
        product = body.get("product")
        return dict(product) if isinstance(product, dict) else None

    def close(self) -> None:
        self._client.close()
