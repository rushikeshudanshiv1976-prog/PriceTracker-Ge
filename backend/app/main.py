import logging
import os
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

from app.database import Base, engine
from app import models  # Register ORM models before create_all.
from app.routes import auth, products

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger(__name__)


def price_scheduler_enabled() -> bool:
    return os.getenv(
        "ENABLE_PRICE_SCHEDULER", "true"
    ).strip().lower() in {"1", "true", "yes"}


def refresh_on_startup() -> bool:
    # Off by default: with uvicorn --reload every code save restarts the
    # server, and a startup refresh spends one SerpApi search per product.
    return os.getenv(
        "REFRESH_ON_STARTUP", "false"
    ).strip().lower() in {"1", "true", "yes"}


def price_refresh_hours() -> int:
    try:
        hours = int(os.getenv("PRICE_REFRESH_HOURS", "12"))
        if hours < 1:
            raise ValueError("PRICE_REFRESH_HOURS must be at least 1.")
        return hours
    except ValueError:
        logger.warning(
            "Invalid PRICE_REFRESH_HOURS; using the default of 12 hours."
        )
        return 12


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize database tables.
    try:
        Base.metadata.create_all(bind=engine)
        logger.info("Database initialization completed.")
    except Exception:
        logger.exception(
            "Database initialization failed. "
            "Check DATABASE_URL and PostgreSQL availability."
        )

    scheduler = None

    if price_scheduler_enabled():
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
            from app.services.background_jobs import refresh_tracked_prices

            scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

            # Optionally run the first refresh right after startup. Disable it
            # (REFRESH_ON_STARTUP=false) while developing with --reload so
            # every code save does not spend SerpApi quota.
            if refresh_on_startup():
                scheduler.add_job(
                    refresh_tracked_prices,
                    "date",
                    id="refresh_tracked_prices_startup",
                    replace_existing=True,
                )

            # Schedule recurring price refreshes.
            refresh_hours = price_refresh_hours()

            scheduler.add_job(
                refresh_tracked_prices,
                "interval",
                hours=refresh_hours,
                id="refresh_tracked_prices",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )

            scheduler.start()

            # Get the actual next run time of the recurring job.
            recurring_job = scheduler.get_job("refresh_tracked_prices")

            if recurring_job and recurring_job.next_run_time:
                next_run_time = recurring_job.next_run_time.strftime(
                    "%d %b %Y, %I:%M:%S %p %Z"
                )
            else:
                next_run_time = "Not available"

            logger.info(
                "Price scheduler started. "
                "startup refresh: %s; "
                "recurring refresh interval: every %s hour(s). "
                "Next recurring refresh: %s",
                refresh_on_startup(),
                refresh_hours,
                next_run_time,
            )

        except ImportError:
            logger.exception(
                "APScheduler is not installed. Automatic price refresh "
                "is disabled. Install it in the backend virtual environment."
            )
            scheduler = None

        except Exception:
            logger.exception("Price scheduler could not start.")
            if scheduler is not None and scheduler.running:
                scheduler.shutdown(wait=False)
            scheduler = None

    else:
        logger.info(
            "Automatic price scheduler disabled by ENABLE_PRICE_SCHEDULER."
        )

    app.state.price_scheduler = scheduler

    yield

    # Shut down the scheduler cleanly when FastAPI stops.
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        logger.info("Price scheduler stopped.")


app = FastAPI(
    title="Price Tracker API",
    version="1.0.0",
    lifespan=lifespan,
)


allowed_origins = [
    origin.strip().rstrip("/")
    for origin in os.getenv(
        "FRONTEND_ORIGINS",
        "https://price-tracker-rho-opal.vercel.app,"
        "http://localhost:5173,"
        "http://127.0.0.1:5173,"
        "http://localhost:3000",
    ).split(",")
    if origin.strip()
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

    allow_credentials=True,
    allow_methods=[
        "GET",
        "POST",
        "PATCH",
        "DELETE",
        "OPTIONS",
    ],
    allow_headers=[
        "Authorization",
        "Content-Type",
    ],
)

app.include_router(auth.router)
app.include_router(products.router)


@app.get("/")
def read_root():
    return {
        "message": "Price Tracker API is running",
        "version": app.version,
        "docs": "/docs",
        "price_scheduler_enabled": price_scheduler_enabled(),
    }