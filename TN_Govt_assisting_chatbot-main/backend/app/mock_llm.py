"""Deterministic stand-in for the Gemini intake call.

Runs when no GEMINI_API_KEY is set (and in tests). It mimics the real model's
behaviour closely enough to demo and test the full state machine: keyword-based
category + severity + sensitivity detection, simple location parsing, and the
same 'ask one thing / ready_to_file' decision logic the prompt asks Gemini for.
"""
from __future__ import annotations
import re
from .schemas import IntakeResult, Extraction, Location, RequiredStatus, Category, Severity
from . import config

_CATEGORY_HINTS = {
    "streetlight": ["street light", "streetlight", "தெருவிளக்கு", "lamp", "light", "eriyala", "எரியல", "விளக்கு"],
    "garbage":     ["garbage", "trash", "waste", "குப்பை", "கழிவு", "kuppai", "rubbish", "bin"],
    "water_supply":["water", "தண்ணீ", "தண்ணி", "குடிநீர்", "thanni", "supply", "no water", "tap"],
    "drainage":    ["drain", "storm water", "வடிகால்", "flood", "water logging", "மழைநீர்"],
    "sewage":      ["sewage", "sewer", "manhole", "சாக்கடை", "கழிவுநீர்", "overflow", "மேன்ஹோல்"],
    "road":        ["road", "pothole", "pit", "சாலை", "பள்ளம்", "pallam", "rasta", "ரோடு"],
}
_HIGH = ["manhole", "மேன்ஹோல்", "live wire", "current", "மின்சாரம்", "dark", "இருட்ட", "ratri", "ராத்திரி",
         "flood", "overflow", "danger", "ஆபத்து", "shock", "spark", "night"]


def _detect_category(t: str):
    tl = t.lower()
    for cat, hints in _CATEGORY_HINTS.items():
        if any(h in tl for h in hints):
            return Category(cat)
    return None


def _detect_ward(t: str):
    tl = t.lower()
    for w in config.WARDS:
        area = w.split(" (")[0].lower()
        if area in tl:
            return w
    return None


def _detect_lang(t: str):
    has_tamil = bool(re.search(r"[\u0B80-\u0BFF]", t))
    has_latin = bool(re.search(r"[a-zA-Z]", t))
    if has_tamil and has_latin:
        return "mixed"
    if has_tamil:
        return "ta"
    return "en"


def _sensitive(t: str) -> bool:
    tl = t.lower()
    return any(k.lower() in tl for k in config.SENSITIVE_KEYWORDS)


# very small in-memory notion of "what we already know" is handled by the engine;
# the mock only reads the LATEST message plus a compact "known so far" prefix that
# the engine prepends (see intake.build_user_content).
def mock_intake(user_content: str) -> IntakeResult:
    # The engine passes: "KNOWN: <json>\n---\n<latest citizen text>"
    latest = user_content.split("---", 1)[-1].strip()
    known = user_content.split("---", 1)[0]

    cat = _detect_category(user_content)
    ward = _detect_ward(user_content)
    lang = _detect_lang(latest)
    sev = Severity.high if any(h in user_content.lower() for h in _HIGH) else Severity.medium

    # Extract state from known
    already_have_desc = _has_value(known, "description_clean")
    already_have_loc = _has_value(known, "ward") or _has_value(known, "landmark") or _has_value(known, "address_text")
    already_have_cat = _has_value(known, "category")

    # description: use latest non-trivial citizen text ONLY if we don't have one yet
    desc = None
    if not already_have_desc:
        desc = latest if len(latest) > 8 else None

    # address_text: if we don't have location yet and already have a description,
    # then the user is responding to the location question.
    address_text = None
    if not already_have_loc and not ward and already_have_desc:
        lower_latest = latest.lower()
        is_refusal = any(w in lower_latest for w in ["don't know", "dont know", "no idea", "i don't", "idk", "teriyathu", "தெரியாது"])
        is_category_hint = any(h in lower_latest for hints in _CATEGORY_HINTS.values() for h in hints)
        if not is_refusal and not is_category_hint:
            address_text = latest

    have_cat = cat is not None or already_have_cat
    have_loc = ward is not None or already_have_loc or address_text is not None
    have_desc = desc is not None or already_have_desc

    rs = RequiredStatus(
        category="filled" if have_cat else "missing",
        location="filled" if have_loc else "missing",
        description="filled" if have_desc else "missing",
    )

    # Decide the single next question (location first), else ready.
    if not have_loc:
        msg, action = ("இது எந்தப் பகுதி அல்லது வார்டில்?" if lang == "ta"
                       else "Which area or ward is this in?"), "ask"
    elif not have_cat:
        msg, action = ("என்ன பிரச்சினை என்று சொல்ல முடியுமா?" if lang == "ta"
                       else "What is the problem exactly?"), "ask"
    elif not have_desc:
        msg, action = ("சிறிது விவரம் சொல்லுங்கள்." if lang == "ta"
                       else "Could you describe it briefly?"), "ask"
    else:
        msg, action = ("நன்றி. உங்கள் புகாரின் விவரங்கள் கீழே தயார் செய்யப்பட்டுள்ளன. தயவுசெய்து அவற்றைச் சரிபார்த்து சமர்ப்பிக்கவும்." if lang == "ta"
                       else "Thank you. The details of your grievance have been drafted below. Please review and submit."), "ready_to_file"

    return IntakeResult(
        assistant_message=msg,
        extraction=Extraction(
            category=cat, description_clean=desc,
            location=Location(ward=ward, address_text=address_text),
            severity=sev, language_detected=lang,
        ),
        required_status=rs,
        sensitivity_hint="sensitive" if _sensitive(user_content) else "normal",
        action=action,
    )


def _has_value(blob: str, key: str) -> bool:
    m = re.search(rf'"{key}"\s*:\s*("([^"]+)"|[^,\}}]+)', blob)
    if not m:
        return False
    val = m.group(1).strip().strip('"')
    return val not in ("", "null", "None")
