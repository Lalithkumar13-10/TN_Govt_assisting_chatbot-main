"""Central configuration: settings, the pilot-city directory, and the Gemini pool.

Everything a deployment would change lives here or in env vars — no city data or
model limits are hardcoded inside business logic.
"""
from __future__ import annotations
import os
from dataclasses import dataclass, field
from dotenv import load_dotenv

load_dotenv()


# ----------------------------------------------------------------------------
# Runtime settings (env-overridable)
# ----------------------------------------------------------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
# When no key is present we run in deterministic MOCK mode so the app is fully
# demoable without credentials. Set a real key to use the live Gemini pool.
LLM_MODE = os.getenv("LLM_MODE", "auto")  # auto | mock | live
DB_URL = os.getenv("DB_URL", "sqlite:///./nagar_kuraiteer.db")
QUESTION_BUDGET = int(os.getenv("QUESTION_BUDGET", "3"))
MOCK_OTP = os.getenv("MOCK_OTP", "123456")
CITY_NAME = os.getenv("CITY_NAME", "Coimbatore")
CITY_CODE = os.getenv("CITY_CODE", "CBE")


def llm_is_live() -> bool:
    if LLM_MODE == "mock":
        return False
    if LLM_MODE == "live":
        return True
    return bool(GEMINI_API_KEY)  # auto


# ----------------------------------------------------------------------------
# Gemini pool — real limits read off the project's AI Studio rate-limit page.
# rpm / tpm are per-minute; rpd is per-day. order = priority within a group.
# ----------------------------------------------------------------------------
@dataclass
class ModelCfg:
    name: str          # litellm model string (gemini/<id>)
    group: str         # "intake" (everyday) or "reason" (hard/sensitive)
    order: int         # lower = tried first
    rpm: int
    tpm: int
    rpd: int


GEMINI_POOL: list[ModelCfg] = [
    # Workhorse: highest daily budget by far — carries ordinary intake traffic.
    ModelCfg("gemini/gemini-3.1-flash-lite", "intake", 1, rpm=15, tpm=250_000, rpd=500),
    ModelCfg("gemini/gemini-2.5-flash-lite", "intake", 2, rpm=10, tpm=250_000, rpd=20),
    ModelCfg("gemini/gemini-2.5-flash",      "intake", 3, rpm=5,  tpm=250_000, rpd=20),
    # Reason tier: strongest models, used only for hard / sensitive turns.
    # Small daily budget is fine because these calls are rare.
    ModelCfg("gemini/gemini-3.5-flash",      "reason", 1, rpm=5,  tpm=250_000, rpd=20),
    ModelCfg("gemini/gemini-3-flash",        "reason", 2, rpm=5,  tpm=250_000, rpd=20),
]

# ----------------------------------------------------------------------------
# Pilot-city directory: wards, category -> department + SLA, escalation ladder.
# ----------------------------------------------------------------------------
CATEGORIES = {
    "streetlight":  {"ta": "தெருவிளக்கு",        "en": "Streetlight",        "dept": "Street Lighting Wing",       "sla_days": 3},
    "garbage":      {"ta": "குப்பை / கழிவு",     "en": "Garbage / Solid Waste","dept": "Solid Waste Management",     "sla_days": 2},
    "water_supply": {"ta": "குடிநீர்",           "en": "Water Supply",        "dept": "Water Supply Wing",          "sla_days": 2},
    "drainage":     {"ta": "மழைநீர் வடிகால்",    "en": "Storm Water Drain",   "dept": "Storm Water Drain Wing",     "sla_days": 4},
    "sewage":       {"ta": "கழிவுநீர் / சாக்கடை","en": "Sewage",              "dept": "Underground Sewerage Wing",  "sla_days": 2},
    "road":         {"ta": "சாலை / பள்ளம்",      "en": "Road / Pothole",      "dept": "Roads Wing",                 "sla_days": 7},
    "other":        {"ta": "மற்றவை",             "en": "Other",               "dept": "Public Grievance Cell",      "sla_days": 5},
}

WARDS = [
    "Gandhipuram (Ward 42)",
    "R.S. Puram (Ward 55)",
    "Peelamedu (Ward 71)",
    "Saibaba Colony (Ward 49)",
    "Singanallur (Ward 89)",
    "Ukkadam (Ward 20)",
    # Coimbatore zone-wise wards
    "Thudiyalur (Wards 1, 2)",
    "Saravanampatti (Wards 3, 4)",
    "Keeranatham (Ward 4)",
    "Chinnavedampatti (Ward 27)",
    "Ganapathy (Wards 28, 29)",
    "Sivanandha Colony (Ward 40)",
    "Saibaba Colony (Wards 42, 43)",
    "R.S. Puram (Wards 46, 47)",
    "Town Hall (Ward 80)",
    "Ukkadam (Ward 81)",
    "Selvapuram (Ward 82)",
    "Race Course (Ward 83)",
    "Ramanathapuram (Ward 67)",
    "Singanallur (Wards 63, 64)",
    "Peelamedu (Wards 56, 57, 58)",
    "Hope College (Ward 58)",
    "Civil Aerodrome (Ward 59)",
    "Avinashi Road (East Stretch - Wards 60, 61, 62)",
    "Ondipudur (Ward 69)",
    "Podanur (Wards 92, 93)",
    "Sundarapuram (Ward 95)",
    "Kuniyamuthur (Wards 96, 97)",
    "Madukkarai (Ward 100)",
    "Other / Not Listed"
]


# Escalation ladder — each level is a role that owns the grievance if the
# previous level breaches SLA. Roles map to Officer.role in the DB.
ESCALATION_LADDER = ["ward_officer", "dept_head", "commissioner", "collector", "cm_cell"]

# Topics that make a grievance sensitive -> restricted visibility + special routing.
SENSITIVE_KEYWORDS = [
    # English / Tanglish
    "bribe", "corrupt", "corruption", "harass", "molest", "assault", "threat",
    "safety", "unsafe", "women", "girl", "caste", "official took", "officer took",
    "engineer took", "demanded money", "money for", "kickback",
    # Tamil
    "லஞ்சம்", "ஊழல்", "மிரட்டல்", "தாக்குதல்", "பாதுகாப்பு", "பெண்", "சாதி", "பணம் கேட்டார்",
]
