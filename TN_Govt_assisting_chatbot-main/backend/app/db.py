"""Database engine, session, and ORM models.

Governance rule baked in here: the audit log is APPEND-ONLY and there is no
hard-delete path anywhere. Closure is a status, never a row deletion.
"""
from __future__ import annotations
import datetime as dt
from sqlalchemy import (create_engine, String, Integer, Float, DateTime, Text, ForeignKey)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column, sessionmaker, relationship)
from .config import DB_URL

engine = create_engine(DB_URL, connect_args={"check_same_thread": False} if DB_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def now() -> dt.datetime:
    return dt.datetime.utcnow()


class Grievance(Base):
    __tablename__ = "grievances"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    reporter_phone: Mapped[str] = mapped_column(String)
    gps_lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    gps_lng: Mapped[float | None] = mapped_column(Float, nullable=True)
    channel: Mapped[str] = mapped_column(String, default="web")
    created_at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    status: Mapped[str] = mapped_column(String, default="registered")
    ui_language: Mapped[str] = mapped_column(String, default="en")
    assigned_dept: Mapped[str] = mapped_column(String)
    assigned_officer_id: Mapped[str | None] = mapped_column(String, nullable=True)
    sla_due_at: Mapped[dt.datetime] = mapped_column(DateTime)
    escalation_level: Mapped[int] = mapped_column(Integer, default=0)
    sensitivity: Mapped[str] = mapped_column(String, default="normal")
    # model-extracted
    category: Mapped[str] = mapped_column(String)
    sub_category: Mapped[str | None] = mapped_column(String, nullable=True)
    description_clean: Mapped[str] = mapped_column(Text)
    description_raw: Mapped[str] = mapped_column(Text)   # citizen's exact words — write once
    language_detected: Mapped[str] = mapped_column(String, default="en")
    ward: Mapped[str | None] = mapped_column(String, nullable=True)
    landmark: Mapped[str | None] = mapped_column(String, nullable=True)
    address_text: Mapped[str | None] = mapped_column(String, nullable=True)
    severity: Mapped[str] = mapped_column(String, default="medium")
    # dedup / hotspot
    parent_grievance_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reporter_count: Mapped[int] = mapped_column(Integer, default=1)

    events: Mapped[list["AuditEvent"]] = relationship(back_populates="grievance", order_by="AuditEvent.at")


class AuditEvent(Base):
    """Append-only. Never updated or deleted. Optional hash chain for integrity."""
    __tablename__ = "audit_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grievance_id: Mapped[str] = mapped_column(ForeignKey("grievances.id"))
    event_type: Mapped[str] = mapped_column(String)   # created|status|assign|note|reopen|sensitive_access
    from_val: Mapped[str | None] = mapped_column(String, nullable=True)
    to_val: Mapped[str | None] = mapped_column(String, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    actor_id: Mapped[str] = mapped_column(String)
    actor_role: Mapped[str] = mapped_column(String)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)
    prev_hash: Mapped[str | None] = mapped_column(String, nullable=True)
    hash: Mapped[str | None] = mapped_column(String, nullable=True)

    grievance: Mapped["Grievance"] = relationship(back_populates="events")


class Officer(Base):
    __tablename__ = "officers"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    role: Mapped[str] = mapped_column(String)   # ward_officer|dept_head|commissioner|collector|cm_cell|auditor
    department: Mapped[str | None] = mapped_column(String, nullable=True)
    ward: Mapped[str | None] = mapped_column(String, nullable=True)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    grievance_id: Mapped[str] = mapped_column(ForeignKey("grievances.id"))
    rating: Mapped[int] = mapped_column(Integer)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    at: Mapped[dt.datetime] = mapped_column(DateTime, default=now)


def init_db() -> None:
    Base.metadata.create_all(engine)
