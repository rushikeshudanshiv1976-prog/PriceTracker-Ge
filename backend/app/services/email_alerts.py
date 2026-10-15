
"""SMTP email alerts for PriceTracker-Ge."""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage

logger = logging.getLogger(__name__)


def send_price_alert(
    to_email: str,
    title: str,
    price: float,
    target: float | None = None,
) -> bool:
    """Send a price alert. Return True only when delivery is accepted by SMTP."""

    host = os.getenv("SMTP_HOST", "smtp.gmail.com").strip()
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    sender = os.getenv("SMTP_FROM", username).strip()
    port = int(os.getenv("SMTP_PORT", "587"))

    if not all([host, username, password, sender, to_email]):
        logger.error(
            "Email alert not sent: SMTP_HOST, SMTP_USERNAME, "
            "SMTP_PASSWORD, SMTP_FROM, and recipient must be configured."
        )
        return False

    msg = EmailMessage()
    msg["Subject"] = f"Price alert: {title[:90]}"
    msg["From"] = sender
    msg["To"] = to_email

    target_text = (
        f"\nYour target price is ₹{target:,.2f}."
        if target is not None
        else ""
    )

    msg.set_content(
        f"Good news!\n\n"
        f"The latest observed price for {title} is ₹{price:,.2f}."
        f"{target_text}\n\n"
        "Prices may change. Please open the product link in "
        "Price Tracker to confirm the current offer."
    )

    try:
        context = ssl.create_default_context()

        with smtplib.SMTP(host, port, timeout=20) as server:
            server.ehlo()
            server.starttls(context=context)
            server.ehlo()
            server.login(username, password)
            server.send_message(msg)

        logger.info("Price alert email accepted by SMTP for %s", to_email)
        return True

    except (OSError, smtplib.SMTPException):
        logger.exception("Failed to send price alert email to %s", to_email)
        return False
