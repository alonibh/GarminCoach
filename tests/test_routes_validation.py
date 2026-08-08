"""Route-level input validation: bad input must 4xx, never 500.

Imports app with the scheduler and Garmin login stubbed out, auth disabled,
and the DB pointed at an isolated in-memory SQLite.
"""
from pathlib import Path

import config
import pytest


@pytest.fixture
def client(monkeypatch):
    import config
    # Disable app auth so requests pass the cookie middleware.
    monkeypatch.setattr(config, "APP_USERNAME", "", raising=False)

    # Stub the startup side effects (scheduler thread + Garmin network login).
    import sync.scheduler as scheduler
    monkeypatch.setattr(scheduler, "start_scheduler", lambda: None)
    class FakeGarminClient:
        def login(self, *a, **k):
            return False
        def is_authenticated(self):
            return True
    fake_client = FakeGarminClient()
    monkeypatch.setattr("sync.garmin_registry.GarminClientRegistry.get", lambda self, uid: fake_client)
    from control_db import User
    monkeypatch.setattr("app.resolve_web_session", lambda s, token: User(id="00000000-0000-0000-0000-000000000001", email="test@example.com", status="active", role="owner", onboarding_step="complete"))
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
    import tenant_store
    monkeypatch.setattr(tenant_store, "engine_for_user", lambda uid, root=None: engine)
    db_module.Base.metadata.create_all(engine)

    from fastapi.testclient import TestClient
    import app as app_module
    import tenant_context
    with tenant_context.tenant_scope(tenant_context.TenantIdentity("00000000-0000-0000-0000-000000000001")):
        yield TestClient(app_module.app), db_module


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
    from db import Activity, ExerciseSet
    from datetime import datetime
    with db_module.get_session() as s:
        if not s.get(Activity, 7001):
            s.add(Activity(id=7001, activity_type="strength_training", start_time=datetime.now(), duration_s=1800))
            s.flush()
        if not s.get(ExerciseSet, 42):
            s.add(ExerciseSet(id=42, activity_id=7001, set_index=0,
                              exercise_category="BENCH_PRESS", reps=10, weight_kg=20.0))

    resp = c.post("/set/42", data={"reps": "abc", "weight_kg": ""}, follow_redirects=False)
    assert resp.status_code == 400


def test_set_valid_update_redirects(client):
    c, db_module = client
    from db import Activity, ExerciseSet
    from datetime import datetime
    with db_module.get_session() as s:
        if not s.get(Activity, 7002):
            s.add(Activity(id=7002, activity_type="strength_training", start_time=datetime.now(), duration_s=1800))
            s.flush()
        if not s.get(ExerciseSet, 43):
            s.add(ExerciseSet(id=43, activity_id=7002, set_index=0,
                              exercise_category="SQUAT", reps=10, weight_kg=20.0))

    resp = c.post("/set/43", data={"reps": "12", "weight_kg": "25.5"}, follow_redirects=False)
    assert resp.status_code == 303


def test_editor_preserves_submitted_exercise_id_for_rest_only_edit(client):
    c, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(name="P", status="draft")
        s.add(program); s.flush()
        planned = ProgramSession(program_id=program.id, name="A")
        s.add(planned); s.flush()
        exercise = SessionExercise(program_session_id=planned.id, exercise_name="Goblet Squat",
            exercise_key="SQUAT:GOBLET_SQUAT", garmin_category="SQUAT", garmin_name="GOBLET_SQUAT",
            sets=2, reps=10, weight_kg=12, rest_seconds=60)
        s.add(exercise); s.flush()
        session_id, exercise_id = planned.id, exercise.id

    response = c.post(f"/api/session/{session_id}/exercises", json=[{
        "id": exercise_id, "exercise_name": "Goblet Squat", "exercise_key": "SQUAT:GOBLET_SQUAT",
        "sets": 2, "reps": 10, "weight_kg": 12, "rest_seconds": 90,
    }])

    assert response.status_code == 200
    with db_module.get_session() as s:
        exercise = s.get(SessionExercise, exercise_id)
        assert exercise is not None and exercise.rest_seconds == 90


