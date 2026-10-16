from typing import Any
from datetime import datetime, timedelta
from urllib.parse import urlparse
import math

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, HttpUrl, Field
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.security import get_current_user
from app.models import (
    Product,
    ProductOffer,
    OfferPriceHistory,
    PriceHistory,
    Subscription,
    User,
)
from app.services.scraper import (
    is_provider_url,
    scrape_product_page,
)
from app.services.product_search import (
    ProductSearchError,
    search_external_products,
)
from app.services.scraper import title_from_url
from app.services.price_fallback import lookup_provider_price
from app.services.cleanup import delete_product_completely, has_subscribers
from app.services.product_comparison import compare_product_offers
from app.services.discount_detector import annotate_discount_evidence


router = APIRouter(prefix="/api/products", tags=["Products"])


class IngestProductRequest(BaseModel):
    url: HttpUrl
    # Optional name, used ONLY when the store blocks the page fetch (Amazon,
    # Flipkart etc. often do). Prices then come from Google Shopping search.
    title: str | None = Field(default=None, max_length=300)


class ExternalTrackRequest(BaseModel):
    title: str = Field(min_length=2, max_length=300)
    url: HttpUrl
    retailer: str = Field(min_length=1, max_length=120)
    price: float | None = Field(default=None, gt=0)
    image_url: HttpUrl | None = None
    target_price: float | None = Field(default=None, gt=0)


class TargetPriceRequest(BaseModel):
    target_price: float | None = Field(default=None, gt=0)


class UpdateProductURLRequest(BaseModel):
    url: HttpUrl


def product_payload(product: Product) -> dict[str, Any]:
    return {
        "id": product.id,
        "title": product.title,
        "image_url": product.image_url,
        "retailer": product.retailer,
        "current_price": product.current_price,
        "url": product.url,
        "price_status": (
            "unverified" if product.current_price is None else "stored"
        ),
    }


def like_pattern(term: str) -> str:
    """Escape LIKE wildcards so user input like '100%' or 'a_b' is literal."""
    escaped = (
        term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    )
    return f"%{escaped}%"


def get_retailer(url: str) -> str:
    """Return a normalized hostname for display."""
    return (
        urlparse(url).hostname or "Unknown retailer"
    ).removeprefix("www.")


def refresh_product_data(db: Session, product: Product) -> dict[str, Any]:
    """
    Refresh one catalog product.

    Never overwrite the last known price when a new price
    cannot be extracted from the retailer page.
    """
    if is_provider_url(product.url):
        raise HTTPException(
            status_code=400,
            detail=(
                "This product uses a Google Shopping/provider URL, "
                "not a direct retailer product URL. Update the URL "
                "before refreshing."
            ),
        )

    now = datetime.utcnow()
    scraped = scrape_product_page(product.url)

    if scraped.get("status") == "error":
        raise HTTPException(
            status_code=502,
            detail=scraped.get(
                "message",
                "Could not retrieve the product page.",
            ),
        )

    final_url = scraped.get("url") or product.url

    if is_provider_url(final_url):
        raise HTTPException(
            status_code=502,
            detail="The product page redirected to a shopping provider.",
        )

    title = scraped.get("title")
    image_url = scraped.get("image_url")
    raw_price = scraped.get("current_price")

    try:
        price = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price = None

    if price is not None and (
        not math.isfinite(price) or price <= 0
    ):
        price = None

    retailer = get_retailer(final_url)

    try:
        if title and title != "Unknown product":
            product.title = title

        if image_url:
            product.image_url = image_url

        product.retailer = retailer

        # Only update the stored price after a successful price extraction.
        if price is not None:
            product.current_price = price

        # Find an offer associated with this product and its current URL.
        offer = (
            db.query(ProductOffer)
            .filter(
                ProductOffer.product_id == product.id,
                ProductOffer.url == product.url,
            )
            .first()
        )

        if offer is None:
            offer = ProductOffer(
                product_id=product.id,
                retailer=retailer,
                url=product.url,
                current_price=price,
                currency="INR",
                price_status=(
                    "verified" if price is not None else "unavailable"
                ),
                last_checked=now,
            )
            db.add(offer)
            db.flush()
        else:
            offer.retailer = retailer
            offer.last_checked = now

            if price is not None:
                offer.current_price = price
                offer.price_status = "verified"
            elif offer.current_price is not None:
                offer.price_status = "stale"
            else:
                offer.price_status = "unavailable"

        # Record history only for a newly extracted positive price.
        if price is not None:
            db.add(
                OfferPriceHistory(
                    offer_id=offer.id,
                    price=price,
                    currency="INR",
                    recorded_at=now,
                )
            )
            db.add(
                PriceHistory(
                    product_id=product.id,
                    price=price,
                    recorded_at=now,
                )
            )

        db.commit()
        db.refresh(product)
        db.refresh(offer)

    except Exception:
        db.rollback()
        raise

    return {
        "message": (
            "Price refreshed successfully."
            if price is not None
            else (
                "The page was checked, but a new price could not "
                "be verified. The last known price was preserved."
            )
        ),
        "product_id": product.id,
        "offer_id": offer.id,
        "title": product.title,
        "image_url": product.image_url,
        "retailer": product.retailer,
        "url": product.url,
        "current_price": product.current_price,
        "latest_scraped_price": price,
        "price_status": offer.price_status,
        "last_checked": (
            offer.last_checked.isoformat()
            if offer.last_checked
            else None
        ),
    }


