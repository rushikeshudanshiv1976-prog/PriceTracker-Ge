import os

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

# Load backend/.env before reading settings (this module can be imported
# before main.py has called load_dotenv()).
load_dotenv()

# Set DATABASE_URL in backend/.env, e.g.
#   postgresql+psycopg2://user:password@localhost:5432/price_tracker
# No credentials live in source control. Without DATABASE_URL the app falls
# back to a local SQLite file so it still starts for development.
SQLALCHEMY_DATABASE_URL = os.getenv("DATABASE_URL", "").strip() or (
    "sqlite:///./price_tracker.db"
)

# Hosting providers often hand out "postgres://", which SQLAlchemy rejects.
if SQLALCHEMY_DATABASE_URL.startswith("postgres://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace(
        "postgres://", "postgresql+psycopg2://", 1
    )
elif SQLALCHEMY_DATABASE_URL.startswith("postgresql://"):
    SQLALCHEMY_DATABASE_URL = SQLALCHEMY_DATABASE_URL.replace(
        "postgresql://", "postgresql+psycopg2://", 1
    )

engine_options = {"pool_pre_ping": True}
if SQLALCHEMY_DATABASE_URL.startswith("sqlite"):
    engine_options["connect_args"] = {"check_same_thread": False}

engine = create_engine(SQLALCHEMY_DATABASE_URL, **engine_options)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
