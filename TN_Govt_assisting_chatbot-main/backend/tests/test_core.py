"""Core guarantees, all with the LLM in mock mode (no network)."""
import os, sys, pathlib
os.environ["LLM_MODE"] = "mock"
os.environ["DB_URL"] = "sqlite:///./test_nk.db"
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import pytest
from app import intake as ie
from app.config import QUESTION_BUDGET


@pytest.fixture(autouse=True)
def clean_db():
    from app.db import Base, engine, init_db
    Base.metadata.drop_all(bind=engine)
    init_db()



def fresh(sid):
    ie._SESSIONS.pop(sid, None)
    return ie.get_session(sid, "en")


def test_asks_for_location_then_files():
    s = fresh("s1")
    r1 = ie.step(s, "street light not working")     # has category + desc, missing ward
    assert r1["mode"] == "ask"
    r2 = ie.step(s, "in Gandhipuram")               # supplies ward
    assert r2["mode"] == "confirm"
    assert r2["draft"]["category"] == "streetlight"
    assert "Gandhipuram" in r2["draft"]["ward"]


def test_complete_first_message_files_without_questions():
    s = fresh("s2")
    r = ie.step(s, "garbage not collected in R.S. Puram for days")
    assert r["mode"] == "confirm"          # nothing to ask — must not interrogate
    assert s.clarify_count == 0


def test_question_budget_is_hard_capped():
    s = fresh("s3")
    for _ in range(10):
        ie.step(s, "there is a problem")   # deliberately vague, never gives location
        if s.clarify_count >= QUESTION_BUDGET:
            break
    r = ie.step(s, "still vague")
    assert s.clarify_count <= QUESTION_BUDGET
    assert r["mode"] == "confirm"           # forced to confirm, never a 4th question


def test_severity_inferred_high_for_danger():
    s = fresh("s4")
    r = ie.step(s, "open manhole overflowing near Ukkadam, very dangerous")
    assert r["mode"] == "confirm"
    assert r["draft"]["severity"] == "high"


def test_sensitivity_flagged():
    s = fresh("s5")
    r = ie.step(s, "the water officer demanded money in Ukkadam to fix the line")
    assert r["draft"]["sensitivity"] == "sensitive"


def test_governance_filing_dedup_and_audit_chain():
    from app.db import SessionLocal, init_db
    from app import governance as gov
    init_db()
    db = SessionLocal()
    g = gov.file_grievance(db, reporter_phone="9111111111", ui_language="en",
                           category="streetlight", ward="Peelamedu (Ward 71)", landmark="x",
                           description_raw="light off", description_clean="light off",
                           severity="high", sensitivity="normal")
    assert g.id.startswith("TN-CBE-")
    assert gov.verify_chain(db, g.id) is True
    dup = gov.find_duplicate(db, "streetlight", "Peelamedu (Ward 71)")
    assert dup is not None and dup.id == g.id
    db.close()


def test_sensitive_never_routed_to_ward_officer():
    from app.db import SessionLocal, Officer, init_db
    from app import governance as gov
    init_db()
    db = SessionLocal()
    if not db.query(Officer).filter_by(role="collector").first():
        db.add(Officer(id="C1", name="Collector", role="collector")); db.commit()
    g = gov.file_grievance(db, reporter_phone="9222222222", ui_language="en",
                           category="water_supply", ward="Ukkadam (Ward 20)", landmark=None,
                           description_raw="engineer took a bribe", description_clean="bribe",
                           severity="high", sensitivity="sensitive")
    assert g.sensitivity == "sensitive"
    off = db.get(Officer, g.assigned_officer_id) if g.assigned_officer_id else None
    assert off is None or off.role == "collector"    # never a ward officer
    db.close()


