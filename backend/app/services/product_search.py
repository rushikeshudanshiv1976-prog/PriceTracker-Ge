import json
import logging
import os
import re
import threading
import time
from datetime import date
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

SERPAPI_URL = "https://serpapi.com/search.json"
MAX_RESULTS = 20
REQUEST_TIMEOUT = 20

STOP_WORDS = {
    "a", "an", "and", "for", "in", "of", "on",
    "the", "with", "new", "online", "official",
}

INDIAN_RETAILER_ALIASES = (
    "amazon.in",
    "amazon india",
    "flipkart",
    "croma",
    "reliance digital",
    "vijay sales",
    "tata cliq",
    "tatacliq",
    "jiomart",
    "shopsy",
    "poorvika",
    "sangeetha mobiles",
    "samsung india",
    "apple india",
)

GOOGLE_HOSTS = {
    "google.com",
    "google.co.in",
    "shopping.google.com",
    "serpapi.com",
}


class ProductSearchError(Exception):
    """Raised when external product search fails."""


def normalize_text(value: str | None) -> str:
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def normalize_retailer_name(retailer: str | None) -> str:
    return normalize_text(retailer)


def _canonical_retailer(retailer: str | None) -> str:
    """Normalize common marketplace aliases for duplicate detection."""
    name = normalize_retailer_name(retailer)

    if "flipkart" in name or "shopsy" in name:
        return "flipkart"

    if "tata cliq" in name or "tatacliq" in name:
        return "tata cliq"

    if "amazon" in name:
        return "amazon"

    if "jiomart" in name:
        return "jiomart"

    if "croma" in name:
        return "croma"

    if "reliance digital" in name:
        return "reliance digital"

    if "vijay sales" in name:
        return "vijay sales"

    return name


def is_indian_retailer(retailer: str | None) -> bool:
    name = normalize_retailer_name(retailer)

    if not name:
        return False

    return any(
        normalize_retailer_name(alias) in name
        for alias in INDIAN_RETAILER_ALIASES
    )


def _is_google_or_provider_url(url: str) -> bool:
    """Identify known provider URLs that are not retailer product pages."""
    try:
        hostname = (urlparse(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return True

    return any(
        hostname == domain or hostname.endswith("." + domain)
        for domain in GOOGLE_HOSTS
    )


def _valid_http_url(value) -> str | None:
    """Accept safe HTTP(S) URLs without embedded credentials."""
    if not isinstance(value, str) or not value.strip():
        return None

    value = value.strip()

    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        return None

    if (
        parsed.scheme not in {"http", "https"}
        or not hostname
        or parsed.username
        or parsed.password
    ):
        return None

    return value




# --- Quota protection -------------------------------------------------------
# Every real SerpApi call spends one search from a small quota. So:
#  * identical queries are answered from a cache that is SAVED TO DISK, so it
#    survives server restarts and uvicorn --reload;
#  * the number of real calls per day is counted (also saved to disk) and
#    capped.
#   SERPAPI_CACHE_MINUTES  default 720 (12h); 0 disables the cache
#   SERPAPI_DAILY_LIMIT    default 20 real calls per day; 0 = no cap
#   SERPAPI_CACHE_FILE     default serpapi_cache.json (in the folder you run
#                          uvicorn from; do not commit it)
_cache_lock = threading.Lock()
_state: dict | None = None  # {"usage": {"day": str, "count": int}, "entries": {}}
MAX_CACHE_ENTRIES = 200


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.getenv(name, str(default))))
    except ValueError:
        return default


def _cache_path() -> str:
    return os.getenv("SERPAPI_CACHE_FILE", "serpapi_cache.json")


def _load_state() -> dict:
    """Load saved cache/usage once per process (caller holds the lock)."""
    global _state
    if _state is None:
        _state = {"usage": {"day": "", "count": 0}, "entries": {}}
        try:
            with open(_cache_path(), "r", encoding="utf-8") as handle:
                saved = json.load(handle)
            if isinstance(saved, dict):
                usage = saved.get("usage")
                entries = saved.get("entries")
                if isinstance(usage, dict):
                    _state["usage"] = {
                        "day": str(usage.get("day", "")),
                        "count": int(usage.get("count", 0)),
                    }
                if isinstance(entries, dict):
                    _state["entries"] = entries
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError):
            logger.warning("Ignoring unreadable SerpApi cache file")
    return _state


def _save_state(ttl: int) -> None:
    """Write cache/usage to disk (caller holds the lock). Never raises."""
    state = _state
    if state is None:
        return
    try:
        now = time.time()
        entries = {
            key: value
            for key, value in state["entries"].items()
            if ttl and now - value[0] < ttl
        }
        # Keep only the newest entries.
        newest = sorted(entries.items(), key=lambda kv: kv[1][0], reverse=True)
        state["entries"] = dict(newest[:MAX_CACHE_ENTRIES])

        path = _cache_path()
        tmp_path = f"{path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(state, handle)
        os.replace(tmp_path, path)
    except (OSError, TypeError, ValueError):
        logger.warning("Could not save the SerpApi cache file", exc_info=True)