@router.get("/search")
def search_products(
    q: str = Query(min_length=1, max_length=120),
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
):
    """Search products already in the local catalog."""
    term = q.strip()

    if not term:
        return []

    products = (
        db.query(Product)
        .filter(Product.title.ilike(like_pattern(term), escape="\\"))
        .order_by(Product.title.asc())
        .limit(limit)
        .all()
    )

    return [product_payload(product) for product in products]


@router.post(
    "/{product_id}/track",
    status_code=status.HTTP_201_CREATED,
)
def track_product(
    product_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    product = (
        db.query(Product)
        .filter(Product.id == product_id)
        .first()
    )

    if product is None:
        raise HTTPException(
            status_code=404,
            detail="Product not found in the catalog",
        )

    existing = (
        db.query(Subscription)
        .filter(
            Subscription.user_email == current_user.email,
            Subscription.product_id == product_id,
        )
        .first()
    )

    if existing:
        return {
            "message": "Product is already in your watchlist",
            "subscription_id": existing.id,
        }

    subscription = Subscription(
        user_email=current_user.email,
        product_id=product_id,
    )

    db.add(subscription)
    try:
        db.commit()
    except IntegrityError:
        # Concurrent duplicate request: the unique (user, product) constraint
        # rejected it, so return the row that already exists.
        db.rollback()
        existing = (
            db.query(Subscription)
            .filter(
                Subscription.user_email == current_user.email,
                Subscription.product_id == product_id,
            )
            .first()
        )
        return {
            "message": "Product is already in your watchlist",
            "subscription_id": existing.id if existing else None,
        }
    db.refresh(subscription)

    return {
        "message": "Product added to your watchlist",
        "subscription_id": subscription.id,
    }


@router.get("/watchlist")
def get_watchlist(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    rows = (
        db.query(Subscription, Product)
        .join(Product, Product.id == Subscription.product_id)
        .filter(Subscription.user_email == current_user.email)
        .order_by(Subscription.id.asc())
        .all()
    )

    result = []

    for sub, product in rows:
        item = product_payload(product)
        item.update({
            "subscription_id": sub.id,
            "target_price": sub.target_price,
        })
        result.append(item)

    return result


@router.post("/watchlist/refresh")
def manual_refresh_watchlist(
    current_user: User = Depends(get_current_user),
):
    """
    Manually refresh products tracked by the signed-in user.

    This calls the same function used by the automatic scheduler.
    The user's email is passed so other users' products and alerts
    are not included in this manual request.
    """
    from app.services.background_jobs import refresh_tracked_prices

    result = refresh_tracked_prices(user_email=current_user.email)

    # Return a clear response if an automatic or manual refresh is
    # already running.
    if result.get("busy"):
        raise HTTPException(
            status_code=409,
            detail="A price refresh is already running. Please try again shortly.",
        )

    # Send the refresh summary back to the frontend.
    return {
        "message": "Price refresh completed.",
        **result,
    }



@router.patch("/watchlist/{subscription_id}/url")
def update_tracked_product_url(
    subscription_id: int,
    payload: UpdateProductURLRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """
    Update a tracked product URL while preserving its product ID,
    stored price, price history, subscription ID and target price.
    """
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.id == subscription_id,
            Subscription.user_email == current_user.email,
        )
        .first()
    )

    if subscription is None:
        raise HTTPException(
            status_code=404,
            detail="Tracked product not found in your watchlist.",
        )

    new_url = str(payload.url).rstrip("/")

    try:
        parsed = urlparse(new_url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Enter a valid retailer product URL.",
        ) from exc

    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        raise HTTPException(
            status_code=400,
            detail="Enter a valid HTTP or HTTPS product URL.",
        )

    if is_provider_url(new_url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Use the direct retailer product URL, not a "
                "Google Shopping/provider URL."
            ),
        )

    product = (
        db.query(Product)
        .filter(Product.id == subscription.product_id)
        .first()
    )

    if product is None:
        raise HTTPException(
            status_code=404,
            detail="Product not found.",
        )

    duplicate_product = (
        db.query(Product)
        .filter(
            Product.url == new_url,
            Product.id != product.id,
        )
        .first()
    )

    if duplicate_product:
        raise HTTPException(
            status_code=409,
            detail="This URL already belongs to another product.",
        )

    duplicate_offer = (
        db.query(ProductOffer)
        .filter(
            ProductOffer.url == new_url,
            ProductOffer.product_id != product.id,
        )
        .first()
    )

    if duplicate_offer:
        raise HTTPException(
            status_code=409,
            detail=(
                "This URL is already registered as another "
                "product's retailer offer."
            ),
        )

    old_url = product.url

    try:
        # Preserve product.id, current_price, history and subscription.
        product.url = new_url

        # Update the existing offer only when it matches the old URL.
        offer = (
            db.query(ProductOffer)
            .filter(
                ProductOffer.product_id == product.id,
                ProductOffer.url == old_url,
            )
            .first()
        )

        if offer:
            offer.url = new_url
            offer.retailer = get_retailer(new_url)
            offer.last_checked = None

            # The previous provider price is not verified for this URL.
            offer.price_status = (
                "stale"
                if offer.current_price is not None
                else "unavailable"
            )

        db.commit()
        db.refresh(product)
        db.refresh(subscription)

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not update the product URL.",
        ) from exc

    return {
        "message": "Product URL updated. Refresh it to check the retailer price.",
        "subscription_id": subscription.id,
        "product_id": product.id,
        "url": product.url,
        "current_price": product.current_price,
        "target_price": subscription.target_price,
    }


