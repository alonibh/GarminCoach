import json
from datetime import date, datetime, timedelta
from datetime import timezone
import pytz

from sqlalchemy import create_engine, inspect, text

from coach.program_duration_review import (
    apply_program_duration_review_action,
    build_program_duration_review_facts,
    completion_context,
    enqueue_due_program_duration_review_notifications,
    materialize_program_duration_review_notification,
    reconcile_program_duration_review,
)
from notify.outbox import deliver_notification, process_due_notifications
from coach.program_policy import PROGRAM_POLICIES, validate_program_policies
from coach.programs import PROGRAMS
from db import (
    Activity, ActivityProgramMatch, NotificationOutbox, PlannedSession,
    ProgramCursor, ProgramDurationReview, ProgramSession, SessionExercise,
    TrainingProgram,
)


def _program(session, key="dumbbell_full_body_3", *, activated_at=datetime(2026, 1, 1, 9)):
    policy = PROGRAM_POLICIES[key]
    row = TrainingProgram(name="Reviewed Program", source_type="curated_archetype",
        source_url=policy.source_url, goal_tags=json.dumps([key]), active=True,
        status="active", activated_at=activated_at, created_at=activated_at)
    session.add(row); session.flush()
    sessions = []
    for index, name in enumerate(policy.session_names, 1):
        source = ProgramSession(program_id=row.id, name=name, sequence_order=index,
            session_role="coach_strength", is_custom=False, is_addon=False)
        session.add(source); session.flush(); sessions.append(source)
        session.add(SessionExercise(program_session_id=source.id, exercise_name=f"EX_{index}", order_index=1,
            sets=3, reps=8, weight_kg=50))
    session.add(ProgramCursor(program_id=row.id, next_program_session_id=sessions[0].id,
        policy_version=policy.version, created_at=activated_at, updated_at=activated_at))
    session.flush()
    return row, sessions


def test_duration_policy_is_exact_and_rejects_bad_values():
    expected = {
        "dumbbell_full_body_3": 8, "phul_4": 12, "dumbbell_upper_lower_4": 12,
        "barbell_no_rack_4": 8, "barbell_upper_lower_4": 10, "maul_5": 12,
        "dumbbell_split_5": 12, "built_different_ppl_6": 10,
        "muscle_mania_6": 10,
    }
    assert {key: item.source_duration_weeks for key, item in PROGRAM_POLICIES.items()
            if item.source_duration_weeks is not None} == expected
    validate_program_policies(PROGRAMS)


def test_facts_reconcile_due_boundary_and_custom_addon(session):
    program, sources = _program(session)
    session.add(ProgramSession(program_id=program.id, name="Optional Add On", sequence_order=99,
        session_role="coach_strength", is_custom=True, is_addon=True))
    facts = build_program_duration_review_facts(session, program)
    assert facts and facts.activated_local_date == date(2026, 1, 1)
    assert facts.due_on == date(2026, 2, 26)
    before = reconcile_program_duration_review(session, local_today=facts.due_on - timedelta(days=1), now_utc=datetime(2026, 2, 25, 12))
    assert before.status == "scheduled"
    due = reconcile_program_duration_review(session, local_today=facts.due_on, now_utc=datetime(2026, 2, 26, 12))
    assert due.id == before.id and due.status == "pending" and due.first_due_at == datetime(2026, 2, 26, 12)
    assert len(sources) == due.source_session_count


def test_only_custom_addons_can_coexist_with_exact_source_sessions(session):
    program, sources = _program(session)
    session.add(ProgramSession(program_id=program.id, name="Custom standalone", sequence_order=99,
        session_role="coach_strength", is_custom=True, is_addon=False))
    assert build_program_duration_review_facts(session, program) is None
    session.rollback()

    program, sources = _program(session)
    sources[0].is_custom = True
    assert build_program_duration_review_facts(session, program) is None
    session.rollback()

    program, _ = _program(session)
    session.add(ProgramSession(program_id=program.id, name="Non custom add on", sequence_order=99,
        session_role="coach_strength", is_custom=False, is_addon=True))
    assert build_program_duration_review_facts(session, program) is None


