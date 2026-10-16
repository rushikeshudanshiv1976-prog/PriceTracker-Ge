"""
Background price refresh for PriceTracker-Ge.

Price source:
    Google Shopping through SerpApi.

Important rules:
    1. Never invent or guess prices.
    2. Ignore missing, invalid, or non-positive prices.
    3. Match Google Shopping results against the tracked product title.
    4. Select the lowest price among matching results with valid prices.
    5. Save product and offer price history when the price changes or
       when the daily history interval has elapsed.
    6. Never replace an existing ProductOffer.url with a Google Shopping
       result URL. ProductOffer.url is unique in the database.
    7. Send target-price alerts only when a valid matching price is found.
    8. Handle errors per product so one failed product does not stop
       the refresh of all remaining products.
"""

import logging
import math
import os
import re
import threading
from datetime import datetime, timedelta

from app.database import SessionLocal
from app.models import (
    Product,
    ProductOffer,
    OfferPriceHistory,
    PriceHistory,
    Subscription,
)
from app.services.email_alerts import send_price_alert
from app.services.price_fallback import lookup_provider_price
from app.services.product_search import (
    ProductSearchError,
    search_external_products,
)

logger = logging.getLogger(__name__)

# Keep the graph useful without recording an unchanged price every 12 hours.
HISTORY_INTERVAL = timedelta(hours=24)

# Prevent overlapping refreshes in this running backend process.
_refresh_lock = threading.Lock()


def _valid_price(value):
    """Return True only for a valid, positive, finite numeric price."""
    if isinstance(value, bool) or value is None:
        return False

    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return False

    return math.isfinite(number) and number > 0


def _jump_guard_percent():
    try:
        return max(0, int(os.getenv("PRICE_JUMP_GUARD_PERCENT", "50")))
    except ValueError:
        return 50


def _implausible_jump(old_price, new_price):
    """True when new_price differs from old_price by more than the guard."""
    guard = _jump_guard_percent()
    if not guard or not _valid_price(old_price) or not _valid_price(new_price):
        return False
    change = abs(float(new_price) - float(old_price)) / float(old_price)
    return change * 100 > guard


def _normalize(text):
    """Normalize a title into lowercase alphanumeric tokens."""
    if not text:
        return []

    return re.findall(r"[a-z0-9]+", str(text).lower())


def _matching_google_results(product_title, results):
    """
    Keep plausible results for the same product.

    Model identifiers containing digits are treated carefully: if the tracked
    title contains such an identifier, the candidate must contain it too.
    This reduces the risk of recording a price for a different model.
    """
    tracked_tokens = _normalize(product_title)

    if not tracked_tokens:
        return []

    tracked_set = set(tracked_tokens)

    # Model numbers and identifiers are particularly useful for electronics,
    # watches, shoes, and other products with multiple similar variants.
    # Ordinals such as "12th" (12th Gen) are descriptive, not model numbers,
    # and are often omitted from Google's shorter titles.
    tracked_identifiers = {
        token
        for token in tracked_set
        if any(char.isdigit() for char in token)
        and not re.fullmatch(r"\d+(?:st|nd|rd|th)", token)
    }

    matches = []

    for result in results:
        if not isinstance(result, dict):
            continue

        candidate_title = result.get("title")
        candidate_tokens = _normalize(candidate_title)

        if not candidate_tokens:
            continue

        candidate_set = set(candidate_tokens)

        # Do not match a different model when the tracked product has a model
        # number or other digit-containing identifier.
        if tracked_identifiers and not tracked_identifiers.issubset(
            candidate_set
        ):
            continue

        overlap = tracked_set.intersection(candidate_set)

        # Similarity is measured against the tracked title, not against
        # unrelated extra words in the candidate title.
        similarity = len(overlap) / max(len(tracked_set), 1)

        # Require a reasonable title overlap before considering a price.
        if similarity < 0.50:
            continue

        # The reverse check: a candidate full of words the tracked title does
        # not have is usually a different variant or model (for example
        # "Carina 2.0" vs "Carina Slim Perf"). Google's titles are normally
        # shorter than stored ones, so a true match has almost no extra words.
        candidate_coverage = len(overlap) / max(len(candidate_set), 1)
        if candidate_coverage < 0.80:
            continue

        matches.append(result)

    return matches


