from sqlalchemy import Boolean
from sqlalchemy import UniqueConstraint
from datetime import datetime
from app.database import Base
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import relationship


class User(Base):
  __tablename__ = "users"
  id = Column(Integer, primary_key=True, index=True)
  email = Column(String, unique=True, index=True, nullable=False)
  hashed_password = Column(String, nullable=False)
  created_at = Column(DateTime, default=datetime.utcnow)


class Product(Base):
  __tablename__ = "products"
  id = Column(Integer, primary_key=True, index=True)
  title = Column(String, index=True, nullable=False)
  image_url = Column(String)
  retailer = Column(String)
  current_price = Column(Float, nullable=True)
  url = Column(String, unique=True)



class Subscription(Base):
  __tablename__ = "subscriptions"
  __table_args__ = (
    UniqueConstraint("user_email", "product_id", name="uq_subscription_user_product"),
  )
  id = Column(Integer, primary_key=True, index=True)
  user_email = Column(String, index=True, nullable=False)
  product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
  target_price = Column(Float, nullable=True)
  alert_sent_at = Column(DateTime, nullable=True, default=None)



class PriceHistory(Base):
  __tablename__ = "price_history"
  id = Column(Integer, primary_key=True, index=True)
  product_id = Column(Integer, ForeignKey("products.id"), nullable=False, index=True)
  price = Column(Float, nullable=False)
  recorded_at = Column(DateTime, default=datetime.utcnow, index=True)


class ProductOffer(Base):
    __tablename__ = "product_offers"

    id = Column(Integer, primary_key=True, index=True)

    product_id = Column(
        Integer,
        ForeignKey("products.id"),
        nullable=False,
        index=True,
    )

    retailer = Column(String, nullable=False)
    url = Column(String, unique=True, nullable=False)

    # NULL means no price has been verified.
    current_price = Column(Float, nullable=True)
    currency = Column(String, default="INR", nullable=False)

    price_status = Column(
        String,
        default="unavailable",
        nullable=False,
    )

    last_checked = Column(DateTime, nullable=True)

    product = relationship("Product")


class OfferPriceHistory(Base):
    __tablename__ = "offer_price_history"

    id = Column(Integer, primary_key=True, index=True)

    offer_id = Column(
        Integer,
        ForeignKey("product_offers.id"),
        nullable=False,
        index=True,
    )

    price = Column(Float, nullable=False)
    currency = Column(String, default="INR", nullable=False)
    recorded_at = Column(DateTime, default=datetime.utcnow)

    offer = relationship("ProductOffer")
