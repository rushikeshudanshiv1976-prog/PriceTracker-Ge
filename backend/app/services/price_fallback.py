"""
Google Shopping price fallback for "track by URL".

When a retailer page can be identified but its price cannot be read (the
store blocked the request or hides the price), look the product up on Google
Shopping through SerpApi and use the provider-reported price. The price is
labelled "provider_reported"; nothing is invented. It reuses the cached,
quota-capped search layer, so it costs at most one SerpApi search per new URL.
"""

import logging
import os
import re

from app.services.product_search import (
    ProductSearchError,
    search_external_products,
)

logger = logging.getLogger(__name__)


def fallback_enabled() -> bool:
    return os.getenv("INGEST_PRICE_FALLBACK", "true").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def _tokens(text):
    return re.findall(r"[a-z0-9]+", (text or "").lower())


def _head(title: str) -> str:
    """The short product name before the first separator of a long title."""
    head = re.split(r"\s[|\-–—,(]\s?|[|(]", title or "", maxsplit=1)[0]
    words = head.split()
    return " ".join(words[:8]).strip()


def _key_tokens(title: str):
    """Words and model identifiers a Google result must contain."""
    head_tokens = _tokens(_head(title))
    # Storage / memory sizes (e.g. 16gb, 512gb, 1tb) separate variants.
    sizes = {
        t
        for t in _tokens(title)
        if re.fullmatch(r"\d+(?:gb|tb)", t)
    }
    # "16GB+512GB" style pairs tokenise into the same two tokens.
    return set(head_tokens) | sizes


def lookup_provider_price(title: str):
    """
    Return {"price", "title", "url", "retailer"} or None.

    A result is accepted only if it contains every word of the product's
    short name and every storage size from the full title.
    """
    if not fallback_enabled():
        return None

    query = _head(title) or (title or "").strip()
    if len(query) < 3:
        return None

    try:
        results = search_external_products(
            query, limit=20, strict_match=False
        )
    except ProductSearchError as exc:
        logger.info("Price fallback skipped for %r: %s", query, exc)
        return None
    except Exception:
        logger.exception("Price fallback failed for %r", query)
        return None

    needed = _key_tokens(title)
    if not needed:
        return None

    best = None
    for item in results or []:
        if not isinstance(item, dict):
            continue
        try:
            price = float(item.get("price"))
        except (TypeError, ValueError):
            continue
        if price <= 0 or price != price or price == float("inf"):
            continue
        if not needed.issubset(set(_tokens(item.get("title")))):
            continue
        if best is None or price < best["price"]:
            best = {
                "price": price,
                "title": item.get("title"),
                "url": item.get("url") or item.get("link"),
                "retailer": item.get("retailer") or item.get("source"),
            }

    return best