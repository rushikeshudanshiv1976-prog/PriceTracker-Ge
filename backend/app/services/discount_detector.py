
"""Evidence-based checks for retailer discount claims.

The detector uses only persisted price observations from an exact retailer
offer. It does not treat a provider's strike-through price as historical
evidence, and it does not mix history from different retailer offers.

Indirect shopping-search URLs cannot reliably identify a retailer listing.
Those results are reported as unassessable until a direct retailer URL can
be matched to a stored offer.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from math import isfinite
from statistics import median
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy.orm import Session

from app.models import OfferPriceHistory, ProductOffer


MIN_OBSERVATIONS = 5
MIN_HISTORY_SPAN_DAYS = 14
RECENT_PEAK_WINDOW_DAYS = 30
PRE_SALE_INCREASE_THRESHOLD = 0.12
CONSISTENT_PRICE_TOLERANCE = 0.07
CURRENT_PRICE_TOLERANCE = 0.05

TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "ref",
    "ref_",
    "tag",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}

PROVIDER_HOSTS = {
    "google.com",
    "www.google.com",
    "shopping.google.com",
    "serpapi.com",
    "www.serpapi.com",
}


def _escape_like(value: str) -> str:
    return (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )


def _as_price(value) -> float | None:
    """Return a finite, positive price or None."""
    if value is None or isinstance(value, bool):
        return None

    try:
        price = float(value)
    except (TypeError, ValueError):
        return None

    return price if isfinite(price) and price > 0 else None


def _discount_percent(original_price: float, current_price: float) -> float:
    return round((original_price - current_price) / original_price * 100, 1)


def _canonical_listing_url(url: str | None) -> str | None:
    """Remove known tracking parameters without changing product identity."""
    if not isinstance(url, str) or not url.strip():
        return None

    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return None

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None

    # Do not normalize credentials or unusual ports into a match.
    if parsed.username is not None or parsed.password is not None:
        return None

    query = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_QUERY_KEYS
        and not key.lower().startswith("utm_")
    ]

    netloc = parsed.netloc.lower()

    return urlunsplit(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path.rstrip("/") or "/",
            urlencode(query, doseq=True),
            "",
        )
    )


def _is_provider_or_indirect_url(url: str | None) -> bool:
    """Identify common search-provider URLs, not retailer product pages."""
    if not isinstance(url, str) or not url.strip():
        return True

    try:
        parsed = urlsplit(url.strip())
    except ValueError:
        return True

    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return True

    hostname = parsed.hostname.lower().rstrip(".")

    return any(
        hostname == provider or hostname.endswith("." + provider)
        for provider in PROVIDER_HOSTS
    )


def _history_points(rows) -> list[tuple[float, datetime]]:
    """Normalize observations and sort them chronologically."""
    points = []

    for row in rows:
        price = _as_price(getattr(row, "price", None))
        recorded_at = getattr(row, "recorded_at", None)

        if price is not None and isinstance(recorded_at, datetime):
            points.append((price, recorded_at))

    return sorted(points, key=lambda point: point[1])


def _matching_offer(
    db: Session,
    listing_url: str | None,
) -> ProductOffer | None:
    """Find an offer only when its URL identifies the same retailer listing."""
    if not listing_url or _is_provider_or_indirect_url(listing_url):
        return None

    offer = (
        db.query(ProductOffer)
        .filter(ProductOffer.url == listing_url)
        .first()
    )

    if offer is not None:
        return offer

    canonical_url = _canonical_listing_url(listing_url)

    if canonical_url is None:
        return None

    # Conservative fallback: only remove known tracking parameters.
    # No fuzzy title or product-name matching is performed.
    # Narrow candidates to the same host first instead of scanning the table.
    host = urlsplit(canonical_url).hostname or ""
    for candidate in (
        db.query(ProductOffer)
        .filter(ProductOffer.url.ilike(f"%{_escape_like(host)}%", escape="\\"))
        .all()
    ):
        if _canonical_listing_url(candidate.url) == canonical_url:
            return candidate

    return None


def _history_for_offer(
    db: Session,
    offer: ProductOffer | None,
) -> tuple[list[tuple[float, datetime]], str | None]:
    """Use verified history from this exact retailer offer only."""
    if offer is None:
        return [], None

    # Provider-reported offers are not verified retailer observations.
    if getattr(offer, "price_status", None) != "verified":
        return [], None

    rows = (
        db.query(OfferPriceHistory)
        .filter(OfferPriceHistory.offer_id == offer.id)
        .order_by(OfferPriceHistory.recorded_at.asc())
        .all()
    )

    points = _history_points(rows)

    if not points:
        return [], None

    return points, "offer_price_history"


def _history_evidence(
    points: list[tuple[float, datetime]],
    source: str | None,
    matched_url: str | None,
) -> dict:
    """Build transparent evidence from stored observations."""
    values = [price for price, _recorded_at in points]
    count = len(values)

    days_spanned = (
        max(0, (points[-1][1].date() - points[0][1].date()).days)
        if count > 1
        else 0
    )

    recent_points = []

    if points:
        cutoff = points[-1][1] - timedelta(days=RECENT_PEAK_WINDOW_DAYS)
        recent_points = [
            point for point in points if point[1] >= cutoff
        ]

    recent_peak = (
        max(recent_points, key=lambda point: point[0])
        if recent_points
        else None
    )

    return {
        "matched": bool(points),
        "matched_url": matched_url,
        "source": source,
        "observation_count": count,
        "days_spanned": days_spanned,
        "median_price": round(median(values), 2) if values else None,
        "lowest_price": round(min(values), 2) if values else None,
        "highest_price": round(max(values), 2) if values else None,
        "recent_peak_price": (
            round(recent_peak[0], 2) if recent_peak else None
        ),
        "recent_peak_at": (
            recent_peak[1].isoformat() if recent_peak else None
        ),
    }


def _has_enough_history(evidence: dict) -> bool:
    return (
        evidence["observation_count"] >= MIN_OBSERVATIONS
        and evidence["days_spanned"] >= MIN_HISTORY_SPAN_DAYS
    )


def _pre_sale_increase_evidence(
    points: list[tuple[float, datetime]],
    current_price: float,
    original_price: float,
) -> bool:
    """Detect a documented recent increase followed by a return to baseline."""
    if not points:
        return False

    cutoff = points[-1][1] - timedelta(days=RECENT_PEAK_WINDOW_DAYS)
    recent_points = [
        point for point in points if point[1] >= cutoff
    ]

    if len(recent_points) < 2:
        return False

    peak_price, peak_at = max(
        recent_points,
        key=lambda point: point[0],
    )

    baseline_points = [
        price
        for price, recorded_at in points
        if recorded_at < peak_at
    ]

    if len(baseline_points) < 3:
        baseline_points = [price for price, _ in points]

    baseline = median(baseline_points)

    claimed_matches_peak = (
        peak_price * (1 - CONSISTENT_PRICE_TOLERANCE)
        <= original_price
        <= peak_price * (1 + CONSISTENT_PRICE_TOLERANCE)
    )

    return (
        baseline > 0
        and peak_price >= baseline * (1 + PRE_SALE_INCREASE_THRESHOLD)
        and current_price <= baseline * (1 + CURRENT_PRICE_TOLERANCE)
        and peak_price >= current_price * (1 + PRE_SALE_INCREASE_THRESHOLD)
        and claimed_matches_peak
    )


def _result(
    *,
    status: str,
    severity: str,
    message: str,
    current_price: float | None,
    original_price: float | None,
    evidence: dict,
) -> dict:
    has_claim = (
        current_price is not None
        and original_price is not None
        and original_price > current_price
    )

    return {
        "status": status,
        "severity": severity,
        "message": message,
        "has_claim": has_claim,
        "claimed_original_price": original_price,
        "current_price": current_price,
        "claimed_discount_percent": (
            _discount_percent(original_price, current_price)
            if has_claim
            else None
        ),
        "history": evidence,
    }


def assess_discount_claim(
    *,
    current_price,
    original_price,
    history_points: list[tuple[float, datetime]],
    history_source: str | None = None,
    matched_url: str | None = None,
) -> dict:
    """Assess a claimed discount against recorded observations."""
    current = _as_price(current_price)
    original = _as_price(original_price)

    evidence = _history_evidence(
        history_points,
        history_source,
        matched_url,
    )

    if current is None or original is None or original <= current:
        return _result(
            status="unverified_claim",
            severity="neutral",
            message=(
                "No valid pre-discount price was reported, so there is no "
                "discount claim to verify."
            ),
            current_price=current,
            original_price=original,
            evidence=evidence,
        )

    if not _has_enough_history(evidence):
        return _result(
            status="insufficient_history",
            severity="neutral",
            message=(
                "This discount claim cannot be assessed yet. "
                f"{evidence['observation_count']} verified observation(s) "
                f"across {evidence['days_spanned']} day(s) are available. "
                f"At least {MIN_OBSERVATIONS} observations across "
                f"{MIN_HISTORY_SPAN_DAYS} days are required. "
                "Insufficient history does not mean the discount is fake."
            ),
            current_price=current,
            original_price=original,
            evidence=evidence,
        )

    if _pre_sale_increase_evidence(
        history_points,
        current,
        original,
    ):
        return _result(
            status="suspicious_pre_sale_increase",
            severity="warning",
            message=(
                "Recorded prices show a recent rise that matches the claimed "
                "pre-sale price, followed by a return near the earlier typical "
                "price. Treat this discount claim cautiously."
            ),
            current_price=current,
            original_price=original,
            evidence=evidence,
        )

    historical_high = evidence["highest_price"]
    historical_low = evidence["lowest_price"]

    if (
        historical_high is not None
        and historical_low is not None
        and original
        <= historical_high * (1 + CONSISTENT_PRICE_TOLERANCE)
        and current >= historical_low * (1 - CURRENT_PRICE_TOLERANCE)
    ):
        return _result(
            status="consistent_with_history",
            severity="positive",
            message=(
                "The claimed pre-discount price and current price are broadly "
                "within the recorded range for this exact retailer listing. "
                "This supports the claim but does not guarantee the retailer's "
                "final price."
            ),
            current_price=current,
            original_price=original,
            evidence=evidence,
        )

    return _result(
        status="unverified_claim",
        severity="caution",
        message=(
            "Recorded price history does not corroborate this claimed "
            "pre-discount price. It may still be valid, but the available "
            "history is not enough to verify it."
        ),
        current_price=current,
        original_price=original,
        evidence=evidence,
    )


def annotate_discount_evidence(db: Session, listing: dict) -> dict:
    """Attach discount evidence to one external-search listing in place."""
    listing_url = listing.get("url")
    offer = _matching_offer(db, listing_url)

    points, source = _history_for_offer(db, offer)

    if _is_provider_or_indirect_url(listing_url):
        warning = assess_discount_claim(
            current_price=listing.get("price"),
            original_price=listing.get("original_price"),
            history_points=[],
        )

        if warning["has_claim"]:
            warning["message"] = (
                "This result uses an indirect shopping-provider URL, so "
                "PricePulse cannot reliably match it to a specific retailer "
                "offer. The provider's reported discount has not been verified. "
                "Open the retailer's product page and track its direct URL to "
                "build usable price history."
            )

        listing["discount_warning"] = warning
        return listing

    warning = assess_discount_claim(
        current_price=listing.get("price"),
        original_price=listing.get("original_price"),
        history_points=points,
        history_source=source,
        matched_url=offer.url if offer is not None else None,
    )

    if offer is None and warning["has_claim"]:
        warning["message"] = (
            "No exact matching retailer offer was found in PricePulse, so "
            "there is no verified history to assess this provider-reported "
            "discount. Track the direct retailer URL to begin collecting "
            "observations. This does not mean the discount is fake."
        )

    listing["discount_warning"] = warning
    return listing
