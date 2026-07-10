"""Route-level input validation: bad input must 4xx, never 500.

Imports app with the scheduler and Garmin login stubbed out, auth disabled,
and the DB pointed at an isolated in-memory SQLite.
"""
import pytest


@pytest.fixture
def client(monkeypatch):
    import config
    # Disable app auth so requests pass the cookie middleware.
    monkeypatch.setattr(config, "APP_USERNAME", "", raising=False)

    # Stub the startup side effects (scheduler thread + Garmin network login).
    import sync.scheduler as scheduler
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: None)
    import sync.garmin_client as gc
    monkeypatch.setattr(gc.client, "login", lambda *a, **k: False, raising=False)

    # Point the shared DB at an isolated in-memory database.
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    import db as db_module

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    monkeypatch.setattr(db_module, "engine", engine)
    monkeypatch.setattr(
        db_module, "SessionLocal",
        sessionmaker(bind=engine, expire_on_commit=False, future=True),
    )
    db_module.Base.metadata.create_all(engine)

    from fastapi.testclient import TestClient
    import app as app_module
    return TestClient(app_module.app), db_module


def test_calendar_invalid_month_returns_400(client):
    c, _ = client
    resp = c.get("/calendar?month=13&year=2026")
    assert resp.status_code == 400


def test_calendar_valid_month_ok(client):
    c, _ = client
    resp = c.get("/calendar?month=6&year=2026")
    assert resp.status_code == 200


def test_browser_chat_route_is_not_exposed(client):
    c, _ = client
    resp = c.get("/chat", follow_redirects=False)
    assert resp.status_code == 404


def test_set_non_numeric_reps_returns_400(client):
    c, db_module = client
    # Seed an activity + set to edit.
    from db import Activity, ExerciseSet
    from datetime import datetime
    with db_module.get_session() as s:
        s.add(Activity(id=7001, activity_type="strength_training", start_time=datetime.now()))
        s.flush()
        s.add(ExerciseSet(id=42, activity_id=7001, set_index=0,
                          exercise_category="BENCH_PRESS", reps=10, weight_kg=20.0))

    resp = c.post("/set/42", data={"reps": "abc", "weight_kg": ""}, follow_redirects=False)
    assert resp.status_code == 400


def test_set_valid_update_redirects(client):
    c, db_module = client
    from db import Activity, ExerciseSet
    from datetime import datetime
    with db_module.get_session() as s:
        s.add(Activity(id=7002, activity_type="strength_training", start_time=datetime.now()))
        s.flush()
        s.add(ExerciseSet(id=43, activity_id=7002, set_index=0,
                          exercise_category="SQUAT", reps=10, weight_kg=20.0))

    resp = c.post("/set/43", data={"reps": "12", "weight_kg": "25.5"}, follow_redirects=False)
    assert resp.status_code == 303


def test_safe_next_blocks_open_redirect(client):
    import app as app_module
    assert app_module._safe_next("https://evil.com") == "/"
    assert app_module._safe_next("//evil.com") == "/"
    assert app_module._safe_next("/dashboard") == "/dashboard"
    assert app_module._safe_next("") == "/"
    assert app_module._safe_next("/\\evil") == "/"


def test_manual_sync_forces_recent_fetch(client, monkeypatch):
    c, _ = client
    import app as app_module

    captured = {}
    monkeypatch.setattr(app_module.client, "is_authenticated", lambda: True)
    monkeypatch.setattr(
        app_module.sync_runner,
        "try_start_sync",
        lambda full, force=False: captured.update({"full": full, "force": force}) or True,
    )

    resp = c.post("/sync", follow_redirects=False)

    assert resp.status_code == 303
    assert captured == {"full": False, "force": True}


def test_onboarding_renders_history_defaults(client):
    c, db_module = client
    from datetime import datetime
    from db import Activity, Workout

    with db_module.get_session() as s:
        s.add(Activity(id=8101, activity_type="strength_training", start_time=datetime.now()))
        s.add(Activity(id=8102, activity_type="strength_training", start_time=datetime.now()))
        s.add(Activity(id=8103, activity_type="running", start_time=datetime.now()))
        s.add(Workout(workout_id=8104, name="Upper Strength", sport_type="strength_training", steps_json="[]"))

    resp = c.get("/onboarding")

    assert resp.status_code == 200
    assert "Workout history" in resp.text
    assert "Strength focused" in resp.text
    assert "Garmin templates" not in resp.text
    assert "Additional sessions" not in resp.text
    assert "Training days" not in resp.text
    assert "Upper Strength" not in resp.text