def _cached_request_results(query: str, api_key: str) -> list[dict]:
    ttl = _env_int("SERPAPI_CACHE_MINUTES", 720) * 60
    limit = _env_int("SERPAPI_DAILY_LIMIT", 20)
    key = normalize_text(query)

    with _cache_lock:
        state = _load_state()

        if ttl:
            hit = state["entries"].get(key)
            if hit and time.time() - hit[0] < ttl:
                logger.info("SerpApi cache hit for %r (no search spent)", query)
                return hit[1]

        today = date.today().isoformat()
        usage = state["usage"]
        if usage["day"] != today:
            usage["day"] = today
            usage["count"] = 0

        if limit and usage["count"] >= limit:
            raise ProductSearchError(
                f"Daily SerpApi limit of {limit} searches reached "
                "(set SERPAPI_DAILY_LIMIT to change it). Showing no new "
                "prices until tomorrow."
            )

        # Count before the call: a failed request can still spend quota.
        usage["count"] += 1
        _save_state(ttl)
        logger.info(
            "SerpApi REAL call #%s today (limit: %s)",
            usage["count"],
            limit or "none",
        )

    items = _request_results(query, api_key)

    with _cache_lock:
        state = _load_state()
        if ttl:
            state["entries"][key] = [time.time(), items]
        _save_state(ttl)

    return items


def _request_results(query: str, api_key: str) -> list[dict]:
    try:
        response = requests.get(
            SERPAPI_URL,
            params={
                "engine": "google_shopping",
                "q": query,
                "api_key": api_key,
                "gl": "in",
                "hl": "en",
                "location": "India",
                "num": MAX_RESULTS,
            },
            timeout=REQUEST_TIMEOUT,
        )

        response.raise_for_status()
        data = response.json()

    except requests.HTTPError as exc:
        status = (
            exc.response.status_code
            if exc.response is not None
            else "unknown"
        )
        raise ProductSearchError(
            f"SerpApi returned HTTP {status} for query: {query}"
        ) from exc

    except requests.Timeout as exc:
        raise ProductSearchError(
            f"SerpApi timed out for query: {query}"
        ) from exc

    except requests.RequestException as exc:
        raise ProductSearchError(
            f"Could not connect to SerpApi: {exc.__class__.__name__}"
        ) from exc

    except ValueError as exc:
        raise ProductSearchError(
            "SerpApi returned invalid JSON."
        ) from exc

    if not isinstance(data, dict):
        raise ProductSearchError(
            "SerpApi returned an unexpected response."
        )

    if data.get("error"):
        raise ProductSearchError(str(data["error"]))

    results = data.get("shopping_results", [])
    return results if isinstance(results, list) else []


def _valid_numeric_price(value) -> float | None:
    """Convert a positive numeric price to float."""
    if isinstance(value, bool) or value is None:
        return None

    try:
        price = float(str(value).replace(",", "").strip())

        if price > 0:
            return price

    except (ValueError, TypeError, OverflowError):
        pass

    return None


def _validated_original_price(
    original_price: float | None,
    current_price: float | None,
) -> float | None:
    """
    Retain an original price only when it is greater than the current price.
    This validates the arithmetic, not whether the original price is genuine.
    """
    if original_price is None or current_price is None:
        return None

    if original_price <= current_price:
        return None

    return original_price


def _discount_percent(
    original_price: float | None,
    current_price: float | None,
) -> float | None:
    """Calculate the provider-claimed discount percentage."""
    if (
        original_price is not None
        and current_price is not None
        and original_price > current_price
    ):
        return round(
            (original_price - current_price) / original_price * 100,
            1,
        )

    return None


def _discount_warning(
    original_price: float | None,
    current_price: float | None,
) -> dict:
    """
    Describe the provider's discount claim without pretending to verify it.

    Historical price records are needed to assess whether the original price
    was inflated. A large discount percentage alone does not prove fraud.
    """
    if original_price is None or current_price is None:
        return {
            "status": "unverified_claim",
            "message": (
                "No valid comparable original price was reported, "
                "so the discount claim cannot be evaluated."
            ),
        }

    if original_price <= current_price:
        return {
            "status": "unverified_claim",
            "message": (
                "No valid markdown was reported. "
                "The original price has not been historically verified."
            ),
        }

    discount = (
        (original_price - current_price) / original_price * 100
    )

    return {
        "status": "claimed_discount_unverified",
        "message": (
            f"A discount of {discount:.1f}% is claimed by the provider. "
            "Historical price evidence is required to determine whether "
            "the original price was inflated."
        ),
    }




