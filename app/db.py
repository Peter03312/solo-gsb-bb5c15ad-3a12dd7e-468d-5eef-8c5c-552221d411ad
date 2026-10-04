"""Database engine, session factory and startup table creation."""
from __future__ import annotations

import time

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from .config import settings

_connect_args = (
    {"check_same_thread": False}
    if settings.DATABASE_URL.startswith("sqlite")
    else {}
)

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True,
                       connect_args=_connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()


def init_db() -> None:
    """Create tables, retrying while the database is still starting up."""
    from . import models  # noqa: F401  (register tables)

    last: Exception | None = None
    for _ in range(settings.DB_CONNECT_RETRIES):
        try:
            Base.metadata.create_all(engine)
            return
        except Exception as exc:  # database not ready yet
            last = exc
            time.sleep(settings.DB_CONNECT_DELAY_SECONDS)
    raise RuntimeError(f"cannot initialise database: {last}")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