@router.delete(
    "/watchlist/{subscription_id}",
    status_code=status.HTTP_200_OK,
)
def remove_from_watchlist(
    subscription_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.id == subscription_id,
            Subscription.user_email == current_user.email,
        )
        .first()
    )

    if subscription is None:
        raise HTTPException(
            status_code=404,
            detail="Tracked product not found",
        )

    product_id = subscription.product_id
    db.delete(subscription)
    db.flush()

    # If nobody else tracks this product, delete it for good (offers and
    # price history included) so it is not refreshed or kept in the database.
    removed_permanently = False
    if not has_subscribers(db, product_id):
        delete_product_completely(db, product_id)
        removed_permanently = True

    db.commit()

    return {
        "message": "Product removed from your watchlist",
        "deleted_permanently": removed_permanently,
    }


@router.post(
    "/watchlist/{subscription_id}/refresh",
    status_code=status.HTTP_200_OK,
)
def refresh_tracked_product(
    subscription_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    from app.services.background_jobs import refresh_tracked_prices

    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.id == subscription_id,
            Subscription.user_email == current_user.email,
        )
        .first()
    )

    if subscription is None:
        raise HTTPException(
            status_code=404,
            detail="Tracked product not found",
        )

    # Refresh only this item, not the user's whole watchlist.
    result = refresh_tracked_prices(
        user_email=current_user.email,
        product_id=subscription.product_id,
    )

    if result.get("busy"):
        raise HTTPException(
            status_code=409,
            detail="A price refresh is already running. Try again shortly.",
        )

    return {
        "message": "Price refresh completed.",
        **result,
    }