def test_naive_utc_anchor_and_identity_mismatch_fail_closed(session):
    program, sources = _program(session, activated_at=datetime(2026, 1, 1, 23, 30))
    facts = build_program_duration_review_facts(session, program)
    assert facts and facts.activated_local_date == date(2026, 1, 2)
    review = reconcile_program_duration_review(session, local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1))
    sources[0].name = "Changed source identity"
    assert build_program_duration_review_facts(session, program) is None
    assert reconcile_program_duration_review(session, local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1)) is None
    assert review.status == "superseded"


def test_completion_context_and_actions_do_not_change_training_rows(session):
    program, sources = _program(session)
    review = reconcile_program_duration_review(session, local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1))
    activity = Activity(id=2001, activity_type="strength_training", start_time=datetime(2026, 2, 1))
    session.add(activity); session.flush()
    session.add(ActivityProgramMatch(activity_id=activity.id, program_id=program.id,
        program_session_id=sources[0].id, match_method="test", policy_version="test", matched_at=datetime(2026, 2, 1)))
    planned = PlannedSession(program_session_id=sources[0].id, target_date=date(2026, 3, 2),
        garmin_workout_id=123, status="approved")
    session.add(planned); session.flush()
    context = completion_context(session, review)
    assert (context.matched_source_sessions, context.completed_source_cycles, context.next_session_name) == (1, 0, sources[0].name)
    exercise = session.query(SessionExercise).first(); cursor = session.get(ProgramCursor, program.id)
    baseline = (exercise.weight_kg, cursor.next_program_session_id, planned.garmin_workout_id, planned.status)
    assert apply_program_duration_review_action(session, review_id=review.id, fingerprint=review.review_fingerprint,
        action="deload_planned", local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1, 8)) == "applied"
    assert (exercise.weight_kg, cursor.next_program_session_id, planned.garmin_workout_id, planned.status) == baseline
    assert (review.status, review.decision) == ("resolved", "deload_planned")
    assert apply_program_duration_review_action(session, review_id=review.id, fingerprint=review.review_fingerprint,
        action="deload_planned", local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1, 9)) == "already"


def test_snooze_outbox_dedup_and_stale_delivery(session):
    program, _ = _program(session)
    review = reconcile_program_duration_review(session, local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1))
    assert enqueue_due_program_duration_review_notifications(session, local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1, 8)) == 1
    assert enqueue_due_program_duration_review_notifications(session, local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1, 8)) == 0
    outbox = session.query(NotificationOutbox).one()
    assert materialize_program_duration_review_notification(session, review_id=review.id,
        reminder_sequence=0, fingerprint=review.review_fingerprint)[1:] == (None, None)
    assert "reviewed source duration" in materialize_program_duration_review_notification(session,
        review_id=review.id, reminder_sequence=0, fingerprint=review.review_fingerprint)[0]
    assert apply_program_duration_review_action(session, review_id=review.id, fingerprint=review.review_fingerprint,
        action="snooze", local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1, 9)) == "applied"
    assert materialize_program_duration_review_notification(session, review_id=review.id,
        reminder_sequence=0, fingerprint=review.review_fingerprint) is None
    assert reconcile_program_duration_review(session, local_today=date(2026, 3, 8), now_utc=datetime(2026, 3, 8, 8)).status == "pending"
    assert enqueue_due_program_duration_review_notifications(session, local_today=date(2026, 3, 8), now_utc=datetime(2026, 3, 8, 8)) == 1
    assert session.query(NotificationOutbox).count() == 2 and outbox.event_type == "program_duration_review"


def test_duration_review_migration_is_tenant_schema_only_and_idempotent(tmp_path):
    import db
    engine = create_engine(f"sqlite:///{tmp_path / 'duration-review.db'}", future=True)
    db.init_db(engine); db.init_db(engine)
    with engine.connect() as conn:
        assert "program_duration_reviews" in inspect(engine).get_table_names()
        assert conn.execute(text("SELECT COUNT(*) FROM app_migrations WHERE migration_key = :key"),
            {"key": "program_duration_reviews_2026_08_01_v1"}).scalar_one() == 1
        assert conn.execute(text("PRAGMA foreign_key_list('program_duration_reviews')")).first() is not None
    engine.dispose()