def test_goal_route_and_nav_removed(client):
    c, _ = client

    resp = c.get("/goal", follow_redirects=False)
    nav = c.get("/onboarding")

    assert resp.status_code == 404
    assert 'href="/goal"' not in nav.text


def test_onboarding_creates_reviewable_program_proposal(client):
    c, db_module = client
    import json
    from db import AthleteProfile, Goal, ProgramSession, TrainingProgram, Workout

    with db_module.get_session() as s:
        s.add(Workout(workout_id=9001, name="Upper Strength", sport_type="strength_training", steps_json="[]"))

    resp = c.post(
        "/onboarding",
        data={
            "training_type": "strength_focused",
            "experience_level": "intermediate",
            "primary_goal": "Build strength",
            "preferred_activities": ["Strength", "Running"],
            "equipment_access": ["gym", "outdoor"],
            "training_days": ["Monday", "Wednesday", "Friday"],
            "days_per_week": "3",
            "preferred_time_of_day": "evening",
            "injuries_limitations": "No heavy overhead press",
            "sport_commitments": "Soccer Saturday",
            "scheduling_options": ["calendar_aware", "recovery_based"],
            "scheduling_notes": "Avoid late nights",
            "program_name": "Strength routine",
            "plan_mode": "existing_templates",
            "selected_templates": ["9001"],
            "custom_sessions": "Mobility",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    with db_module.get_session() as s:
        profile = s.get(AthleteProfile, 1)
        assert profile.training_type == "strength_focused"
        assert json.loads(profile.preferred_activities) == ["Strength", "Running"]
        assert json.loads(profile.equipment_access) == ["gym", "outdoor"]
        assert "Monday" in profile.availability
        assert "calendar_aware" in profile.scheduling_preferences

        goal = s.get(Goal, 1)
        assert goal.goal == "Build strength"
        assert goal.custom_input == "No heavy overhead press"

        program = s.query(TrainingProgram).filter(TrainingProgram.status == "draft").one()
        assert program.name == "Strength to support your sport · 2 days"
        assert program.mode == "curated_strength"
        assert program.days_per_week == 3
        assert program.active is False
        assert "without assigning dates" in program.rationale

        sessions = s.query(ProgramSession).order_by(ProgramSession.sequence_order.asc()).all()
        assert [ps.name for ps in sessions] == ["Strength A", "Strength B"]
        assert all(ps.base_workout_id is None for ps in sessions)


def test_onboarding_proposal_is_reviewed_before_activation(client):
    c, db_module = client
    from datetime import datetime, timedelta
    from db import Activity, ProgramSession, TrainingProgram

    with db_module.get_session() as s:
        for idx, name in enumerate(["Upper Strength", "Lower Strength", "Upper Strength", "Lower Strength"]):
            s.add(Activity(
                id=9100 + idx,
                activity_type="strength_training",
                start_time=datetime.now() - timedelta(days=4 - idx),
                name=name,
            ))

    resp = c.post(
        "/onboarding",
        data={
            "training_type": "strength_focused",
            "primary_goal": "Build strength",
            "preferred_activities": ["Strength"],
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    with db_module.get_session() as s:
        program = s.query(TrainingProgram).filter(TrainingProgram.status == "draft").one()
        program_id = program.id
        assert s.query(ProgramSession).filter_by(program_id=program_id).count() == 2

    review = c.get(f"/program?proposal={program_id}")
    assert review.status_code == 200
    assert "Review your program" in review.text
    assert "Approve program" in review.text

    approved = c.post(f"/program/{program_id}/approve", follow_redirects=False)
    assert approved.status_code == 303
    with db_module.get_session() as s:
        program = s.get(TrainingProgram, program_id)
        assert program.active is True
        assert program.status == "active"