def test_editor_round_trips_rest_seconds_and_ignores_legacy_fields(client):
    """save API persists rest_seconds; silently ignores superset_group and transition_rest_seconds."""
    c, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(name="P", status="draft")
        s.add(program); s.flush()
        routine = ProgramSession(program_id=program.id, name="A")
        s.add(routine); s.flush()
        rows = [
            SessionExercise(program_session_id=routine.id, exercise_name="Goblet Squat", exercise_key="SQUAT:GOBLET_SQUAT", sets=2, reps=10, rest_seconds=60, order_index=0),
            SessionExercise(program_session_id=routine.id, exercise_name="Dumbbell Row", exercise_key="ROW:DUMBBELL_ROW", sets=2, reps=10, rest_seconds=60, order_index=1),
        ]
        s.add_all(rows); s.flush()
        session_id, ids = routine.id, [row.id for row in rows]
    # Old client sends legacy fields — both must be silently ignored; rest_seconds is authoritative
    payload = [
        {"id": ids[0], "exercise_name": "Goblet Squat", "exercise_key": "SQUAT:GOBLET_SQUAT", "sets": 2, "reps": 10, "rest_seconds": 75, "superset_group": "pair_1", "transition_rest_seconds": 90},
        {"id": ids[1], "exercise_name": "Dumbbell Row", "exercise_key": "ROW:DUMBBELL_ROW", "sets": 2, "reps": 10, "rest_seconds": 75, "superset_group": "pair_1", "transition_rest_seconds": 90},
    ]
    response = c.post(f"/api/session/{session_id}/exercises", json=payload)
    assert response.status_code == 200
    assert [row["id"] for row in response.json()["exercises"]] == ids
    with db_module.get_session() as s:
        for item_id in ids:
            ex = s.get(SessionExercise, item_id)
            assert ex.rest_seconds == 75
            assert not hasattr(ex, "transition_rest_seconds") or ex.transition_rest_seconds is None
            assert not hasattr(ex, "superset_group") or ex.superset_group is None


def test_delete_endpoint_removes_any_exercise_without_superset_guard(client):
    """delete_session_exercise no longer blocks on superset_group."""
    c, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(name="P", status="draft")
        s.add(program); s.flush()
        routine = ProgramSession(program_id=program.id, name="A")
        s.add(routine); s.flush()
        ex1 = SessionExercise(program_session_id=routine.id, exercise_name="Goblet Squat", exercise_key="SQUAT:GOBLET_SQUAT", sets=2, reps=10, rest_seconds=60, order_index=0)
        ex2 = SessionExercise(program_session_id=routine.id, exercise_name="Dumbbell Row", exercise_key="ROW:DUMBBELL_ROW", sets=2, reps=10, rest_seconds=60, order_index=1)
        s.add_all([ex1, ex2]); s.flush()
        session_id, id1, id2 = routine.id, ex1.id, ex2.id
    assert c.delete(f"/api/session/{session_id}/exercises/{id1}").status_code == 200
    with db_module.get_session() as s:
        assert s.get(SessionExercise, id1) is None
        assert s.get(SessionExercise, id2) is not None


def test_new_editor_row_response_installs_id_for_idempotent_second_save(client):
    c, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(name="P", status="draft")
        s.add(program); s.flush()
        planned = ProgramSession(program_id=program.id, name="Empty")
        s.add(planned); s.flush()
        session_id = planned.id
    row = {"exercise_name": "Goblet Squat", "exercise_key": "SQUAT:GOBLET_SQUAT",
           "sets": 2, "reps": 10, "weight_kg": 12, "rest_seconds": 60}

    first = c.post(f"/api/session/{session_id}/exercises", json=[row])
    assert first.status_code == 200
    first_body = first.json()
    assert first_body["ok"] is True and len(first_body["exercises"]) == 1
    exercise_id = first_body["exercises"][0]["id"]
    second = c.post(f"/api/session/{session_id}/exercises", json=[{**row, "id": exercise_id}])

    assert second.status_code == 200
    assert second.json()["exercises"] == [{"id": exercise_id, "order_index": 0}]
    with db_module.get_session() as s:
        rows = s.query(SessionExercise).filter_by(program_session_id=session_id).all()
        assert [item.id for item in rows] == [exercise_id]