def _convert_result(item: dict) -> dict | None:
    """Convert a Google Shopping result into the standard product format."""
    title = (item.get("title") or "").strip()
    retailer = (item.get("source") or "").strip()

    if not title or not retailer or retailer == "Unknown retailer":
        return None

    # Prefer a direct retailer URL, but retain a valid provider URL
    # as a fallback so the search can still display the product.
    direct_url = None
    fallback_url = None

    for candidate in (item.get("product_link"), item.get("link")):
        url = _valid_http_url(candidate)

        if not url:
            continue

        if fallback_url is None:
            fallback_url = url

        if not _is_google_or_provider_url(url):
            direct_url = url
            break

    url = direct_url or fallback_url

    if not url:
        return None

    price = _valid_numeric_price(item.get("extracted_price"))
    original_price = _valid_numeric_price(
        item.get("extracted_old_price")
    )
    original_price = _validated_original_price(original_price, price)

    image_url = _valid_http_url(item.get("thumbnail"))
    provider_product_id = str(item.get("product_id") or "").strip()

    return {
        "title": title,
        "retailer": retailer,
        "price": price,
        "original_price": original_price,
        "currency": "INR" if price is not None else None,
        "url": url,
        "link_type": (
            "retailer_direct" if direct_url else "shopping_result"
        ),
        "image_url": image_url,
        "price_status": (
            "provider_reported" if price is not None else "unavailable"
        ),
        "discount_percent": _discount_percent(original_price, price),
        "discount_warning": _discount_warning(original_price, price),
        "provider_product_id": provider_product_id or None,
        "source": "Google Shopping via SerpApi",
    }




def _query_matches_title(query: str, title: str) -> bool:
    """Filter weak matches while allowing reasonable title variations."""
    query_tokens = {
        token
        for token in normalize_text(query).split()
        if token not in STOP_WORDS
    }

    title_tokens = set(normalize_text(title).split())

    if not query_tokens:
        return True

    # Require important identifiers such as RTX 4050 or model numbers.
    critical_tokens = {
        token
        for token in query_tokens
        if any(char.isdigit() for char in token)
    }

    if not critical_tokens.issubset(title_tokens):
        return False

    ordinary_tokens = query_tokens - critical_tokens

    if not ordinary_tokens:
        return True

    matched = len(ordinary_tokens & title_tokens)

    return matched / len(ordinary_tokens) >= 0.5


def _listing_identity(result: dict) -> tuple:
    """
    Identify duplicate marketplace listings using retailer, title, price,
    and image. Provider IDs alone are unreliable for duplicate detection.
    """
    retailer = _canonical_retailer(result.get("retailer"))
    title = normalize_text(result.get("title"))
    price = result.get("price")

    image_url = result.get("image_url") or ""

    try:
        parsed_image = urlparse(image_url)
        image_identity = (
            (parsed_image.hostname or "").lower(),
            parsed_image.path.rstrip("/").lower(),
        )
    except ValueError:
        image_identity = ("", normalize_text(image_url))

    normalized_price = round(price, 2) if price is not None else None

    return (
        retailer,
        title,
        normalized_price,
        image_identity,
    )


def search_external_products(
    query: str,
    limit: int = MAX_RESULTS,
    strict_match: bool = True,
) -> list[dict]:
    """
    Search Google Shopping.

    strict_match=True (user searches): drop results whose title does not
    contain the query's model-number tokens.
    strict_match=False (price refresh): keep everything Google returns and let
    the caller match against the tracked product itself. Refresh queries are
    full product titles, which Google's shorter titles rarely contain in full.
    """
    api_key = os.getenv("SERPAPI_KEY")

    if not api_key:
        raise ProductSearchError(
            "SERPAPI_KEY is missing. Check backend/.env."
        )

    query = (query or "").strip()

    if len(query) < 2:
        return []

    try:
        limit = max(1, min(int(limit), MAX_RESULTS))
    except (ValueError, TypeError, OverflowError):
        limit = MAX_RESULTS

    items = _cached_request_results(query, api_key)
    collected = {}
    converted_count = 0
    rejected_titles: list[str] = []

    for item in items:
        if not isinstance(item, dict):
            continue

        result = _convert_result(item)

        if not result:
            continue

        converted_count += 1

        if strict_match and not _query_matches_title(query, result["title"]):
            if len(rejected_titles) < 3:
                rejected_titles.append(result["title"][:80])
            continue

        if (
            not result["retailer"]
            or result["retailer"] == "Unknown retailer"
        ):
            continue

        identity = _listing_identity(result)
        previous = collected.get(identity)

        if previous is None:
            collected[identity] = result
            continue

        # Prefer a usable price, then a direct retailer URL, then an image.
        previous_score = (
            previous["price"] is not None,
            previous["link_type"] == "retailer_direct",
            bool(previous["image_url"]),
        )

        result_score = (
            result["price"] is not None,
            result["link_type"] == "retailer_direct",
            bool(result["image_url"]),
        )

        if result_score > previous_score:
            collected[identity] = result

    results = list(collected.values())

    # Shows at which stage results disappear: raw -> converted -> kept.
    logger.info(
        "SerpApi %r: raw=%s converted=%s kept=%s strict=%s%s",
        query,
        len(items),
        converted_count,
        len(results),
        strict_match,
        f" rejected_examples={rejected_titles}" if rejected_titles else "",
    )

    results.sort(
        key=lambda item: (
            not is_indian_retailer(item["retailer"]),
            item["price"] is None,
            (
                item["price"]
                if item["price"] is not None
                else float("inf")
            ),
            normalize_text(item["title"]),
        )
    )

    return results[:limit]