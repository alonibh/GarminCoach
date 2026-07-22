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
    monkeypatch.setattr(gc.client, "is_authenticated", lambda: True, raising=False)

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


def test_manual_sync_json_response_stays_on_dashboard(client, monkeypatch):
    c, _ = client
    import app as app_module

    captured = {}
    monkeypatch.setattr(app_module.client, "is_authenticated", lambda: True)
    monkeypatch.setattr(
        app_module.sync_runner,
        "try_start_sync",
        lambda full, force=False: captured.update({"full": full, "force": force}) or True,
    )
    monkeypatch.setattr(app_module.sync_runner, "is_running", lambda: True)

    resp = c.post("/sync", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json() == {"started": True, "running": True}
    assert captured == {"full": False, "force": True}


def test_sync_status_returns_fresh_chart_data_when_idle(client, monkeypatch):
    c, _ = client
    import app as app_module

    monkeypatch.setattr(app_module.sync_runner, "is_running", lambda: False)
    monkeypatch.setattr(
        app_module,
        "_dashboard_chart_data",
        lambda session: {
            "health_series": [{"day": "2026-07-22", "steps": 1234}],
            "sleep_series": [{"day": "2026-07-22", "hours": 7.5}],
        },
    )

    resp = c.get("/sync/status")

    assert resp.status_code == 200
    assert resp.json()["running"] is False
    assert resp.json()["health_series"][0]["steps"] == 1234
    assert resp.json()["sleep_series"][0]["hours"] == 7.5


def test_dashboard_sync_updates_charts_without_page_reload(client):
    c, db_module = client
    from db import AthleteProfile

    with db_module.get_session() as s:
        s.add(AthleteProfile(id=1, onboarding_complete=True))

    response = c.get("/")

    assert response.status_code == 200
    assert "location.reload()" not in response.text
    assert "fetch('/sync'" in response.text
    assert "chart.update('none')" in response.text
    assert 'id="sync-form"' in response.text
    assert ".dashboard-actions [hidden] { display: none !important; }" in response.text


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
    assert 'How many days a week do you plan to work out?' in resp.text
    assert 'data-days-filter="2"' in resp.text
    assert 'data-plan-days="2"' in resp.text
    assert '/static/onboarding.js?v=' in resp.text
    for badge_label in ("Goal", "Type", "Level", "Days", "Time"):
        assert badge_label in resp.text
    assert 'class="routine-source-details"' in resp.text
    assert 'Source details</summary>' not in resp.text
    assert 'routine-detail-fact beginner' in resp.text
    assert 'routine-detail-fact intermediate' in resp.text
    assert 'routine-detail-fact advanced' in resp.text
    assert "Recent training context · last 90 days" in resp.text
    assert "Recent activity mix · last 90 days" in resp.text
    assert "Strength focused" in resp.text
    assert "Garmin templates" not in resp.text
    assert "Additional sessions" not in resp.text
    assert "Training days" not in resp.text
    assert "Activity anchors" not in resp.text
    assert "equipment_access" not in resp.text
    assert "Maximum gym-session length" not in resp.text
    assert "When can we suggest a gym workout?" not in resp.text
    assert "What are we working toward?" not in resp.text
    assert "Anything more specific?" not in resp.text
    assert "How many gym sessions can you" not in resp.text
    assert "How long have you trained consistently" not in resp.text
    assert "max 45 minutes" in resp.text
    assert "Full Body · 2 days" in resp.text
    assert "Beginner Full Body · 3 days" in resp.text
    assert "Upper / Lower Bodybuilding · 4 days" in resp.text
    assert "Push / Pull / Legs A/B · 6 days" in resp.text
    assert "Upper Strength" not in resp.text


def test_goal_route_and_setup_nav_removed(client):
    c, _ = client

    resp = c.get("/goal", follow_redirects=False)
    nav = c.get("/onboarding")

    assert resp.status_code == 404
    assert 'href="/goal"' not in nav.text
    nav_html = nav.text.split("<nav", 1)[1].split("</nav>", 1)[0]
    assert ">Setup<" not in nav_html
    assert 'href="/program"' in nav_html


def test_garmin_login_starts_sync_and_redirects_to_onboarding(client, monkeypatch):
    c, _ = client
    import app as app_module

    started = {}
    monkeypatch.setattr(app_module.client, "login", lambda **kwargs: None)
    monkeypatch.setattr(
        app_module.sync_runner,
        "try_start_sync",
        lambda full, force=False: started.update({"full": full, "force": force}) or True,
    )

    response = c.post("/login", data={"password": "temporary", "mfa": ""}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/onboarding"
    assert started == {"full": True, "force": False}


def test_dashboard_routes_new_user_through_connection_and_onboarding(client, monkeypatch):
    c, _ = client
    import app as app_module

    monkeypatch.setattr(app_module.client, "is_authenticated", lambda: False)
    disconnected = c.get("/", follow_redirects=False)
    assert disconnected.status_code == 303
    assert disconnected.headers["location"] == "/login"

    onboarding = c.get("/onboarding")
    assert "Connect Garmin" in onboarding.text

    monkeypatch.setattr(app_module.client, "is_authenticated", lambda: True)
    connected = c.get("/", follow_redirects=False)
    assert connected.status_code == 303
    assert connected.headers["location"] == "/onboarding"


def test_onboarding_creates_reviewable_program_proposal(client):
    c, db_module = client
    import json
    from db import AthleteProfile, Goal, ProgramSession, TrainingProgram, Workout

    with db_module.get_session() as s:
        s.add(Workout(workout_id=9001, name="Upper Strength", sport_type="strength_training", steps_json="[]"))

    resp = c.post(
        "/onboarding",
        data={
            "plan_key": "ppl_6",
            "injuries_limitations": "No heavy overhead press; max 45 minutes; never suggest after 21:00",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    with db_module.get_session() as s:
        profile = s.get(AthleteProfile, 1)
        assert profile.training_type == "strength_focused"
        assert profile.goal_detail == ""
        assert profile.timing_preferences == ""
        assert profile.availability == ""
        assert json.loads(profile.equipment_access) == ["gym"]

        goal = s.get(Goal, 1)
        assert goal.goal == ""
        assert goal.custom_input == "No heavy overhead press; max 45 minutes; never suggest after 21:00"

        program = s.query(TrainingProgram).filter(TrainingProgram.status == "draft").one()
        assert program.name == "Push / Pull / Legs A/B · 6 days"
        assert program.mode == "curated_strength"
        assert program.days_per_week == 6
        assert program.active is False
        assert "does not assign dates or upload" in program.rationale

        sessions = s.query(ProgramSession).order_by(ProgramSession.sequence_order.asc()).all()
        assert [ps.name for ps in sessions] == ["Push A", "Pull A", "Legs A", "Push B", "Pull B", "Legs B"]
        assert all(ps.session_role == "coach_strength" for ps in sessions)
        assert all(ps.base_workout_id is None for ps in sessions)

    setup = c.get("/onboarding")
    assert 'value="ppl_6" checked' in setup.text
    assert "No heavy overhead press; max 45 minutes; never suggest after 21:00" in setup.text


def test_existing_total_package_legacy_defaults_migrate_once(client):
    _, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram

    with db_module.get_session() as s:
        program = TrainingProgram(
            name="Total Package",
            goal_tags='["total_package_3"]',
            status="draft",
        )
        s.add(program)
        s.flush()
        routine = ProgramSession(program_id=program.id, name="Day 1")
        s.add(routine)
        s.flush()
        exercise = SessionExercise(
            program_session_id=routine.id,
            exercise_name="Squat",
            rest_seconds=90,
        )
        s.add(exercise)
        s.flush()
        exercise_id = exercise.id

    db_module._migrate_add_columns()
    with db_module.get_session() as s:
        assert s.get(SessionExercise, exercise_id).rest_seconds == 180
        s.get(SessionExercise, exercise_id).rest_seconds = 91

    db_module._migrate_add_columns()
    with db_module.get_session() as s:
        assert s.get(SessionExercise, exercise_id).rest_seconds == 91


def test_existing_source_template_rest_defaults_are_migrated_without_touching_custom_values(client):
    _, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram

    fixtures = [
        ("beginner_full_body_3", "Full Body 1", "Trap Bar Deadlift", 180, 300),
        ("ms_full_body_3", "Full Body 2", "Romanian Deadlift", 120, 90),
        ("upper_lower_4", "Upper A", "Bench Press", 60, 90),
        ("shul_4", "Lower Strength", "Front Squat", 180, 300),
        ("shul_4", "Lower Strength", "Hack Squat", 90, 120),
        ("shul_4", "Lower Hypertrophy", "Leg Extension", 60, 45),
        ("muscle_strength_5", "Upper Strength", "Bent Over Barbell Row", 120, 180),
        ("muscle_strength_5", "Legs Size", "Leg Press", 60, 90),
    ]
    exercise_ids = []
    with db_module.get_session() as s:
        for index, (program_key, session_name, exercise_name, old_rest, _) in enumerate(fixtures):
            program = TrainingProgram(
                name=f"Source program {index}",
                goal_tags=f'["{program_key}"]',
                status="draft",
            )
            s.add(program)
            s.flush()
            routine = ProgramSession(program_id=program.id, name=session_name)
            s.add(routine)
            s.flush()
            exercise = SessionExercise(
                program_session_id=routine.id,
                exercise_name=exercise_name,
                rest_seconds=old_rest,
            )
            s.add(exercise)
            s.flush()
            exercise_ids.append(exercise.id)

        custom_program = TrainingProgram(
            name="Customized source program",
            goal_tags='["total_package_3"]',
            status="draft",
        )
        s.add(custom_program)
        s.flush()
        custom_session = ProgramSession(program_id=custom_program.id, name="Day 1")
        s.add(custom_session)
        s.flush()
        custom_exercise = SessionExercise(
            program_session_id=custom_session.id,
            exercise_name="Squat",
            rest_seconds=61,
        )
        s.add(custom_exercise)
        s.flush()
        custom_exercise_id = custom_exercise.id

    db_module._migrate_add_columns()
    with db_module.get_session() as s:
        assert [s.get(SessionExercise, exercise_id).rest_seconds for exercise_id in exercise_ids] == [
            expected for *_, expected in fixtures
        ]
        assert s.get(SessionExercise, custom_exercise_id).rest_seconds == 61


def test_existing_total_package_sessions_receive_full_body_names(client):
    _, db_module = client
    from db import ProgramSession, TrainingProgram

    with db_module.get_session() as s:
        total_package = TrainingProgram(
            name="Total Package · 3 days",
            goal_tags='["total_package_3"]',
            status="active",
        )
        unrelated = TrainingProgram(
            name="Custom routine",
            goal_tags='["custom"]',
            status="active",
        )
        s.add_all([total_package, unrelated])
        s.flush()
        s.add_all([
            ProgramSession(program_id=total_package.id, name="Day 1"),
            ProgramSession(program_id=total_package.id, name="Day 2"),
            ProgramSession(program_id=total_package.id, name="Day 3"),
            ProgramSession(program_id=unrelated.id, name="Day 1"),
        ])

    db_module._migrate_add_columns()

    with db_module.get_session() as s:
        total_names = [item.name for item in s.query(ProgramSession).filter_by(
            program_id=total_package.id
        ).order_by(ProgramSession.id)]
        unrelated_name = s.query(ProgramSession).filter_by(
            program_id=unrelated.id
        ).one().name
        assert total_names == ["Full Body 1", "Full Body 2", "Full Body 3"]
        assert unrelated_name == "Day 1"


def test_onboarding_proposal_is_reviewed_before_activation(client):
    c, db_module = client
    from datetime import datetime, timedelta
    from db import Activity, PlannedSession, ProgramSession, TrainingProgram

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
            "plan_key": "full_body_2",
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
    assert "Save and approve program" in review.text
    assert "Save day" not in review.text
    assert "Add additional session" in review.text
    assert 'data-muscle-group="' in review.text
    assert "No matching same-muscle exercises" in review.text
    assert "Include warm-up set" in review.text
    assert 'placeholder="Auto"' not in review.text
    assert 'onclick="resetProgram(' in review.text
    assert "location.reload()" not in review.text
    assert "window.scrollTo" not in review.text
    assert "function replaceProgramContent" in review.text

    missing_name = c.post(f"/api/program/{program_id}/sessions", json={})
    assert missing_name.status_code == 422

    added = c.post(f"/api/program/{program_id}/sessions", json={"name": "Accessories"})
    assert added.status_code == 200
    assert added.json()["name"] == "Accessories"
    added_session_id = added.json()["id"]
    not_ready = c.post(f"/program/{program_id}/approve", follow_redirects=False)
    assert not_ready.status_code == 422
    removed = c.delete(f"/api/program/{program_id}/sessions/{added_session_id}")
    assert removed.status_code == 200
    with db_module.get_session() as s:
        assert s.query(ProgramSession).filter_by(program_id=program_id).count() == 2

    approved = c.post(f"/program/{program_id}/approve", follow_redirects=False)
    assert approved.status_code == 303
    assert approved.headers["location"] == "/program?view=active&approved=1"
    with db_module.get_session() as s:
        program = s.get(TrainingProgram, program_id)
        assert program.active is True
        assert program.status == "active"
        assert s.query(PlannedSession).count() == 0
        session_id = s.query(ProgramSession).filter_by(program_id=program_id).first().id

    active_page = c.get("/program?view=active")
    assert active_page.status_code == 200
    assert "This remains editable" in active_page.text
    assert "<h1>A/B Full Body" in active_page.text
    assert "View original plan" in active_page.text
    assert "Adjust plan" in active_page.text
    assert "Nothing scheduled yet" in active_page.text
    assert "Reset to template" in active_page.text
    assert "Save all days" in active_page.text
    assert "Reset day" in active_page.text

    edited = c.post(
        f"/api/session/{session_id}/exercises",
        json=[{
            "exercise_name": "Goblet Squat", "sets": 2, "reps": 10, "weight_kg": 12,
            "warmup_enabled": True, "warmup_reps": 7, "warmup_weight_kg": 6,
        }],
    )
    assert edited.status_code == 200
    with db_module.get_session() as s:
        from db import SessionExercise
        exercise = s.query(SessionExercise).filter_by(program_session_id=session_id).one()
        assert exercise.exercise_name == "Goblet Squat"
        assert exercise.sets == 2
        assert exercise.warmup_enabled is True
        assert exercise.warmup_reps == 7
        assert exercise.warmup_weight_kg == 6

    reset_day = c.post(f"/program/{program_id}/sessions/{session_id}/reset", follow_redirects=False)
    assert reset_day.status_code == 303
    assert reset_day.headers["location"] == "/program?view=active"
    with db_module.get_session() as s:
        restored_names = [
            exercise.exercise_name
            for exercise in s.query(SessionExercise).filter_by(program_session_id=session_id).order_by(SessionExercise.order_index)
        ]
        assert "Goblet Squat" not in restored_names
        assert "Trap Bar Deadlift" in restored_names

    manual_warmup = c.post(
        f"/api/session/{session_id}/exercises",
        json=[{
            "exercise_name": "Rope Pressdown", "sets": 2, "reps": 12, "weight_kg": 10,
            "warmup_enabled": True, "warmup_reps": 8, "warmup_weight_kg": 5,
        }],
    )
    assert manual_warmup.status_code == 200
    with db_module.get_session() as s:
        exercise = s.query(SessionExercise).filter_by(program_session_id=session_id).one()
        assert exercise.exercise_name == "Rope Pressdown"
        assert exercise.warmup_enabled is True
        assert exercise.warmup_reps == 8
        assert exercise.warmup_weight_kg == 5

    timed_manual_warmup = c.post(
        f"/api/session/{session_id}/exercises",
        json=[{
            "exercise_name": "Plank", "sets": 2, "duration_seconds": 30,
            "warmup_enabled": True, "warmup_duration_seconds": 15,
        }],
    )
    assert timed_manual_warmup.status_code == 200
    with db_module.get_session() as s:
        exercise = s.query(SessionExercise).filter_by(program_session_id=session_id).one()
        assert exercise.warmup_enabled is True
        assert exercise.warmup_reps is None
        assert exercise.warmup_duration_seconds == 15

    rep_warmup_on_timed_exercise = c.post(
        f"/api/session/{session_id}/exercises",
        json=[{
            "exercise_name": "Plank", "sets": 2, "duration_seconds": 30,
            "warmup_enabled": True, "warmup_target_type": "reps", "warmup_reps": 8,
        }],
    )
    assert rep_warmup_on_timed_exercise.status_code == 200
    with db_module.get_session() as s:
        exercise = s.query(SessionExercise).filter_by(program_session_id=session_id).one()
        assert exercise.warmup_reps == 8
        assert exercise.warmup_duration_seconds is None

    rejected = c.post(
        f"/api/session/{session_id}/exercises",
        json=[{"exercise_name": "Made Up Exercise", "sets": 3, "reps": 10}],
    )
    assert rejected.status_code == 422

    reset = c.post(f"/program/{program_id}/reset", follow_redirects=False)
    assert reset.status_code == 303
    assert reset.headers["location"] == "/program?view=active"
    with db_module.get_session() as s:
        sessions = s.query(ProgramSession).filter_by(program_id=program_id).order_by(ProgramSession.sequence_order).all()
        assert [item.name for item in sessions] == ["Full Body 1", "Full Body 2"]
        restored_names = [item.exercise_name for item in sessions[0].exercises]
        assert "Rope Pressdown" not in restored_names
        assert "Trap Bar Deadlift" in restored_names


def test_onboarding_uses_matching_recent_weight_and_half_weight_warmup(client):
    c, db_module = client
    from datetime import datetime
    from db import Activity, ExerciseSet, ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        s.add(Activity(id=9901, activity_type="strength_training", start_time=datetime.now(), name="Gym"))
        s.add(ExerciseSet(id=9902, activity_id=9901, set_index=1, set_type="ACTIVE", exercise_category="FRONT_SQUAT", exercise_name="Front Squat", reps=8, weight_kg=60))
    response = c.post("/onboarding", data={"plan_key": "full_body_2"}, follow_redirects=False)
    assert response.status_code == 303
    with db_module.get_session() as s:
        program = s.query(TrainingProgram).filter_by(status="draft").one()
        exercise = s.query(SessionExercise).join(ProgramSession).filter(ProgramSession.program_id == program.id, SessionExercise.exercise_key == "SQUAT:BARBELL_FRONT_SQUAT").one()
        assert exercise.weight_kg == 60
        assert exercise.warmup_weight_kg == 30
        assert exercise.warmup_reps == 8


def test_selected_plan_overrides_history_recommendation(client):
    c, db_module = client
    from datetime import datetime, timedelta
    from db import Activity, ExerciseSet, TrainingProgram

    patterns = [
        ("Push", ["BENCH_PRESS", "OVERHEAD_PRESS", "TRICEPS_EXTENSION"]),
        ("Pull", ["LAT_PULL_DOWN", "BENT_OVER_ROW", "BICEP_CURL"]),
        ("Legs", ["SQUAT", "LUNGE", "CALF_RAISE"]),
    ] * 2
    with db_module.get_session() as s:
        for index, (name, exercises) in enumerate(patterns):
            activity_id = 9300 + index
            s.add(Activity(id=activity_id, activity_type="strength_training", start_time=datetime.now() - timedelta(days=12 - index), name=name))
            for set_index, exercise in enumerate(exercises):
                s.add(ExerciseSet(id=9400 + index * 10 + set_index, activity_id=activity_id, set_index=set_index, exercise_category=exercise, exercise_name=exercise, reps=8, weight_kg=40))

    onboarding = c.get("/onboarding")
    assert "Best match" in onboarding.text

    response = c.post("/onboarding", data={"plan_key": "full_body_2"}, follow_redirects=False)
    assert response.status_code == 303
    with db_module.get_session() as s:
        program = s.query(TrainingProgram).filter_by(status="draft").one()
        assert program.days_per_week == 2
        assert "full_body_2" in program.goal_tags


def test_active_program_hides_legacy_non_gym_sessions(client):
    c, db_module = client
    from db import AthleteProfile, ProgramSession, TrainingProgram

    with db_module.get_session() as s:
        profile = AthleteProfile(id=1, onboarding_complete=True)
        program = TrainingProgram(name="Test", status="active", active=True, days_per_week=2)
        s.add_all([profile, program])
        s.flush()
        s.add_all([
            ProgramSession(program_id=program.id, name="Gym", session_role="coach_strength"),
            ProgramSession(program_id=program.id, name="Soccer", session_role="activity_anchor"),
        ])

    response = c.get("/program?view=active")
    assert response.status_code == 200
    assert "Gym strength" in response.text
    assert "Activity anchors" not in response.text