def test_editor_rejects_duplicate_cross_session_and_malformed_ids(client):
    c, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(name="P", status="draft")
        s.add(program); s.flush()
        first, other = ProgramSession(program_id=program.id, name="A"), ProgramSession(program_id=program.id, name="B")
        s.add_all((first, other)); s.flush()
        exercise = SessionExercise(program_session_id=first.id, exercise_name="Goblet Squat",
            exercise_key="SQUAT:GOBLET_SQUAT", sets=2, reps=10, weight_kg=12)
        s.add(exercise); s.flush()
        first_id, other_id, exercise_id = first.id, other.id, exercise.id
    row = {"id": exercise_id, "exercise_name": "Goblet Squat", "exercise_key": "SQUAT:GOBLET_SQUAT", "sets": 2, "reps": 10, "weight_kg": 12}

    assert c.post(f"/api/session/{first_id}/exercises", json=[row, row]).status_code == 422
    assert c.post(f"/api/session/{other_id}/exercises", json=[row]).status_code == 422
    assert c.post(f"/api/session/{first_id}/exercises", json=[{**row, "id": "bad"}]).status_code == 422


def test_delete_custom_session_stales_child_pending_proposal(client):
    c, db_module = client
    from db import ProgramSession, SessionExercise, StrengthProgressionProposal, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(name="P", status="draft")
        s.add(program); s.flush()
        planned = ProgramSession(program_id=program.id, name="Custom", is_custom=True)
        s.add(planned); s.flush()
        exercise = SessionExercise(program_session_id=planned.id, exercise_name="Goblet Squat",
            exercise_key="SQUAT:GOBLET_SQUAT", sets=2, reps=10, weight_kg=12)
        s.add(exercise); s.flush()
        proposal = StrengthProgressionProposal(proposal_id="pending-custom", session_exercise_id=exercise.id,
            session_exercise_id_snapshot=exercise.id, policy_version="strength-progression-v1",
            prescription_fingerprint="fp", direction="increase", current_weight_grams=12000,
            suggested_weight_grams=14500, status="pending", decisive_evidence_one_id="a",
            decisive_evidence_two_id="b", reason_codes_json="[]", idempotency_key="pending-custom",
            current_pending_key=f"{exercise.id}:strength-progression-v1:fp")
        s.add(proposal); s.flush()
        program_id, session_id, exercise_id = program.id, planned.id, exercise.id

    response = c.delete(f"/api/program/{program_id}/sessions/{session_id}")

    assert response.status_code == 200
    with db_module.get_session() as s:
        proposal = s.get(StrengthProgressionProposal, "pending-custom")
        assert proposal.status == "stale" and proposal.current_pending_key is None
        assert proposal.session_exercise_id_snapshot == exercise_id


def test_safe_next_blocks_open_redirect(client):
    import app as app_module
    assert app_module._safe_next("https://evil.com") == "/"
    assert app_module._safe_next("//evil.com") == "/"
    assert app_module._safe_next("/dashboard") == "/dashboard"
    assert app_module._safe_next("") == "/"
    assert app_module._safe_next("/\\evil") == "/"


def test_manual_sync_forces_recent_fetch_without_backfill(client, monkeypatch):
    c, _ = client
    import app as app_module

    captured = {}
    monkeypatch.setattr(
        app_module.sync_runner,
        "try_start_sync",
        lambda full, force=False, allow_backfill=False: captured.update(
            {"full": full, "force": force, "allow_backfill": allow_backfill}
        ) or True,
    )

    resp = c.post("/sync", follow_redirects=False)

    assert resp.status_code == 303
    assert captured == {"full": False, "force": True, "allow_backfill": False}


def test_manual_sync_ignores_full_form_field(client, monkeypatch):
    c, _ = client
    import app as app_module

    captured = {}
    monkeypatch.setattr(
        app_module.sync_runner,
        "try_start_sync",
        lambda full, force=False, allow_backfill=False: captured.update(
            {"full": full, "force": force, "allow_backfill": allow_backfill}
        ) or True,
    )

    response = c.post("/sync", data={"full": "true"}, follow_redirects=False)

    assert response.status_code == 303
    assert captured == {"full": False, "force": True, "allow_backfill": False}