@router.get("/compare")
def compare_product_prices(
    q: str = Query(min_length=1, max_length=120),
    db: Session = Depends(get_db),
):
    """Compare saved retailer offers for matching catalog products."""
    term = q.strip()

    if not term:
        return {"query": term, "offers": []}

    products = (
        db.query(Product)
        .filter(Product.title.ilike(like_pattern(term), escape="\\"))
        .all()
    )

    if not products:
        return {"query": term, "offers": []}

    product_ids = [product.id for product in products]

    offers = (
        db.query(ProductOffer)
        .options(joinedload(ProductOffer.product))
        .filter(ProductOffer.product_id.in_(product_ids))
        .all()
    )

    results = []

    for offer in offers:
        results.append({
            "offer_id": offer.id,
            "product_id": offer.product_id,
            "product_title": offer.product.title,
            "retailer": offer.retailer,
            "url": offer.url,
            "price": offer.current_price,
            "currency": offer.currency,
            "price_status": offer.price_status,
            "last_checked": (
                offer.last_checked.isoformat()
                if offer.last_checked
                else None
            ),
        })

    results.sort(
        key=lambda item: (
            item["price"] is None
            or item["price_status"] != "verified",
            item["price"]
            if item["price"] is not None
            else float("inf"),
        )
    )

    return {
        "query": term,
        "offers": results,
        "offer_count": len(results),
    }


