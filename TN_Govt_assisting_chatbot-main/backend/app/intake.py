"""Conversational intake state machine.

Two independent layers guarantee 'ask only when needed, never nag':
  Layer 1 (model): the prompt tells Gemini to ask one required item at a time and
                   stop once all three are known.
  Layer 2 (here) : a hard QUESTION_BUDGET and a server-side merge that never lets
                   the model loop — once the budget is hit we force the confirm step,
                   pushing any remaining uncertainty into the editable card.
"""
from __future__ import annotations
import json
from dataclasses import dataclass, field, asdict
from .config import QUESTION_BUDGET
from .schemas import IntakeResult, Extraction
from .prompts import INTAKE_SYSTEM_PROMPT
from .llm_gateway import POOL, GatewayExhausted


@dataclass
class Accum:
    category: str | None = None
    sub_category: str | None = None
    description_clean: str | None = None
    ward: str | None = None
    landmark: str | None = None
    address_text: str | None = None
    severity: str | None = None
    language_detected: str = "en"
    sensitivity: str = "normal"


@dataclass
class Session:
    id: str
    ui_language: str = "en"
    acc: Accum = field(default_factory=Accum)
    clarify_count: int = 0
    transcript: list[dict] = field(default_factory=list)


_SESSIONS: dict[str, Session] = {}


def get_session(sid: str, ui_language: str = "en") -> Session:
    s = _SESSIONS.get(sid)
    if not s:
        s = Session(id=sid, ui_language=ui_language)
        _SESSIONS[sid] = s
    s.ui_language = ui_language
    return s


def _merge(acc: Accum, ext: Extraction) -> None:
    if ext.category: acc.category = ext.category.value
    if ext.sub_category: acc.sub_category = ext.sub_category
    if ext.description_clean: acc.description_clean = ext.description_clean
    if ext.severity: acc.severity = ext.severity.value
    if ext.language_detected: acc.language_detected = ext.language_detected
    if ext.location.ward: acc.ward = ext.location.ward
    if ext.location.landmark: acc.landmark = ext.location.landmark
    if ext.location.address_text: acc.address_text = ext.location.address_text


def _known_json(acc: Accum) -> str:
    return json.dumps({
        "category": acc.category,
        "description_clean": acc.description_clean,
        "ward": acc.ward, "landmark": acc.landmark,
        "address_text": acc.address_text,
        "severity": acc.severity,
    }, ensure_ascii=False)


def build_user_content(acc: Accum, latest: str) -> str:
    # The mock and the real model both receive: known-so-far + latest citizen line.
    return f'KNOWN: {_known_json(acc)}\n---\n{latest}'


def _required_complete(acc: Accum) -> bool:
    return bool(acc.category and (acc.ward or acc.landmark or acc.address_text)
                and acc.description_clean)


def step(session: Session, text: str) -> dict:
    """Process one citizen turn. Returns a dict the API/frontend renders."""
    session.transcript.append({"role": "user", "text": text})
    system = INTAKE_SYSTEM_PROMPT.format(city="Coimbatore", ui_language=session.ui_language)
    user = build_user_content(session.acc, text)

    try:
        result: IntakeResult = POOL.generate(group="intake", system=system, user=user)
    except GatewayExhausted:
        # Degrade gracefully — never turn a citizen away.
        return _confirm_payload(session, forced=True,
                                note="high_load")

    _merge(session.acc, result.extraction)
    if result.sensitivity_hint == "sensitive":
        session.acc.sensitivity = "sensitive"

    budget_hit = session.clarify_count >= QUESTION_BUDGET
    ready = result.action == "ready_to_file" or _required_complete(session.acc)

    if ready or budget_hit:
        return _confirm_payload(session, forced=budget_hit and not ready)

    session.clarify_count += 1
    session.transcript.append({"role": "bot", "text": result.assistant_message})
    return {
        "mode": "ask",
        "assistant_message": result.assistant_message,
        "clarify_count": session.clarify_count,
        "budget": QUESTION_BUDGET,
    }


def _confirm_payload(session: Session, forced: bool, note: str = "") -> dict:
    a = session.acc
    return {
        "mode": "confirm",
        "assistant_message": ("நன்றி. உங்கள் புகாரின் விவரங்கள் கீழே தயார் செய்யப்பட்டுள்ளன. தயவுசெய்து அவற்றைச் சரிபார்த்து சமர்ப்பிக்கவும்."
                              if session.ui_language == "ta"
                              else "Thank you. The details of your grievance have been drafted below. Please review and submit."),
        "draft": {
            "category": a.category, "sub_category": a.sub_category,
            "description": a.description_clean, "ward": a.ward,
            "landmark": a.landmark or a.address_text, "severity": a.severity or "medium",
            "sensitivity": a.sensitivity, "language_detected": a.language_detected,
        },
        "forced": forced, "note": note,
    }
