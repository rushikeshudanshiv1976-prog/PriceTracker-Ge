"""Helpers for permanently deleting products and their data."""

from sqlalchemy.orm import Session

from app.models import (
    OfferPriceHistory,
    PriceHistory,
    Product,
    ProductOffer,
    Subscription,
)


def delete_product_completely(db: Session, product_id: int) -> None:
    """
    Delete a product plus its offers and all price history.
    Does not commit; the caller commits. Subscriptions must already be gone.
    """
    offer_ids = [
        row[0]
        for row in db.query(ProductOffer.id)
        .filter(ProductOffer.product_id == product_id)
        .all()
    ]

    if offer_ids:
        db.query(OfferPriceHistory).filter(
            OfferPriceHistory.offer_id.in_(offer_ids)
        ).delete(synchronize_session=False)
        db.query(ProductOffer).filter(
            ProductOffer.id.in_(offer_ids)
        ).delete(synchronize_session=False)

    db.query(PriceHistory).filter(
        PriceHistory.product_id == product_id
    ).delete(synchronize_session=False)
    db.query(Product).filter(Product.id == product_id).delete(
        synchronize_session=False
    )


def has_subscribers(db: Session, product_id: int) -> bool:
    return (
        db.query(Subscription.id)
        .filter(Subscription.product_id == product_id)
        .first()
        is not None
    )


def find_orphan_products(db: Session) -> list[Product]:
    """Products that nobody is tracking."""
    tracked = db.query(Subscription.product_id).distinct()
    return (
        db.query(Product)
        .filter(~Product.id.in_(tracked))
        .order_by(Product.id.asc())
        .all()
    )