def test_minute_poller_does_not_reconcile_or_enqueue_duration_reviews(session, monkeypatch):
    _program(session, activated_at=datetime(2025, 1, 1))
    from contextlib import contextmanager
    @contextmanager
    def current_session():
        yield session
    monkeypatch.setattr("notify.outbox.get_session", current_session)
    monkeypatch.setattr("notify.outbox.send_message", lambda *_args, **_kwargs: True)
    process_due_notifications(datetime(2026, 3, 1, 12))
    process_due_notifications(datetime(2026, 3, 1, 12))
    assert session.query(ProgramDurationReview).count() == 0
    assert session.query(NotificationOutbox).filter_by(event_type="program_duration_review").count() == 0


def test_duration_review_delivery_is_plain_once_and_stale_rows_cancel(session, monkeypatch):
    _program(session)
    review = reconcile_program_duration_review(session, local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1))
    enqueue_due_program_duration_review_notifications(session, local_today=date(2026, 3, 1), now_utc=datetime(2026, 3, 1, 8))
    outbox = session.query(NotificationOutbox).one()
    received = []
    monkeypatch.setattr("notify.outbox.send_message", lambda text, reply_markup=None, parse_mode=None: received.append((text, reply_markup)) or True)
    assert deliver_notification(session, outbox, datetime(2026, 3, 1, 8)) == "sent"
    assert review.notified_at and len(received) == 1 and received[0][1] is None
    assert "http" not in received[0][0] and str(review.id) not in received[0][0]
    assert deliver_notification(session, outbox, datetime(2026, 3, 1, 8)) == "retry"
    review.status = "snoozed"
    stale = NotificationOutbox(event_type="program_duration_review", due_at=datetime(2026, 3, 1, 8),
        payload_json=outbox.payload_json, idempotency_key="stale-duration", created_at=datetime(2026, 3, 1))
    session.add(stale); session.flush()
    assert deliver_notification(session, stale, datetime(2026, 3, 1, 8)) == "cancelled"


def test_aware_anchors_dst_and_cursor_fallback_fail_closed(session, monkeypatch):
    monkeypatch.setattr("coach.program_duration_review.get_local_tz", lambda: pytz.timezone("Asia/Jerusalem"))
    program, _ = _program(session, activated_at=datetime(2026, 1, 1, 22, 30, tzinfo=timezone.utc))
    assert build_program_duration_review_facts(session, program).activated_local_date == date(2026, 1, 2)
    program.activated_at = datetime(2026, 3, 27, 0, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert build_program_duration_review_facts(session, program).activated_local_date == date(2026, 3, 27)
    program.activated_at = None
    assert build_program_duration_review_facts(session, program) is not None
    session.get(ProgramCursor, program.id).policy_version = "wrong"
    assert build_program_duration_review_facts(session, program) is None
    session.delete(session.get(ProgramCursor, program.id))
    session.flush()
    assert build_program_duration_review_facts(session, program) is None


def test_replacement_supersedes_and_reactivation_has_new_fingerprint(session):
    program, _ = _program(session)
    first = reconcile_program_duration_review(session, local_today=date(2026, 1, 2), now_utc=datetime(2026, 1, 2))
    program.active = False; program.status = "archived"
    assert reconcile_program_duration_review(session, local_today=date(2026, 1, 3), now_utc=datetime(2026, 1, 3)) is None
    assert first.status == "superseded"
    program.active = True; program.status = "active"; program.activated_at = datetime(2026, 2, 1)
    second = reconcile_program_duration_review(session, local_today=date(2026, 2, 2), now_utc=datetime(2026, 2, 2))
    assert second.review_fingerprint != first.review_fingerprint and second.status == "scheduled"