@router.post(
    "/ingest",
    status_code=status.HTTP_201_CREATED,
)
def ingest_product(
    payload: IngestProductRequest,
    db: Session = Depends(get_db),
):
    """Ingest a direct retailer URL and save available observations."""
    url = str(payload.url)

    if is_provider_url(url):
        raise HTTPException(
            status_code=400,
            detail=(
                "Google Shopping/provider URLs cannot be ingested as "
                "retailer product pages. Use a direct retailer URL."
            ),
        )

    scraped = scrape_product_page(url)

    if scraped.get("status") == "error":
        raise HTTPException(
            status_code=422,
            detail=scraped.get(
                "message",
                "Unable to retrieve product page.",
            ),
        )

    final_url = scraped.get("url") or url

    if is_provider_url(final_url):
        raise HTTPException(
            status_code=400,
            detail="The URL redirected to a shopping provider.",
        )

    title = scraped.get("title")
    used_manual_title = False

    if not title or title == "Unknown product":
        manual_title = " ".join((payload.title or "").split())

        # No typed name: use the name in the URL itself (shown by Amazon and
        # Flipkart links) when the store blocks the page.
        if len(manual_title) < 2 and scraped.get("status") in {
            "blocked",
            "partial",
        }:
            manual_title = title_from_url(final_url) or ""

        # Fallback only when a public page was reached but the store blocked
        # us or gave no usable title ("blocked"/"partial"). Unsafe or
        # unreachable URLs ("error", handled above) never get this fallback.
        if len(manual_title) >= 2 and scraped.get("status") in {
            "blocked",
            "partial",
        }:
            title = manual_title
            used_manual_title = True
        else:
            raise HTTPException(
                status_code=422,
                detail=(
                    scraped.get("message")
                    or "Could not identify the product on this page."
                ),
            )

    retailer = get_retailer(final_url)
    raw_price = scraped.get("current_price")

    try:
        price = float(raw_price) if raw_price is not None else None
    except (TypeError, ValueError):
        price = None

    if price is not None and (
        not math.isfinite(price) or price <= 0
    ):
        price = None

    price_source = "retailer_page" if price is not None else None

    if price is None:
        # The page gave a name but no price (or the store blocked us): use
        # an honest Google Shopping price, labelled provider-reported.
        fallback = lookup_provider_price(title)
        if fallback:
            price = fallback["price"]
            price_source = "google_shopping"

    now = datetime.utcnow()

    try:
        product = (
            db.query(Product)
            .filter(Product.url == final_url)
            .first()
        )

        if product is None:
            product = Product(
                title=title,
                image_url=scraped.get("image_url"),
                retailer=retailer,
                current_price=price,
                url=final_url,
            )
            db.add(product)
            db.flush()
        else:
            # Keep an existing product's title when the name came from the
            # user because the store blocked the page.
            if not used_manual_title:
                product.title = title
            product.image_url = (
                scraped.get("image_url") or product.image_url
            )
            product.retailer = retailer

            if price is not None:
                product.current_price = price

        offer = (
            db.query(ProductOffer)
            .filter(ProductOffer.url == final_url)
            .first()
        )

        if offer is None:
            offer = ProductOffer(
                product_id=product.id,
                retailer=retailer,
                url=final_url,
                current_price=price,
                currency="INR",
                price_status=(
                    (
                        "verified"
                        if price_source == "retailer_page"
                        else "provider_reported"
                    )
                    if price is not None
                    else "unavailable"
                ),
                last_checked=now,
            )
            db.add(offer)
            db.flush()
        else:
            offer.product_id = product.id
            offer.retailer = retailer
            offer.last_checked = now

            if price is not None:
                offer.current_price = price
                offer.price_status = (
                    "verified"
                    if price_source == "retailer_page"
                    else "provider_reported"
                )
            elif offer.current_price is not None:
                offer.price_status = "stale"
            else:
                offer.price_status = "unavailable"

        if price is not None:
            db.add(
                OfferPriceHistory(
                    offer_id=offer.id,
                    price=price,
                    currency="INR",
                    recorded_at=now,
                )
            )
            db.add(
                PriceHistory(
                    product_id=product.id,
                    price=price,
                    recorded_at=now,
                )
            )

        db.commit()
        db.refresh(product)
        db.refresh(offer)

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not save the product. Check backend logs.",
        ) from exc

    return {
        "message": "Product metadata processed",
        "used_manual_title": used_manual_title,
        "product_id": product.id,
        "offer_id": offer.id,
        "title": product.title,
        "retailer": retailer,
        "price": price,
        "price_source": price_source,
        "stored_price": offer.current_price,
        "price_status": offer.price_status,
        "url": final_url,
        "image_url": product.image_url,
        "last_checked": (
            offer.last_checked.isoformat()
            if offer.last_checked
            else None
        ),
    }


