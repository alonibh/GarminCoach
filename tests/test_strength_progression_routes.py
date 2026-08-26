"""Acceptance coverage for the server-rendered progression review flow."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import db
from coach.strength_progression import (
    AppearanceClassification, AppearanceClassificationResult, calculate_proposal,
    derive_streak, prescription_fingerprint,
)
from coach.strength_progression_integration import _prescription
from coach.strength_progression_store import append_evidence, create_or_replace_pending_proposal, evidence_record
from db import Activity, ProgramSession, SessionExercise, StrengthProgressionPolicy, TrainingProgram


@pytest.fixture
def client(monkeypatch):
    import config
    monkeypatch.setattr(config, "APP_USERNAME", "", raising=False)
    from control_db import User
    monkeypatch.setattr("app.resolve_web_session", lambda session, token: User(
        id="00000000-0000-0000-0000-000000000001", email="test@example.com",
        status="active", role="owner", onboarding_step="complete",
    ))
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool, future=True)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine, expire_on_commit=False, future=True))
    import tenant_store
    monkeypatch.setattr(tenant_store, "engine_for_user", lambda uid, root=None: engine)
    db.Base.metadata.create_all(engine)
    import app as app_module
    yield TestClient(app_module.app), db
    engine.dispose()


def _seed(db_module):
    session = db_module.SessionLocal()
    try:
        policy = StrengthProgressionPolicy(policy_version="route-v1", global_increment_grams=2500,
            weight_quantum_grams=250, required_consecutive=2, evidence_window_days=35, is_active=True)
        program = TrainingProgram(name="Active", active=True, status="active")
        session.add_all((policy, program)); session.flush()
        program_session = ProgramSession(program_id=program.id, name="Push")
        session.add(program_session); session.flush()
        exercise = SessionExercise(program_session_id=program_session.id, exercise_name="Bench", exercise_key="BENCH",
            garmin_category="BENCH", garmin_name="BENCH", sets=3, reps=10, weight_kg=70.0)
        session.add(exercise); session.flush()
        prescription = _prescription(exercise, program.id, program_session.id)
        now = datetime(2026, 7, 31, 12)
        payload = (
            {"set_index": 0, "set_type": "REST", "reps": None, "weight_kg_source": None,
             "weight_grams": None, "duration_seconds": 60, "edited": False, "excluded": "rest"},
            {"set_index": 1, "set_type": "WORK", "reps": 10, "weight_kg_source": "70.0",
             "weight_grams": 70000, "duration_seconds": None, "edited": True},
        )
        result = AppearanceClassificationResult(AppearanceClassification.INCREASE_QUALIFIED, 70000, 72500, payload, ())
        evidence = []
        for index in range(2):
            activity = Activity(id=9000 + index)
            session.add(activity); session.flush()
            evidence.append(append_evidence(session, session_exercise_id=exercise.id, activity_id=activity.id,
                policy_version=policy.policy_version, prescription_fingerprint=prescription_fingerprint(prescription),
                source_fingerprint=f"route-{index}", appearance_at=now - timedelta(days=2-index), result=result,
                program_id=program.id, program_session_id=program_session.id, prescribed_sets=3, target_reps=10))
        records = [evidence_record(row) for row in evidence]
        streak = derive_streak(policy, records, session_exercise_id=exercise.id,
            prescription=prescription_fingerprint(prescription), as_of=now)
        proposal = create_or_replace_pending_proposal(session, session_exercise_id=exercise.id, program_id=program.id,
            program_session_id=program_session.id, proposal=calculate_proposal(policy, prescription, streak, records))
        session.commit()
        return proposal
    finally:
        session.close()


def test_progression_page_renders_typed_evidence_and_navigation(client):
    browser, db_module = client
    _seed(db_module)
    response = browser.get("/progression")
    assert response.status_code == 200
    assert 'href="/progression"' in response.text
    assert "70.0 kg" in response.text and "70 kg" in response.text
    assert "Manual correction" in response.text and "Rest — excluded" in response.text
    assert "Approve all" not in response.text and "rollback" not in response.text.lower()


def test_progression_actions_use_prg_and_safe_errors(client):
    browser, db_module = client
    proposal = _seed(db_module)
    invalid = browser.post(f"/progression/{proposal.proposal_id}/approve", data={"approved_weight_kg": "70"}, follow_redirects=False)
    assert invalid.status_code == 422
    approved = browser.post(f"/progression/{proposal.proposal_id}/approve", data={"approved_weight_kg": "72.5"}, follow_redirects=False)
    assert approved.status_code == 303 and approved.headers["location"] == "/progression?result=applied"
    assert browser.post(f"/progression/{proposal.proposal_id}/reject", follow_redirects=False).status_code == 409
    assert browser.post("/progression/unknown/reject", follow_redirects=False).status_code == 404
