"""FastAPI application: wires the routes and serves the two frontends."""
from __future__ import annotations
import datetime as dt
from pathlib import Path
from fastapi import FastAPI, HTTPException, Depends, Header
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from .db import SessionLocal, Grievance, AuditEvent, Officer, Feedback, init_db, now
from . import governance as gov
from . import intake as intake_engine
from .llm_gateway import POOL
from .schemas import IntakeMessageIn, ConfirmIn, OtpIn, OtpVerifyIn
from . import config

app = FastAPI(title="Nagar Kuraiteer — Civic Grievance Platform")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_OTP: dict[str, dict] = {}   # phone -> {code, verified}


def db_dep():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@app.on_event("startup")
def _startup():
    init_db()


# ---------------- health ----------------
@app.get("/health")
def health():
    return {"ok": True, "city": config.CITY_NAME, "llm_live": config.llm_is_live()}


@app.get("/health/llm")
def health_llm():
    return {"live": config.llm_is_live(), "pool": POOL.status()}


# ---------------- auth (mock OTP) ----------------
@app.post("/api/v1/auth/request-otp")
def request_otp(body: OtpIn):
    _OTP[body.phone] = {"code": config.MOCK_OTP, "verified": False}
    # In production this hands off to an SMS gateway. Mock returns the code for the demo.
    return {"sent": True, "demo_code": config.MOCK_OTP}


@app.post("/api/v1/auth/verify-otp")
def verify_otp(body: OtpVerifyIn):
    rec = _OTP.get(body.phone)
    if not rec or rec["code"] != body.code:
        raise HTTPException(400, "Invalid code")
    rec["verified"] = True
    return {"verified": True, "token": f"sess-{body.phone}"}


def _require_phone(phone: str):
    if not _OTP.get(phone, {}).get("verified"):
        raise HTTPException(401, "Phone not verified")


# ---------------- intake ----------------
@app.post("/api/v1/intake/message")
def intake_message(body: IntakeMessageIn):
    session = intake_engine.get_session(body.session_id, body.ui_language)
    return intake_engine.step(session, body.text)


# ---------------- file / track / reopen / feedback ----------------
@app.post("/api/v1/grievances")
def file_grievance(body: ConfirmIn, db: OrmSession = Depends(db_dep)):
    _require_phone(body.reporter_phone)
    dup = None
    if not body.add_support_to and not body.bypass_dedup:
        dup = gov.find_duplicate(db, body.category.value, body.ward)
        if dup:
            return {"duplicate": True, "existing_id": dup.id, "reporter_count": dup.reporter_count,
                    "message": "This issue is already registered in this ward."}

    g = gov.file_grievance(
        db, reporter_phone=body.reporter_phone, ui_language=body.ui_language,
        category=body.category.value, ward=body.ward, landmark=body.landmark,
        description_raw=body.description, description_clean=body.description,
        severity=body.severity.value, sensitivity="normal", gps=body.gps,
        add_support_to=body.add_support_to,
    )
    return _slip(g)


@app.get("/api/v1/grievances/{gid}")
def track(gid: str, db: OrmSession = Depends(db_dep)):
    g = db.get(Grievance, gid)
    if not g:
        raise HTTPException(404, "Not found")
    return _slip(g, with_history=True)


@app.post("/api/v1/grievances/{gid}/reopen")
def reopen(gid: str, db: OrmSession = Depends(db_dep)):
    g = db.get(Grievance, gid)
    if not g:
        raise HTTPException(404, "Not found")
    if g.status not in ("resolved", "closed"):
        raise HTTPException(400, "Only resolved grievances can be reopened")
    gov.audit(db, gid, "reopen", actor_id=g.reporter_phone[-4:], actor_role="citizen",
              from_val=g.status, to_val="reopened", note="Citizen not satisfied")
    g.status = "reopened"
    db.commit()
    return _slip(g, with_history=True)


@app.post("/api/v1/grievances/{gid}/feedback")
def feedback(gid: str, rating: int, comment: str = "", db: OrmSession = Depends(db_dep)):
    g = db.get(Grievance, gid)
    if not g:
        raise HTTPException(404, "Not found")
    db.add(Feedback(grievance_id=gid, rating=rating, comment=comment))
    db.commit()
    return {"ok": True}


# ---------------- admin (role-scoped) ----------------
def _role(x_role: str = Header(default="cm_cell")) -> str:
    return x_role


@app.get("/api/v1/admin/grievances")
def admin_list(status: str | None = None, ward: str | None = None, category: str | None = None,
               role: str = Depends(_role), db: OrmSession = Depends(db_dep)):
    q = select(Grievance).order_by(Grievance.created_at.desc())
    if status: q = q.where(Grievance.status == status)
    if ward: q = q.where(Grievance.ward == ward)
    if category: q = q.where(Grievance.category == category)
    rows = []
    for g in db.execute(q).scalars():
        # Sensitive grievances are masked from roles below collector.
        if g.sensitivity == "sensitive" and role not in ("collector", "cm_cell", "auditor"):
            continue
        rows.append(_row(g))
    return {"role": role, "grievances": rows}