@router.get("/compare/external")
def compare_external_products(
    q: str = Query(min_length=2, max_length=120),
    limit: int = Query(default=20, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Find and group matching external shopping listings."""
    term = q.strip()

    if len(term) < 2:
        raise HTTPException(
            status_code=422,
            detail="Enter at least 2 characters.",
        )

    try:
        offers = search_external_products(term, limit)
    except ProductSearchError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    # Enrich existing listings without changing the route's established
    # response shape. The detector only uses a matching stored retailer offer.
    for offer in offers:
        annotate_discount_evidence(db, offer)

    groups = compare_product_offers(offers)

    return {
        "query": term,
        "source": "Google Shopping via SerpApi",
        "result_count": len(groups),
        "groups": groups,
    }


@router.get("/search/external")
def search_external(
    q: str = Query(min_length=2, max_length=120),
    limit: int = Query(default=20, ge=1, le=20),
    db: Session = Depends(get_db),
):
    """Search external listings without saving them."""
    term = q.strip()

    if len(term) < 2:
        raise HTTPException(
            status_code=422,
            detail="Enter at least two characters.",
        )

    try:
        results = search_external_products(term, limit)
    except ProductSearchError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        ) from exc

    for result in results:
        annotate_discount_evidence(db, result)

    return {
        "query": term,
        "source": "Google Shopping via SerpApi",
        "results": results,
        "result_count": len(results),
    }


@router.post(
    "/track-external",
    status_code=status.HTTP_201_CREATED,
)
def track_external_product(
    payload: ExternalTrackRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Save an external listing and its watchlist entry."""
    url = str(payload.url)
    image_url = str(payload.image_url) if payload.image_url else None
    now = datetime.utcnow()

    try:
        product = (
            db.query(Product)
            .filter(Product.url == url)
            .first()
        )

        if product is None:
            product = Product(
                title=payload.title,
                image_url=image_url,
                retailer=payload.retailer,
                current_price=payload.price,
                url=url,
            )
            db.add(product)
            db.flush()
        else:
            product.title = payload.title
            product.retailer = payload.retailer

            if image_url:
                product.image_url = image_url

            if payload.price is not None:
                product.current_price = payload.price

        offer = (
            db.query(ProductOffer)
            .filter(ProductOffer.url == url)
            .first()
        )

        if offer is None:
            offer = ProductOffer(
                product_id=product.id,
                retailer=payload.retailer,
                url=url,
                current_price=payload.price,
                currency="INR",
                price_status=(
                    "provider_reported"
                    if payload.price is not None
                    else "unavailable"
                ),
                last_checked=now,
            )
            db.add(offer)
            db.flush()
        else:
            offer.product_id = product.id
            offer.retailer = payload.retailer

            if payload.price is not None:
                offer.current_price = payload.price
                offer.price_status = "provider_reported"
                offer.last_checked = now


        subscription = (
            db.query(Subscription)
            .filter(
                Subscription.user_email == current_user.email,
                Subscription.product_id == product.id,
            )
            .first()
        )

        if subscription is None:
            subscription = Subscription(
                user_email=current_user.email,
                product_id=product.id,
                target_price=payload.target_price,
            )
            db.add(subscription)
        elif payload.target_price is not None:
            subscription.target_price = payload.target_price

        db.commit()
        db.refresh(product)
        db.refresh(offer)
        db.refresh(subscription)

    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=500,
            detail="Could not save this product. Check backend logs.",
        ) from exc

    return {
        "message": "Product added to tracking.",
        "product_id": product.id,
        "subscription_id": subscription.id,
        "title": product.title,
        "image_url": product.image_url,
        "current_price": product.current_price,
        "retailer": product.retailer,
        "target_price": subscription.target_price,
        "price_status": offer.price_status,
    }


@router.patch("/watchlist/{subscription_id}/target")
def update_target_price(
    subscription_id: int,
    payload: TargetPriceRequest,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.id == subscription_id,
            Subscription.user_email == current_user.email,
        )
        .first()
    )

    if subscription is None:
        raise HTTPException(
            status_code=404,
            detail="Tracked product not found",
        )

    subscription.target_price = payload.target_price
    # A new target is a new alert condition, so allow it to fire again.
    subscription.alert_sent_at = None
    db.commit()
    db.refresh(subscription)

    return {
        "message": "Target price updated",
        "target_price": subscription.target_price,
    }