def _get_lowest_google_price(product):
    """
    Search Google Shopping for the tracked product.

    Returns the cheapest valid price among plausible matching results.
    Returns None when no reliable matching result is available.
    """
    query = (product.title or "").strip()

    if not query:
        logger.warning(
            "Cannot refresh product_id=%s because its title is empty",
            product.id,
        )
        return None

    # This function uses the existing SerpApi integration. It does not scrape
    # Amazon, Flipkart, or other retailer pages directly.
    # The query is the full tracked title; skip the search layer's strict
    # query-vs-title filter and match with _matching_google_results below.
    results = search_external_products(query, limit=20, strict_match=False)

    if not isinstance(results, list) or not results:
        logger.info(
            "Google Shopping returned no results for product_id=%s title=%r",
            product.id,
            query,
        )
        return None

    matching_results = _matching_google_results(query, results)

    # Only candidates with an actual positive numeric price can be selected.
    priced_results = [
        result
        for result in matching_results
        if _valid_price(result.get("price"))
    ]

    if not priced_results:
        candidate_titles = [
            str(result.get("title", ""))[:100]
            for result in results[:5]
            if isinstance(result, dict)
        ]

        logger.info(
            "No matching Google Shopping price for product_id=%s "
            "title=%r candidates=%s",
            product.id,
            query,
            candidate_titles,
        )
        return None

    # Select the lowest price among the matching, valid provider results.
    lowest_result = min(
        priced_results,
        key=lambda result: float(result["price"]),
    )

    selected = dict(lowest_result)
    selected["price"] = float(selected["price"])

    # Preserve the provider's source URL in the returned search result, but
    # do not automatically assign that URL to ProductOffer.url in the DB.
    selected["price_status"] = "provider_reported"

    return selected


def _latest_product_history(db, product_id):
    """Return the most recent product-level history row, if any."""
    return (
        db.query(PriceHistory)
        .filter(PriceHistory.product_id == product_id)
        .order_by(PriceHistory.recorded_at.desc())
        .first()
    )


def _get_offer_for_product(db, product):
    """
    Find an offer belonging to this product.

    ProductOffer.url is unique, so we do not identify an offer by assigning
    it a new Google Shopping URL. We first look up the offer by product_id.
    """
    return (
        db.query(ProductOffer)
        .filter(ProductOffer.product_id == product.id)
        .first()
    )


def _create_offer_if_url_is_available(db, product, result, now, price):
    """
    Create an offer only when the product's existing URL is not already
    assigned to another offer.

    We deliberately do not use result['url'] here. Google Shopping URLs can
    be shared between search results and can violate the unique URL constraint.
    """
    product_url = product.url

    if not product_url:
        logger.warning(
            "Cannot create ProductOffer for product_id=%s: product URL is empty",
            product.id,
        )
        return None

    url_in_use = (
        db.query(ProductOffer)
        .filter(ProductOffer.url == product_url)
        .first()
    )

    if url_in_use is not None:
        logger.warning(
            "Cannot create a separate ProductOffer for product_id=%s: "
            "its URL is already used by offer_id=%s. "
            "Product-level history will still be saved.",
            product.id,
            url_in_use.id,
        )
        return None

    offer = ProductOffer(
        product_id=product.id,
        retailer=(
            result.get("retailer")
            or product.retailer
            or "Unknown retailer"
        ),
        url=product_url,
        current_price=price,
        currency=result.get("currency") or "INR",
        price_status="provider_reported",
        last_checked=now,
    )

    db.add(offer)
    db.flush()

    return offer


