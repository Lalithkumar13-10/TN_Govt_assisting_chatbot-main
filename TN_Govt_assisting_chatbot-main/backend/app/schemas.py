"""Pydantic v2 schemas. IntakeResult is passed to Gemini as `response_format`
so extraction is schema-guaranteed, and is also the contract the intake engine
and API speak."""
from __future__ import annotations
from enum import Enum
from typing import Optional, Literal
from pydantic import BaseModel, Field


class Category(str, Enum):
    streetlight = "streetlight"
    garbage = "garbage"
    water_supply = "water_supply"
    drainage = "drainage"
    sewage = "sewage"
    road = "road"
    other = "other"


class Severity(str, Enum):
    low = "low"
    medium = "medium"
    high = "high"


SlotState = Literal["filled", "missing", "ambiguous"]


class Location(BaseModel):
    ward: Optional[str] = None
    landmark: Optional[str] = None
    address_text: Optional[str] = None


class Extraction(BaseModel):
    category: Optional[Category] = None
    sub_category: Optional[str] = None
    description_clean: Optional[str] = None
    location: Location = Field(default_factory=Location)
    severity: Optional[Severity] = None
    language_detected: Literal["ta", "ta-latin", "en", "mixed"] = "en"


class RequiredStatus(BaseModel):
    category: SlotState = "missing"
    location: SlotState = "missing"
    description: SlotState = "missing"


class IntakeResult(BaseModel):
    """Exact shape Gemini must return (response_format) and the engine consumes."""
    assistant_message: str
    extraction: Extraction = Field(default_factory=Extraction)
    required_status: RequiredStatus = Field(default_factory=RequiredStatus)
    sensitivity_hint: Literal["normal", "sensitive"] = "normal"
    action: Literal["ask", "ready_to_file"] = "ask"


# ---- API request/response models ----
class IntakeMessageIn(BaseModel):
    session_id: str
    ui_language: Literal["ta", "en"] = "en"
    text: str
    gps: Optional[dict] = None


class ConfirmIn(BaseModel):
    session_id: str
    ui_language: Literal["ta", "en"] = "en"
    reporter_phone: str
    category: Category
    ward: Optional[str] = None
    landmark: Optional[str] = None
    description: str
    severity: Severity = Severity.medium
    gps: Optional[dict] = None
    add_support_to: Optional[str] = None  # existing grievance id when co-reporting
    bypass_dedup: Optional[bool] = False



class OtpIn(BaseModel):
    phone: str


class OtpVerifyIn(BaseModel):
    phone: str
    code: str
