"""ORM model for frozen plans (idempotency store)."""
from __future__ import annotations

from sqlalchemy import JSON, Column, DateTime, Integer, String, func

from .db import Base


class PlanRecord(Base):
    __tablename__ = "plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    request_id = Column(String(128), nullable=False, unique=True, index=True)
    request_hash = Column(String(64), nullable=False)
    request_json = Column(JSON, nullable=False)
    status = Column(String(16), nullable=False)
    response_json = Column(JSON, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(),
                        nullable=False)