def test_manual_sync_json_response_stays_on_dashboard(client, monkeypatch):
    c, _ = client
    import app as app_module

    captured = {}
    monkeypatch.setattr(
        app_module.sync_runner,
        "try_start_sync",
        lambda full, force=False, allow_backfill=False: captured.update(
            {"full": full, "force": force, "allow_backfill": allow_backfill}
        ) or True,
    )
    monkeypatch.setattr(app_module.sync_runner, "is_running", lambda: True)

    resp = c.post("/sync", headers={"Accept": "application/json"})

    assert resp.status_code == 200
    assert resp.json() == {"started": True, "running": True}
    assert captured == {"full": False, "force": True, "allow_backfill": False}


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
        s.merge(AthleteProfile(id=1, onboarding_complete=True))

    response = c.get("/")

    assert response.status_code == 200
    assert "location.reload()" not in response.text
    assert "fetch('/sync'" in response.text
    assert "chart.update('none')" in response.text
    assert 'id="sync-form"' in response.text
    ui_css = (Path(__file__).resolve().parents[1] / "static" / "ui.css").read_text(encoding="utf-8")
    assert ".dashboard-actions [hidden] { display: none !important; }" in ui_css


def test_dashboard_renders_local_informational_trend_surface(client):
    c, _ = client
    response = c.get("/")
    assert response.status_code == 200
    assert "28-day recovery and health trends" not in response.text
    assert "Long-term fitness history" not in response.text
    assert 'data-chart-range="7"' in response.text
    assert 'data-chart-range="28"' in response.text
    assert "dashboardChartRange" in response.text
    # Stress and Body Battery charts removed
    assert 'id="stressChart"' not in response.text
    assert 'id="bodyBatteryChart"' not in response.text
    assert "lineChart('stressChart'" not in response.text
    assert "bodyBatteryChart(" not in response.text
    # Remaining charts still present
    assert 'id="rhrChart"' in response.text
    assert 'id="hrvChart"' in response.text
    assert 'id="sleepChart"' in response.text
    assert 'id="stepsChart"' in response.text
    assert 'id="caloriesChart"' in response.text
    assert "lineChart('rhrChart'" in response.text


def test_workout_detail_renders_stored_hr_zones_without_garmin(client, monkeypatch):
    c, db_module = client
    from datetime import datetime
    from db import Activity
    import app as app_module

    from sync.garmin_client import GarminClient
    monkeypatch.setattr(
        GarminClient,
        "hr_zones",
        lambda *_: (_ for _ in ()).throw(AssertionError("page must stay local")),
    )
    with db_module.get_session() as s:
        s.add(Activity(
            id=8201, activity_type="running", start_time=datetime.now(), duration_s=900,
            hr_zone_seconds="[60, 120, 180, 240, 300]",
        ))

    response = c.get("/workout/8201")

    assert response.status_code == 200
    assert "Z1" in response.text
    assert "1m" in response.text
    assert "7%" in response.text
    assert "Z5" in response.text
    assert "5m" in response.text
    assert "33%" in response.text


def test_stored_hr_zones_calculate_below_z1_locally_and_reject_bad_json():
    import app as app_module

    zones = app_module._stored_hr_zones("[60, 60, 60, 60, 60]", duration_s=600)

    assert zones[0] == {"zone": 0, "low_bpm": None, "minutes": 5, "pct": 50}
    assert zones[1:] == [
        {"zone": 1, "low_bpm": None, "minutes": 1, "pct": 10},
        {"zone": 2, "low_bpm": None, "minutes": 1, "pct": 10},
        {"zone": 3, "low_bpm": None, "minutes": 1, "pct": 10},
        {"zone": 4, "low_bpm": None, "minutes": 1, "pct": 10},
        {"zone": 5, "low_bpm": None, "minutes": 1, "pct": 10},
    ]
    assert app_module._stored_hr_zones(None, duration_s=600) == []
    assert app_module._stored_hr_zones("not json", duration_s=600) == []
    assert app_module._stored_hr_zones("[0, 0, 0, 0, 0]", duration_s=600) == []


def test_dashboard_and_workout_gets_only_use_local_data(client, monkeypatch):
    c, db_module = client
    from datetime import datetime
    from db import Activity
    import app as app_module

    class NoGarminReads:
        def is_authenticated(self):
            return True

        def __getattr__(self, name):
            raise AssertionError(f"unexpected Garmin read: {name}")

    no_reads = NoGarminReads()
    monkeypatch.setattr(app_module, "client", no_reads)
    monkeypatch.setattr(
        "sync.garmin_registry.GarminClientRegistry.get", lambda self, uid: no_reads
    )
    with db_module.get_session() as s:
        s.add(Activity(
            id=8202, activity_type="running", start_time=datetime.now(), duration_s=60,
            hr_zone_seconds="[60, 0, 0, 0, 0]",
        ))

    assert c.get("/").status_code == 200
    assert c.get("/workout/8202").status_code == 200