@app.get("/api/v1/admin/grievances/{gid}")
def admin_detail(gid: str, role: str = Depends(_role), db: OrmSession = Depends(db_dep)):
    g = db.get(Grievance, gid)
    if not g:
        raise HTTPException(404, "Not found")
    if g.sensitivity == "sensitive" and role not in ("collector", "cm_cell", "auditor"):
        raise HTTPException(403, "Restricted — sensitive grievance")
    if g.sensitivity == "sensitive":
        gov.audit(db, gid, "sensitive_access", actor_id=role, actor_role=role,
                  note="Sensitive record viewed"); db.commit()
    return {**_row(g), "description_raw": g.description_raw,
            "history": [_ev(e) for e in g.events],
            "chain_valid": gov.verify_chain(db, gid)}


@app.patch("/api/v1/admin/grievances/{gid}")
def admin_update(gid: str, status: str | None = None, officer_id: str | None = None,
                 note: str | None = None, role: str = Depends(_role), actor: str = "OFF-DESK",
                 db: OrmSession = Depends(db_dep)):
    g = db.get(Grievance, gid)
    if not g:
        raise HTTPException(404, "Not found")
    if officer_id:
        gov.audit(db, gid, "assign", actor_id=actor, actor_role=role,
                  from_val=g.assigned_officer_id, to_val=officer_id, note=note)
        g.assigned_officer_id = officer_id
    if status:
        gov.audit(db, gid, "status", actor_id=actor, actor_role=role,
                  from_val=g.status, to_val=status, note=note)
        g.status = status
    if note and not status and not officer_id:
        gov.audit(db, gid, "note", actor_id=actor, actor_role=role, note=note)
    db.commit(); db.refresh(g)
    return _row(g)


@app.get("/api/v1/admin/oversight")
def oversight(role: str = Depends(_role), db: OrmSession = Depends(db_dep)):
    all_g = list(db.execute(select(Grievance)).scalars())
    open_states = ("registered", "assigned", "in_progress", "reopened", "escalated")
    breached = [g for g in all_g if g.sla_due_at and g.sla_due_at < now() and g.status in open_states]
    by_dept: dict[str, dict] = {}
    for g in all_g:
        d = by_dept.setdefault(g.assigned_dept, {"total": 0, "open": 0, "breached": 0})
        d["total"] += 1
        if g.status in open_states: d["open"] += 1
        if g in breached: d["breached"] += 1
    hotspots = [g for g in all_g if g.reporter_count > 1]
    return {
        "total": len(all_g),
        "open": sum(1 for g in all_g if g.status in open_states),
        "resolved": sum(1 for g in all_g if g.status in ("resolved", "closed")),
        "breached": len(breached),
        "sensitive": sum(1 for g in all_g if g.sensitivity == "sensitive"),
        "by_department": by_dept,
        "hotspots": [{"id": g.id, "category": g.category, "ward": g.ward,
                      "reporters": g.reporter_count} for g in hotspots],
    }


@app.post("/api/v1/admin/run-escalation")
def run_escalation(db: OrmSession = Depends(db_dep)):
    return {"escalated": gov.escalate_overdue(db)}


# ---------------- serializers ----------------
def _cat_label(cat: str) -> dict:
    c = config.CATEGORIES.get(cat, config.CATEGORIES["other"])
    return {"key": cat, "ta": c["ta"], "en": c["en"], "dept": c["dept"]}


def _slip(g: Grievance, with_history: bool = False) -> dict:
    d = {
        "id": g.id, "status": g.status, "category": _cat_label(g.category),
        "dept": g.assigned_dept, "ward": g.ward, "landmark": g.landmark,
        "severity": g.severity, "sensitivity": g.sensitivity,
        "sla_due_at": g.sla_due_at.isoformat() if g.sla_due_at else None,
        "reporter_count": g.reporter_count, "created_at": g.created_at.isoformat(),
        "ui_language": g.ui_language,
    }
    if with_history:
        d["history"] = [_ev(e) for e in g.events]
    return d


def _row(g: Grievance) -> dict:
    overdue = bool(g.sla_due_at and g.sla_due_at < now()
                   and g.status in ("registered", "assigned", "in_progress", "reopened", "escalated"))
    return {**_slip(g), "overdue": overdue, "escalation_level": g.escalation_level,
            "officer_id": g.assigned_officer_id, "description": g.description_clean}


def _ev(e: AuditEvent) -> dict:
    return {"type": e.event_type, "from": e.from_val, "to": e.to_val, "note": e.note,
            "actor": e.actor_id, "role": e.actor_role, "at": e.at.isoformat()}


# ---------------- static frontends ----------------
FRONT = Path(__file__).resolve().parents[2] / "frontend"
if FRONT.exists():
    app.mount("/app", StaticFiles(directory=str(FRONT), html=True), name="frontend")


@app.get("/")
def root():
    return RedirectResponse("/app/citizen.html")
