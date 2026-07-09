"""Governance layer — the CM's-office standard.

* Every state change writes an APPEND-ONLY, hash-chained AuditEvent.
* Sensitive grievances are flagged, routed away from the official they concern,
  and masked from the ordinary queue.
* SLA due dates and an escalation ladder create named accountability.
* Nothing is ever hard-deleted.
"""
from __future__ import annotations
import hashlib, datetime as dt, random
from sqlalchemy import select, func
from sqlalchemy.orm import Session as OrmSession
from .db import Grievance, AuditEvent, Officer, Feedback, now
from . import config


# ---------------- audit (append-only, hash-chained) ----------------
def _last_hash(db: OrmSession, gid: str) -> str | None:
    ev = db.execute(
        select(AuditEvent).where(AuditEvent.grievance_id == gid)
        .order_by(AuditEvent.id.desc())
    ).scalars().first()
    return ev.hash if ev else None


def audit(db: OrmSession, gid: str, event_type: str, actor_id: str, actor_role: str,
          from_val=None, to_val=None, note=None) -> AuditEvent:
    prev = _last_hash(db, gid)
    ts = now()  # pin one timestamp for both the stored row and the hash payload
    payload = f"{gid}|{event_type}|{from_val}|{to_val}|{note}|{actor_id}|{prev}|{ts.isoformat()}"
    h = hashlib.sha256(payload.encode()).hexdigest()
    ev = AuditEvent(grievance_id=gid, event_type=event_type, from_val=from_val,
                    to_val=to_val, note=note, actor_id=actor_id, actor_role=actor_role,
                    prev_hash=prev, hash=h, at=ts)
    db.add(ev)
    db.flush()  # make it visible to the next _last_hash() in this same transaction
    return ev


def verify_chain(db: OrmSession, gid: str) -> bool:
    prev = None
    for ev in db.execute(select(AuditEvent).where(AuditEvent.grievance_id == gid)
                         .order_by(AuditEvent.id)).scalars():
        if ev.prev_hash != prev:
            return False  # linkage broken
        payload = f"{gid}|{ev.event_type}|{ev.from_val}|{ev.to_val}|{ev.note}|{ev.actor_id}|{prev}|{ev.at.isoformat()}"
        if hashlib.sha256(payload.encode()).hexdigest() != ev.hash:
            return False  # content tampered
        prev = ev.hash
    return True


# ---------------- routing / sensitivity / SLA ----------------
def classify_sensitivity(text: str, hint: str = "normal") -> str:
    if hint == "sensitive":
        return "sensitive"
    tl = (text or "").lower()
    return "sensitive" if any(k.lower() in tl for k in config.SENSITIVE_KEYWORDS) else "normal"


def route(db: OrmSession, category: str, ward: str | None, sensitivity: str) -> tuple[str, str | None]:
    """Return (department, assigned_officer_id).

    Sensitive grievances are NEVER routed to the ordinary ward officer (who may be
    the subject); they go to the collector/cm_cell escalation tier instead.
    """
    dept = config.CATEGORIES.get(category, config.CATEGORIES["other"])["dept"]
    if sensitivity == "sensitive":
        officer = db.execute(select(Officer).where(Officer.role == "collector")).scalars().first()
        return dept, (officer.id if officer else None)
    officer = db.execute(
        select(Officer).where(Officer.role == "ward_officer", Officer.ward == ward)
    ).scalars().first()
    if not officer:
        officer = db.execute(
            select(Officer).where(Officer.role == "dept_head", Officer.department == dept)
        ).scalars().first()
    return dept, (officer.id if officer else None)


def sla_due(category: str, created: dt.datetime) -> dt.datetime:
    days = config.CATEGORIES.get(category, config.CATEGORIES["other"])["sla_days"]
    return created + dt.timedelta(days=days)


def escalate_overdue(db: OrmSession) -> list[str]:
    """Move breached, still-open grievances one step up the ladder. Returns ids moved."""
    moved = []
    open_states = ("registered", "assigned", "in_progress", "reopened", "escalated")
    for g in db.execute(select(Grievance).where(Grievance.status.in_(open_states))).scalars():
        if g.sla_due_at and g.sla_due_at < now() and g.escalation_level < len(config.ESCALATION_LADDER) - 1:
            frm = config.ESCALATION_LADDER[g.escalation_level]
            g.escalation_level += 1
            g.status = "escalated"
            to = config.ESCALATION_LADDER[g.escalation_level]
            audit(db, g.id, "status", actor_id="system", actor_role="system",
                  from_val=frm, to_val=to, note="SLA breached — auto-escalated")
            moved.append(g.id)
    db.commit()
    return moved


# ---------------- filing + dedup ----------------
def new_id() -> str:
    return f"TN-{config.CITY_CODE}-{dt.date.today().year}-{random.randint(4000, 9999)}"


def find_duplicate(db: OrmSession, category: str, ward: str | None) -> Grievance | None:
    if not ward or category == "other" or "not listed" in ward.lower():
        return None

    open_states = ("registered", "assigned", "in_progress", "reopened", "escalated")
    return db.execute(
        select(Grievance).where(
            Grievance.category == category, Grievance.ward == ward,
            Grievance.status.in_(open_states), Grievance.parent_grievance_id.is_(None),
        )
    ).scalars().first()



def file_grievance(db: OrmSession, *, reporter_phone, ui_language, category, ward, landmark,
                   description_raw, description_clean, severity, sensitivity, gps=None,
                   add_support_to: str | None = None) -> Grievance:
    # Co-report path: attach the citizen's voice to an existing open issue.
    if add_support_to:
        parent = db.get(Grievance, add_support_to)
        if parent:
            parent.reporter_count += 1
            audit(db, parent.id, "note", actor_id=reporter_phone[-4:], actor_role="citizen",
                  note="Additional citizen co-reported this issue")
            db.commit(); db.refresh(parent)
            return parent

    sensitivity = classify_sensitivity(description_raw, sensitivity)
    dept, officer_id = route(db, category, ward, sensitivity)
    created = now()
    g = Grievance(
        id=new_id(), reporter_phone=reporter_phone, ui_language=ui_language,
        category=category, ward=ward, landmark=landmark,
        description_raw=description_raw, description_clean=description_clean or description_raw,
        severity=severity or "medium", sensitivity=sensitivity,
        assigned_dept=dept, assigned_officer_id=officer_id,
        sla_due_at=sla_due(category, created), created_at=created,
        gps_lat=(gps or {}).get("lat"), gps_lng=(gps or {}).get("lng"),
        status="assigned" if officer_id else "registered",
    )
    db.add(g); db.flush()
    audit(db, g.id, "created", actor_id=reporter_phone[-4:], actor_role="citizen",
          to_val="registered", note=f"Routed to {dept}"
          + (" (sensitive → collector)" if sensitivity == "sensitive" else ""))
    if officer_id:
        audit(db, g.id, "assign", actor_id="system", actor_role="system",
              to_val=officer_id, note="Auto-assigned on intake")
    db.commit(); db.refresh(g)
    return g