def test_onboarding_renders_history_defaults(client):
    c, db_module = client
    from datetime import datetime
    from db import Activity, Workout

    with db_module.get_session() as s:
        if not s.query(Activity).filter_by(id=8101).first():
            s.add(Activity(id=8101, activity_type="strength_training", start_time=datetime.now(), duration_s=1800))
        if not s.query(Activity).filter_by(id=8102).first():
            s.add(Activity(id=8102, activity_type="strength_training", start_time=datetime.now(), duration_s=1800))
        if not s.query(Activity).filter_by(id=8103).first():
            s.add(Activity(id=8103, activity_type="running", start_time=datetime.now(), duration_s=1800))
        if not s.query(Workout).filter_by(workout_id=8104).first():
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
    assert "Weekly Workout Availability" in resp.text
    assert "availability-rows-container" in resp.text
    assert "Full Body · 2 days" in resp.text
    assert "Beginner Full Body · 3 days" in resp.text
    assert "Upper / Lower Bodybuilding · 4 days" in resp.text
    assert "Push / Pull / Legs A/B · 6 days" in resp.text
    assert "Upper Strength" not in resp.text


def test_onboarding_step_garmin_renders_connect_form(client, monkeypatch):
    c, session_factory = client
    import config
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    from control_db import User, init_control_db, get_control_session, utcnow
    init_control_db()
    with get_control_session() as session:
        user = session.get(User, "00000000-0000-0000-0000-000000000001")
        if not user:
            user = User(
                id="00000000-0000-0000-0000-000000000001",
                email="test@example.com",
                status="active",
                onboarding_step="complete",
                consented_at=utcnow(),
                timezone="Asia/Jerusalem",
            )
            session.add(user)
            session.commit()

    resp = c.get("/onboarding?step=garmin")
    assert resp.status_code == 200
    assert "Connect Garmin" in resp.text
    assert "garmin_password" in resp.text



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

    class DisconnectedClient:
        def is_authenticated(self):
            return False
        def login(self, *a, **k):
            return False
    class ConnectedClient:
        def is_authenticated(self):
            return True
        def login(self, *a, **k):
            return True

    monkeypatch.setattr("sync.garmin_registry.GarminClientRegistry.get", lambda self, uid: DisconnectedClient())
    disconnected = c.get("/", follow_redirects=False)
    if config.MULTI_USER_ENABLED:
        assert disconnected.status_code == 200
        assert "Connect Garmin" in disconnected.text
    else:
        assert disconnected.status_code == 303
        assert disconnected.headers["location"] == "/login"

    onboarding = c.get("/onboarding")
    assert "Connect Garmin" in onboarding.text

    monkeypatch.setattr("sync.garmin_registry.GarminClientRegistry.get", lambda self, uid: ConnectedClient())
    connected = c.get("/", follow_redirects=False)
    assert connected.status_code == 200