@router.get("/watchlist/{subscription_id}/history")
def get_tracked_price_history(
    subscription_id: int,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Return 90-day history and a cautious 30-day trend estimate."""
    subscription = (
        db.query(Subscription)
        .filter(
            Subscription.id == subscription_id,
            Subscription.user_email == current_user.email,
        )
        .first()
    )

    if subscription is None:
        raise HTTPException(
            status_code=404,
            detail="Tracked product not found",
        )

    product = (
        db.query(Product)
        .filter(Product.id == subscription.product_id)
        .first()
    )

    cutoff = datetime.utcnow() - timedelta(days=90)

    rows = (
        db.query(PriceHistory)
        .filter(
            PriceHistory.product_id == subscription.product_id,
            PriceHistory.recorded_at >= cutoff,
        )
        .order_by(PriceHistory.recorded_at.asc())
        .all()
    )

    history = [
        {
            "price": float(row.price),
            "recorded_at": row.recorded_at.isoformat(),
        }
        for row in rows
    ]

    values = [point["price"] for point in history]

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    drops = [-change for change in changes if change < 0]
    rises = [change for change in changes if change > 0]

    average_drop = (
        round(sum(drops) / len(drops), 2) if drops else 0
    )
    average_rise = (
        round(sum(rises) / len(rises), 2) if rises else 0
    )
    net_change = (
        round(values[-1] - values[0], 2)
        if len(values) > 1
        else 0
    )

    forecast = {
        "status": "insufficient_data",
        "message": (
            "Not enough historical data yet. Collect at least five "
            "observations across 14 or more days."
        ),
        "days_ahead": 30,
        "estimated_price": None,
        "estimated_low": None,
        "estimated_high": None,
        "daily_slope": None,
    }

    if len(history) >= 5:
        daily_prices = {}

        for point in history:
            day = datetime.fromisoformat(
                point["recorded_at"]
            ).date().isoformat()

            daily_prices.setdefault(day, []).append(point["price"])

        daily = sorted(
            (day, sum(prices) / len(prices))
            for day, prices in daily_prices.items()
        )

        first_day = datetime.fromisoformat(daily[0][0]).date()

        x_values = [
            (
                datetime.fromisoformat(day).date() - first_day
            ).days
            for day, _price in daily
        ]

        y_values = [price for _day, price in daily]

        span_days = x_values[-1] - x_values[0] if x_values else 0

        if len(daily) >= 5 and span_days >= 14:
            x_mean = sum(x_values) / len(x_values)
            y_mean = sum(y_values) / len(y_values)

            denominator = sum(
                (x - x_mean) ** 2 for x in x_values
            )

            if denominator > 0 and y_mean > 0:
                slope = sum(
                    (x - x_mean) * (y - y_mean)
                    for x, y in zip(x_values, y_values)
                ) / denominator

                slope = max(
                    -0.01 * y_mean,
                    min(0.01 * y_mean, slope),
                )

                last_price = values[-1]
                estimate = max(0.0, last_price + slope * 30)

                intercept = y_mean - slope * x_mean

                residuals = [
                    abs(y - (intercept + slope * x))
                    for x, y in zip(x_values, y_values)
                ]

                average_error = (
                    sum(residuals) / len(residuals)
                    if residuals
                    else 0.0
                )

                band = max(average_error, last_price * 0.01)

                if slope < -0.005 * y_mean:
                    message = "The stored prices show a downward trend."
                elif slope > 0.005 * y_mean:
                    message = "The stored prices show an upward trend."
                else:
                    message = "The stored prices look relatively stable."

                forecast = {
                    "status": "available",
                    "message": message,
                    "days_ahead": 30,
                    "estimated_price": round(estimate, 2),
                    "estimated_low": round(
                        max(0.0, estimate - band), 2
                    ),
                    "estimated_high": round(estimate + band, 2),
                    "daily_slope": round(slope, 2),
                    "observations": len(history),
                    "days_spanned": span_days,
                }

    return {
        "product_id": subscription.product_id,
        "title": product.title if product else "Tracked product",
        "history": history,
        "history_days": 90,
        "average_drop": average_drop,
        "average_rise": average_rise,
        "net_change": net_change,
        "forecast": forecast,
        "message": (
            "History starts when Price Tracker first records prices. "
            "Older prices are not invented. Forecasts are estimates, "
            "not guarantees of future prices."
        ),
    }