def test_loop_prevention_and_address_extraction():
    s = fresh("s6")
    # Turn 1: complain
    r1 = ie.step(s, "garbage not collected")
    assert r1["mode"] == "ask"
    assert s.acc.category == "garbage"
    assert s.acc.description_clean == "garbage not collected"
    
    # Turn 2: answer with non-ward area name
    r2 = ie.step(s, "sathya nagar")
    assert r2["mode"] == "confirm"
    assert s.acc.category == "garbage"
    # Ensure description was NOT overwritten by the location!
    assert s.acc.description_clean == "garbage not collected"
    # Ensure address_text was extracted
    assert s.acc.address_text == "sathya nagar"
    assert r2["draft"]["landmark"] == "sathya nagar"



def test_other_category_never_triggers_duplicate():
    from app.db import SessionLocal
    from app import governance as gov
    db = SessionLocal()
    
    # File first grievance of category "other" in Gandhipuram
    g1 = gov.file_grievance(db, reporter_phone="9999999999", ui_language="en",
                            category="other", ward="Gandhipuram (Ward 42)", landmark="street 1",
                            description_raw="stray dogs", description_clean="stray dogs",
                            severity="medium", sensitivity="normal")
    assert g1.id is not None
    
    # Check if find_duplicate detects it
    dup = gov.find_duplicate(db, "other", "Gandhipuram (Ward 42)")
    assert dup is None  # Must be None because category is "other"!
    db.close()


def test_bypass_dedup_flag():
    from app.db import SessionLocal
    from app import governance as gov
    from fastapi.testclient import TestClient
    from app.main import app
    
    db = SessionLocal()
    # File first grievance of category "streetlight" in Gandhipuram
    g1 = gov.file_grievance(db, reporter_phone="9999999991", ui_language="en",
                            category="streetlight", ward="Gandhipuram (Ward 42)", landmark="street 1",
                            description_raw="light off", description_clean="light off",
                            severity="medium", sensitivity="normal")
    db.commit()
    
    # Verify standard find_duplicate finds it
    dup = gov.find_duplicate(db, "streetlight", "Gandhipuram (Ward 42)")
    assert dup is not None and dup.id == g1.id
    db.close()
    
    # Test using TestClient to call the API endpoint
    client = TestClient(app)
    
    # Request OTP to verify phone
    otp_resp = client.post("/api/v1/auth/request-otp", json={"phone": "9999999991"})
    assert otp_resp.status_code == 200
    
    verify_resp = client.post("/api/v1/auth/verify-otp", json={"phone": "9999999991", "code": "123456"})
    assert verify_resp.status_code == 200
    
    # Call without bypass_dedup: should return duplicate status
    payload = {
        "session_id": "sess-test",
        "ui_language": "en",
        "reporter_phone": "9999999991",
        "category": "streetlight",
        "ward": "Gandhipuram (Ward 42)",
        "landmark": "street 1",
        "description": "light off again",
        "severity": "medium"
    }
    resp1 = client.post("/api/v1/grievances", json=payload)
    assert resp1.status_code == 200
    assert resp1.json().get("duplicate") is True
    
    # Call WITH bypass_dedup: should succeed and return the new grievance slip
    payload["bypass_dedup"] = True
    resp2 = client.post("/api/v1/grievances", json=payload)
    assert resp2.status_code == 200
    assert resp2.json().get("duplicate") is not True
    assert resp2.json().get("id") is not None
    assert resp2.json().get("id") != g1.id


def test_other_or_not_listed_ward_never_triggers_duplicate():
    from app.db import SessionLocal
    from app import governance as gov
    db = SessionLocal()
    
    # File first grievance in 'Other / Not Listed'
    g1 = gov.file_grievance(db, reporter_phone="9999999992", ui_language="en",
                            category="streetlight", ward="Other / Not Listed", landmark="street 1",
                            description_raw="light off", description_clean="light off",
                            severity="medium", sensitivity="normal")
    assert g1.id is not None
    
    # Check if find_duplicate detects it
    dup = gov.find_duplicate(db, "streetlight", "Other / Not Listed")
    assert dup is None  # Must be None because ward contains "not listed"!
    db.close()