def test_onboarding_creates_reviewable_program_proposal(client):
    c, db_module = client
    import json
    from db import AthleteProfile, Goal, ProgramSession, TrainingProgram, Workout

    with db_module.get_session() as s:
        if not s.query(Workout).filter_by(workout_id=9001).first():
            s.add(Workout(workout_id=9001, name="Upper Strength", sport_type="strength_training", steps_json="[]"))

    resp = c.post(
        "/onboarding",
        data={
            "plan_key": "ppl_6",
            "avail_start_6": "18:00",
            "avail_end_6": "20:00",
            "avail_off_6": "0",
        },
        follow_redirects=False,
    )

    assert resp.status_code == 303
    with db_module.get_session() as s:
        profile = s.get(AthleteProfile, 1)
        assert profile.training_type == "strength_focused"
        assert profile.goal_detail == ""
        assert json.loads(profile.timing_preferences)["6"]["start"] == "18:00"
        assert json.loads(profile.availability)["6"]["start"] == "18:00"
        assert json.loads(profile.equipment_access) == ["gym"]

        goal = s.get(Goal, 1)
        assert goal.goal == ""
        assert json.loads(goal.custom_input)["6"]["start"] == "18:00"

        program = s.query(TrainingProgram).filter(TrainingProgram.status == "draft").one()
        assert program.name == "Push / Pull / Legs A/B · 6 days"
        assert program.mode == "curated_strength"
        assert program.days_per_week == 6
        assert program.active is False
        assert "does not assign dates or upload" in program.rationale

        sessions = s.query(ProgramSession).filter(ProgramSession.program_id == program.id).order_by(ProgramSession.sequence_order.asc()).all()
        assert [ps.name for ps in sessions] == ["Push A", "Pull A", "Legs A", "Push B", "Pull B", "Legs B"]
        assert all(ps.session_role == "coach_strength" for ps in sessions)
        assert all(ps.base_workout_id is None for ps in sessions)

    setup = c.get("/onboarding")
    assert 'value="ppl_6" checked' in setup.text
    assert 'name="avail_start_6" value="18:00"' in setup.text


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

    from sqlalchemy import text
    with db_module.get_session() as s:
        s.execute(text("CREATE TABLE IF NOT EXISTS app_migrations (migration_key VARCHAR(128) PRIMARY KEY, applied_at DATETIME NOT NULL)"))
        s.execute(text("DELETE FROM app_migrations"))

    db_module._migrate_add_columns()
    with db_module.get_session() as s:
        s.expire_all()
        assert s.get(SessionExercise, exercise_id).rest_seconds == 180
        s.get(SessionExercise, exercise_id).rest_seconds = 91

    db_module._migrate_add_columns()
    with db_module.get_session() as s:
        s.expire_all()
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

    from sqlalchemy import text
    with db_module.get_session() as s:
        s.execute(text("CREATE TABLE IF NOT EXISTS app_migrations (migration_key VARCHAR(128) PRIMARY KEY, applied_at DATETIME NOT NULL)"))
        s.execute(text("DELETE FROM app_migrations"))

    db_module._migrate_add_columns()
    with db_module.get_session() as s:
        assert [s.get(SessionExercise, exercise_id).rest_seconds for exercise_id in exercise_ids] == [
            expected for *_, expected in fixtures
        ]
        assert s.get(SessionExercise, custom_exercise_id).rest_seconds == 61


def test_transition_rest_migration_copies_value_to_rest_seconds_and_is_idempotent(client):
    """Legacy rows with transition_rest_seconds != NULL get rest_seconds updated; migration is idempotent."""
    _, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    from sqlalchemy import text
    with db_module.get_session() as s:
        prog = TrainingProgram(name="PPL", goal_tags='["ppl_6"]', status="draft")
        s.add(prog); s.flush()
        sess = ProgramSession(program_id=prog.id, name="Push A")
        s.add(sess); s.flush()
        # Simulate pre-migration rows: rest_seconds=45, transition_rest_seconds=90
        ex1 = SessionExercise(program_session_id=sess.id, exercise_name="Bench Press", exercise_key="BENCH_PRESS", sets=3, reps=10, rest_seconds=45, order_index=0)
        ex2 = SessionExercise(program_session_id=sess.id, exercise_name="Dumbbell Row", exercise_key="ROW", sets=3, reps=10, rest_seconds=45, order_index=1)
        ex3 = SessionExercise(program_session_id=sess.id, exercise_name="Custom", exercise_key="CUSTOM", sets=3, reps=10, rest_seconds=75, order_index=2)
        s.add_all([ex1, ex2, ex3]); s.flush()
        sess_id, ids = sess.id, [ex1.id, ex2.id, ex3.id]
        # Manually add transition_rest_seconds via raw SQL (simulating legacy DB)
        columns = {r[1] for r in s.execute(text("PRAGMA table_info(session_exercises)")).all()}
        if "transition_rest_seconds" not in columns:
            s.execute(text("ALTER TABLE session_exercises ADD COLUMN transition_rest_seconds INTEGER"))
        s.execute(text("UPDATE session_exercises SET transition_rest_seconds = 90 WHERE id IN (:a, :b)"), {"a": ids[0], "b": ids[1]})
        # ex3 has NULL transition_rest_seconds — must remain at rest_seconds=75
        s.execute(text("CREATE TABLE IF NOT EXISTS app_migrations (migration_key VARCHAR(128) PRIMARY KEY, applied_at DATETIME NOT NULL)"))
        s.execute(text("DELETE FROM app_migrations WHERE migration_key = 'transition_rest_to_rest_seconds_2026_08_08_v1'"))
    db_module._migrate_add_columns()
    with db_module.get_session() as s:
        rows = {r.id: r for r in s.query(SessionExercise).filter_by(program_session_id=sess_id)}
        assert rows[ids[0]].rest_seconds == 90
        assert rows[ids[1]].rest_seconds == 90
        assert rows[ids[2]].rest_seconds == 75
        assert s.execute(text("SELECT COUNT(*) FROM app_migrations WHERE migration_key = 'transition_rest_to_rest_seconds_2026_08_08_v1'")).scalar_one() == 1
    # Idempotent: second run must not change rows
    db_module._migrate_add_columns()
    with db_module.get_session() as s:
        rows = {r.id: r for r in s.query(SessionExercise).filter_by(program_session_id=sess_id)}
        assert rows[ids[0]].rest_seconds == 90
        assert rows[ids[1]].rest_seconds == 90