def refresh_tracked_prices(user_email=None, product_id=None):
    """
    Refresh tracked products using Google Shopping via SerpApi.

    Args:
        user_email:
            Optional email address. If provided, only products subscribed to
            by that user are refreshed. If omitted, all tracked products are
            considered.

    Returns:
        A summary dictionary with checked, history_points, skipped,
        and alerts_sent counts.
    """
    if not _refresh_lock.acquire(blocking=False):
        logger.info("Price refresh skipped because another refresh is running")
        return {
            "busy": True,
            "checked": 0,
            "history_points": 0,
            "skipped": 0,
            "alerts_sent": 0,
        }

    db = SessionLocal()

    checked = 0
    history_points = 0
    skipped = 0
    alerts_sent = 0

    try:
        # Only products that at least one user is tracking are refreshed.
        # Untracked leftovers must never spend SerpApi searches.
        query = db.query(Product).filter(
            Product.url.isnot(None),
            Product.id.in_(db.query(Subscription.product_id).distinct()),
        )

        if user_email:
            subscribed_product_ids = (
                db.query(Subscription.product_id)
                .filter(Subscription.user_email == user_email)
                .distinct()
                .subquery()
            )

            query = query.filter(
                Product.id.in_(
                    db.query(subscribed_product_ids.c.product_id)
                )
            )

        if product_id is not None:
            query = query.filter(Product.id == product_id)

        products = query.all()

        logger.info(
            "Starting Google Shopping price refresh for %s product(s)",
            len(products),
        )

        for product in products:
            try:
                # Search using the tracked title, not its Google Shopping URL.
                result = _get_lowest_google_price(product)

                # Products added by URL with no price yet (long retailer
                # titles often fail the strict match): try the short-name
                # lookup. Only used while the product has no price at all.
                if result is None and not _valid_price(
                    product.current_price
                ):
                    fb = lookup_provider_price(product.title)
                    if fb:
                        result = {
                            "title": fb["title"],
                            "price": fb["price"],
                            "url": fb["url"],
                            "retailer": fb["retailer"],
                            "price_status": "provider_reported",
                        }

                if result is None:
                    skipped += 1
                    continue

                new_price = float(result["price"])

                if not _valid_price(new_price):
                    skipped += 1
                    logger.warning(
                        "Invalid selected price for product_id=%s",
                        product.id,
                    )
                    continue

                # Sanity guard: a huge jump versus the last known price usually
                # means a different product/variant was matched, not a real
                # price change. Skip it rather than store (and alert on) it.
                if _implausible_jump(product.current_price, new_price):
                    skipped += 1
                    logger.warning(
                        "Skipping product_id=%s: price jumped from %s to %s "
                        "(more than %s%%); likely a different variant. "
                        "Set PRICE_JUMP_GUARD_PERCENT=0 to disable.",
                        product.id,
                        product.current_price,
                        new_price,
                        _jump_guard_percent(),
                    )
                    continue

                now = datetime.utcnow()

                # Save the old price before changing it. This is used to
                # determine whether the product has crossed its target price.
                old_price = (
                    float(product.current_price)
                    if _valid_price(product.current_price)
                    else None
                )

                latest_history = _latest_product_history(db, product.id)

                latest_history_price = (
                    float(latest_history.price)
                    if latest_history is not None
                    and _valid_price(latest_history.price)
                    else None
                )

                latest_history_at = (
                    latest_history.recorded_at
                    if latest_history is not None
                    else None
                )

                # Compare against the latest recorded observation. This also
                # handles products that had no initial history point.
                price_changed = (
                    latest_history_price is None
                    or latest_history_price != new_price
                )

                daily_observation_due = (
                    latest_history_at is None
                    or now - latest_history_at >= HISTORY_INTERVAL
                )

                should_record_history = (
                    price_changed or daily_observation_due
                )

                # Update the product only after finding a valid matching price.
                product.current_price = new_price

                # Only the price is refreshed. The tracked product's title,
                # image, retailer and URL are what the user chose, so they are
                # never replaced by whatever listing happened to be cheapest.
                if result.get("image_url") and not product.image_url:
                    product.image_url = result["image_url"]

                # Record product-level history on price change or daily due.
                if should_record_history:
                    db.add(
                        PriceHistory(
                            product_id=product.id,
                            price=new_price,
                            recorded_at=now,
                        )
                    )
                    history_points += 1

                # Update the existing offer belonging to this product.
                offer = _get_offer_for_product(db, product)

                if offer is None:
                    offer = _create_offer_if_url_is_available(
                        db=db,
                        product=product,
                        result=result,
                        now=now,
                        price=new_price,
                    )
                else:
                    # CRITICAL FIX:
                    # Do not change offer.url. Its database column is unique,
                    # and Google Shopping may return a URL used by another offer.
                    offer.current_price = new_price
                    offer.currency = result.get("currency") or "INR"
                    offer.price_status = "provider_reported"
                    offer.last_checked = now

                # Keep offer history in sync when an offer exists.
                if offer is not None and should_record_history:
                    db.add(
                        OfferPriceHistory(
                            offer_id=offer.id,
                            price=new_price,
                            currency=offer.currency or "INR",
                            recorded_at=now,
                        )
                    )

                # Flush before sending alerts so database errors are detected
                # before any email is sent.
                db.flush()

                subscriptions_query = db.query(Subscription).filter(
                    Subscription.product_id == product.id
                )

                if user_email:
                    subscriptions_query = subscriptions_query.filter(
                        Subscription.user_email == user_email
                    )

                subscriptions = subscriptions_query.all()

                for subscription in subscriptions:
                    target = (
                        float(subscription.target_price)
                        if _valid_price(subscription.target_price)
                        else None
                    )

                    # Do not repeatedly email the same subscription while the
                    # price stays at or below its target.
                    already_alerted = (
                        getattr(subscription, "alert_sent_at", None)
                        is not None
                    )

                    # Re-arm: once the price rises back above the target, a
                    # later drop below it should alert again.
                    if (
                        target is not None
                        and new_price > target
                        and already_alerted
                    ):
                        subscription.alert_sent_at = None
                        already_alerted = False

                    if (
                        target is not None
                        and new_price <= target
                        and not already_alerted
                    ):
                        try:
                            sent = send_price_alert(
                                subscription.user_email,
                                product.title,
                                new_price,
                                target,
                            )
                        except Exception:
                            sent = False
                            logger.exception(
                                "Email alert raised an error for product_id=%s",
                                product.id,
                            )

                        if sent:
                            # Mark the subscription only after the email
                            # service confirms that sending succeeded.
                            subscription.alert_sent_at = now
                            alerts_sent += 1

                            logger.info(
                                "Target-price alert sent for product_id=%s "
                                "user=%s",
                                product.id,
                                subscription.user_email,
                            )
                        else:
                            logger.warning(
                                "Target-price alert was not sent for "
                                "product_id=%s user=%s",
                                product.id,
                                subscription.user_email,
                            )

                # Commit each product separately so a failure on another
                # product cannot roll back this product's saved history.
                db.commit()
                checked += 1

            except ProductSearchError as exc:
                db.rollback()
                skipped += 1

                logger.warning(
                    "Google Shopping search failed for product_id=%s: %s",
                    product.id,
                    exc,
                )

            except Exception:
                db.rollback()
                skipped += 1

                logger.exception(
                    "Price refresh failed for product_id=%s",
                    product.id,
                )

        summary = {
            "busy": False,
            "checked": checked,
            "history_points": history_points,
            "skipped": skipped,
            "alerts_sent": alerts_sent,
        }

        logger.info("Price refresh finished: %s", summary)
        return summary

    except Exception:
        db.rollback()
        logger.exception("Price refresh job failed")

        return {
            "busy": False,
            "checked": checked,
            "history_points": history_points,
            "skipped": skipped,
            "alerts_sent": alerts_sent,
        }

    finally:
        db.close()
        _refresh_lock.release()