def test_legacy_db_with_extra_superset_group_column_starts_and_works(client):
    """A DB that still has a legacy superset_group column starts safely without schema reset."""
    _, db_module = client
    from sqlalchemy import text
    with db_module.get_session() as s:
        # Simulate a legacy DB by adding the column manually if not present
        columns = {r[1] for r in s.execute(text("PRAGMA table_info(session_exercises)")).all()}
        if "superset_group" not in columns:
            s.execute(text("ALTER TABLE session_exercises ADD COLUMN superset_group VARCHAR(32)"))
    # The application should start and the migration should complete without error
    db_module._migrate_add_columns()
    # The ORM must NOT map superset_group; confirm the model has no such attribute
    from db import SessionExercise
    assert not hasattr(SessionExercise, "superset_group") or "superset_group" not in [
        c.key for c in SessionExercise.__table__.columns
    ]


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
    assert "Add additional session" not in review.text
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
    assert "Matching Superset" not in active_page.text
    assert "superset-badge" not in active_page.text
    assert "data-superset-group" not in active_page.text
    assert "ex-superset" not in active_page.text
    assert "<h1>A/B Full Body" in active_page.text
    assert "View original plan" in active_page.text
    assert "Change plan" in active_page.text
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


def test_program_tab_shows_active_and_adjust_plan_shows_draft(client):
    c, db_module = client
    from db import AthleteProfile, ProgramSession, TrainingProgram

    with db_module.get_session() as s:
        profile = AthleteProfile(id=1, onboarding_complete=True)
        active_prog = TrainingProgram(name="Active Plan", status="active", active=True, days_per_week=2)
        draft_prog = TrainingProgram(name="Proposed Plan", status="draft", active=False, days_per_week=3)
        s.add_all([profile, active_prog, draft_prog])
        s.flush()
        s.add(ProgramSession(program_id=active_prog.id, name="Active Day 1", session_role="coach_strength"))
        s.add(ProgramSession(program_id=draft_prog.id, name="Draft Day 1", session_role="coach_strength"))

    # Plan tab (/program) should show Active Plan
    plan_tab = c.get("/program")
    assert plan_tab.status_code == 200
    assert "Active Plan" in plan_tab.text
    assert "Review your program" not in plan_tab.text
    assert 'href="/onboarding"' in plan_tab.text

    # Adjust plan (/program?view=draft) should show draft Review page
    adjust_plan = c.get("/program?view=draft")
    assert adjust_plan.status_code == 200
    assert "Review your program" in adjust_plan.text
    assert "Proposed Plan" in adjust_plan.text


def test_legacy_removed_plan_renders_safely_without_reset_controls(client):
    """A program whose catalog key no longer exists renders /program but shows no Reset controls."""
    c, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(
            name="Muscle & Strength Building Split · 5 days",
            goal_tags='["muscle_strength_5"]',
            status="active",
            active=True,
            source_type="curated_archetype",
        )
        s.add(program); s.flush()
        ps = ProgramSession(program_id=program.id, name="Upper Strength", sport_type="strength_training", sequence_order=1)
        s.add(ps); s.flush()
        s.add(SessionExercise(
            program_session_id=ps.id, exercise_name="Bench Press", exercise_key="BENCH_PRESS",
            garmin_category="BENCH_PRESS", garmin_name="BENCH_PRESS", sets=3, reps=8,
            rest_seconds=180, order_index=0,
        ))
    page = c.get("/program?view=active")
    assert page.status_code == 200
    # Page renders without error
    assert "Muscle &amp; Strength Building Split" in page.text or "Muscle" in page.text
    # Reset controls must NOT appear because the catalog template no longer exists
    assert "Reset to template" not in page.text
    assert "Reset day" not in page.text
    # No superset UI
    assert "superset" not in page.text.lower() or "ex-superset" not in page.text


def test_save_api_ignores_legacy_superset_and_transition_fields(client):
    """Saving exercises with superset_group/transition_rest_seconds silently ignores both; rest_seconds is authoritative."""
    c, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(name="P", status="draft")
        s.add(program); s.flush()
        ps = ProgramSession(program_id=program.id, name="A")
        s.add(ps); s.flush()
        session_id = ps.id
    payload = [
        {"exercise_name": "Goblet Squat", "exercise_key": "SQUAT:GOBLET_SQUAT",
         "sets": 3, "reps": 10, "rest_seconds": 60,
         "superset_group": "anything", "transition_rest_seconds": 45},
    ]
    resp = c.post(f"/api/session/{session_id}/exercises", json=payload)
    assert resp.status_code == 200
    with db_module.get_session() as s:
        ex = s.query(SessionExercise).filter_by(program_session_id=session_id).one()
        assert ex.rest_seconds == 60
        assert not hasattr(ex, "transition_rest_seconds") or ex.transition_rest_seconds is None
        assert not hasattr(ex, "superset_group") or ex.superset_group is None


def test_plan_page_has_no_superset_or_transition_ui(client):
    """The /program page has no superset field, badge, data attribute, or transition rest input."""
    c, db_module = client
    from db import ProgramSession, SessionExercise, TrainingProgram
    with db_module.get_session() as s:
        program = TrainingProgram(name="A/B Full Body · 2 days", goal_tags='["full_body_2"]',
                                  status="active", active=True, source_type="curated_archetype",
                                  source_url="https://example.com")
        s.add(program); s.flush()
        ps = ProgramSession(program_id=program.id, name="Full Body 1", sport_type="strength_training", sequence_order=1)
        s.add(ps); s.flush()
        s.add(SessionExercise(
            program_session_id=ps.id, exercise_name="Bench Press", exercise_key="BENCH_PRESS",
            garmin_category="BENCH_PRESS", garmin_name="BENCH_PRESS", sets=3, reps=8,
            rest_seconds=90, order_index=0,
        ))
    page = c.get("/program?view=active")
    assert page.status_code == 200
    assert "ex-superset" not in page.text
    assert "superset-badge" not in page.text
    assert "data-superset-group" not in page.text
    assert "Superset" not in page.text
    assert "ex-transition-rest" not in page.text
    assert "Transition rest" not in page.text
    # Rest (sec) field is present
    assert "ex-rest" in page.text


def test_completed_multi_user_onboarding_renders_questionnaire(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from uuid import uuid4
    from app import get_onboarding
    import config
    from tenant_context import TenantIdentity, tenant_scope
    import sync.garmin_client
    from sync.garmin_registry import get_garmin_registry
    import db

    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    monkeypatch.setattr(sync.garmin_client.GarminClient, "is_authenticated", lambda self: True)
    monkeypatch.setattr(get_garmin_registry(), "get", lambda uid: sync.garmin_client.GarminClient())
    
    request = SimpleNamespace(
        state=SimpleNamespace(user=SimpleNamespace(id=str(uuid4()), onboarding_step="complete", status="active")),
        query_params={},
        url=SimpleNamespace(path="/onboarding"),
    )

    
    with tenant_scope(TenantIdentity(request.state.user.id)):
        with db.get_session() as s:
            from db import AthleteProfile
            s.add(AthleteProfile(id=1, onboarding_complete=True))

        response = get_onboarding(request)
        assert response.status_code == 200
        assert "onboarding.html" in response.template.name
