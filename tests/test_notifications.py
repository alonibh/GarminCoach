from contextlib import contextmanager
from datetime import date, datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from coach.interactions import apply_interaction, stage_calendar_conflict, stage_free_text_change
from db import (
    Activity,
    ActivityProgramMatch,
    Base,
    DailyHealth,
    ExerciseSet,
    NotificationOutbox,
    PendingInteraction,
    PlannedSession,
    Sleep,
)
from notify.outbox import (
    deliver_notification,
    enqueue_notification,
    enqueue_pre_workout_reminder,
    process_due_notifications,
)
from notify.weekly import build_weekly_summary


def _planned(session, *, target=date(2026, 7, 6), start="18:00"):
    row = PlannedSession(
        title="Upper Body",
        activity_type="strength_training",
        target_date=target,
        suggested_time=start,
        duration_min=60,
        status="planned",
        source="coach",
        created_at=datetime(2026, 7, 5, 10),
        updated_at=datetime(2026, 7, 5, 10),
    )
    session.add(row)
    session.flush()
    return row


def test_preworkout_reminder_is_exactly_one_hour_and_one_line(session, monkeypatch):
    planned = _planned(session)
    queued = enqueue_pre_workout_reminder(session, planned)
    assert queued.due_at == datetime(2026, 7, 6, 17, 0)
    sent = []
    monkeypatch.setattr("notify.outbox.send_message", lambda text, reply_markup=None: sent.append(text) or True)
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=2: {"events": [], "state": "fresh", "error": None},
    )

    outcome = deliver_notification(session, queued, datetime(2026, 7, 6, 17, 0))

    assert outcome == "sent"
    assert sent == ["Upper Body - 18:00"]
    assert "\n" not in sent[0]


def test_quiet_hour_reminder_is_deferred_then_revalidated(session, monkeypatch):
    planned = _planned(session, target=date(2026, 7, 7), start="00:00")
    queued = enqueue_pre_workout_reminder(session, planned)
    monkeypatch.setattr("notify.outbox.send_message", lambda *_args, **_kwargs: True)

    assert deliver_notification(session, queued, datetime(2026, 7, 6, 23, 0)) == "deferred"
    assert queued.due_at == datetime(2026, 7, 7, 7, 0)
    assert deliver_notification(session, queued, datetime(2026, 7, 7, 7, 0)) == "cancelled"
    assert queued.status == "cancelled"


def test_outbox_survives_new_sessions_and_sends_only_once(tmp_path, monkeypatch):
    db_file = (tmp_path / "outbox.sqlite").as_posix()
    engine = create_engine(f"sqlite:///{db_file}", future=True)
    Base.metadata.create_all(engine)
    TestSession = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def sessions():
        current = TestSession()
        try:
            yield current
            current.commit()
        finally:
            current.close()

    with sessions() as first_process:
        enqueue_notification(
            first_process,
            event_type="calendar_conflict",
            due_at=datetime(2026, 7, 6, 12),
            payload={"text": "Calendar conflict detected."},
            idempotency_key="restart-safe",
        )

    sent = []
    monkeypatch.setattr("notify.outbox.get_session", sessions)
    monkeypatch.setattr("notify.outbox.send_message", lambda text, reply_markup=None: sent.append(text) or True)
    now = datetime(2026, 7, 6, 12)

    assert process_due_notifications(now)["sent"] == 1
    assert process_due_notifications(now)["sent"] == 0
    assert sent == ["Calendar conflict detected."]
    with sessions() as third_process:
        assert third_process.query(NotificationOutbox).one().status == "sent"
    engine.dispose()


def test_calendar_conflict_offers_revalidated_buttons(session, monkeypatch):
    planned = _planned(session)
    queued = enqueue_pre_workout_reminder(session, planned)
    sent = []
    monkeypatch.setattr("notify.outbox.send_message", lambda text, reply_markup=None: sent.append((text, reply_markup)) or True)
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=2: {
            "events": [{"title": "Appointment", "start": "2026-07-06 17:45", "end": "18:30"}],
            "state": "fresh",
            "error": None,
        },
    )
    version = {"value": "calendar-v1"}
    monkeypatch.setattr("coach.interactions.calendar_version", lambda _session: version["value"])

    assert deliver_notification(session, queued, datetime(2026, 7, 6, 17)) == "sent"
    text, markup = sent[0]
    assert text == "Calendar conflict: Upper Body at 18:00 overlaps Appointment."
    assert [button["text"] for button in markup["inline_keyboard"][0]] == ["Keep time", "Reschedule"]
    keep = session.query(PendingInteraction).filter_by(action_type="keep_calendar_time").one()

    version["value"] = "calendar-v2"
    status, _ = apply_interaction(session, keep.interaction_id)
    assert status == "stale"
    assert keep.status == "superseded"


def test_reschedule_confirmation_rechecks_the_new_time(session, monkeypatch):
    planned = _planned(session)
    monkeypatch.setattr("coach.interactions.calendar_version", lambda _session: "calendar-v1")
    actions = stage_calendar_conflict(
        session,
        planned,
        {"title": "First conflict", "start": "2026-07-06 17:45", "end": "18:30"},
    )
    reschedule = next(row for row in actions if row.action_type == "request_reschedule")
    assert apply_interaction(session, reschedule.interaction_id)[0] == "awaiting_input"
    _text, confirmations = stage_free_text_change(session, "19:00")
    monkeypatch.setattr(
        "coach.calendar.get_upcoming_schedule_result",
        lambda days=2: {
            "events": [{"title": "Second conflict", "start": "2026-07-06 18:45", "end": "19:30"}],
            "state": "fresh",
            "error": None,
        },
    )

    status, message = apply_interaction(session, confirmations[0].interaction_id)

    assert status == "stale"
    assert "Second conflict" in message
    assert planned.suggested_time == "18:00"


def test_weekly_summary_uses_synced_same_rep_progression_without_acwr(session):
    previous = Activity(id=1, activity_type="strength_training", start_time=datetime(2026, 7, 2, 18))
    current = Activity(id=2, activity_type="strength_training", start_time=datetime(2026, 7, 9, 18))
    session.add_all([previous, current])
    session.flush()
    session.add_all([
        ExerciseSet(activity_id=1, set_index=1, set_type="ACTIVE", exercise_name="BENCH_PRESS", reps=8, weight_kg=80),
        ExerciseSet(activity_id=2, set_index=1, set_type="ACTIVE", exercise_name="BENCH_PRESS", reps=8, weight_kg=82.5),
        ExerciseSet(activity_id=2, set_index=2, set_type="ACTIVE", exercise_name="BENCH_PRESS", reps=5, weight_kg=100),
    ])

    summary = build_weekly_summary(session, date(2026, 7, 11))

    assert "Bench Press +2.5 kg at 8 reps" in summary
    assert "ACWR" not in summary
    assert "readiness" not in summary.lower()


def test_weekly_completion_uses_activity_date_not_late_reconciliation_date(session):
    from tests.test_program_state import _add_program

    program, source_sessions = _add_program(session)
    activity = Activity(
        id=44,
        activity_type="strength_training",
        start_time=datetime(2026, 7, 9, 18),
    )
    session.add(activity)
    session.flush()
    session.add(ActivityProgramMatch(
        activity_id=activity.id,
        program_id=program.id,
        program_session_id=source_sessions[0].id,
        match_method="garmin_workout_id",
        policy_version="2026-07-18.1",
        matched_at=datetime(2026, 7, 15, 8),
    ))

    summary = build_weekly_summary(session, date(2026, 7, 11))

    assert "Training: 1 of 2 program sessions completed." in summary
    assert "Unmatched strength activities" not in summary
    assert "not defined" not in summary


def test_weekly_summary_uses_plain_language_and_omits_sleep_stats(session):
    from tests.test_program_state import _add_program

    _add_program(session)
    session.add_all([
        Sleep(day=date(2026, 7, 11), total_s=6.9 * 3600, score=83),
        Sleep(day=date(2026, 7, 4), total_s=7.2 * 3600, score=81.4),
    ])

    summary = build_weekly_summary(session, date(2026, 7, 11))

    assert "Training: 0 of 2 program sessions completed." in summary
    assert "Sleep" not in summary
    assert "score 83" not in summary
    assert "Unmatched" not in summary
    assert "Uncompleted" not in summary
    assert "not defined" not in summary


def test_late_poor_readiness_update_is_daytime_only_and_idempotent(session, monkeypatch):
    from coach.decision_engine import evaluate_morning_decision
    from notify.morning import _enqueue_late_material_update
    from tests.test_decision_engine import TARGET, _fresh_readiness, _fresh_sleep
    from tests.test_program_state import _add_program

    _add_program(session)
    _fresh_sleep(session)
    _fresh_readiness(session, 60)
    first = evaluate_morning_decision(session, target=TARGET, evaluated_at=datetime(2026, 7, 6, 8))
    session.add(NotificationOutbox(
        event_type="morning_briefing",
        due_at=datetime(2026, 7, 6, 8),
        quiet_hour_policy="defer",
        payload_json="{}",
        decision_id=first.decision_id,
        status="sent",
        attempts=1,
        last_error="",
        idempotency_key="sent-brief",
        created_at=datetime(2026, 7, 6, 8),
        sent_at=datetime(2026, 7, 6, 8),
    ))
    session.get(DailyHealth, TARGET).training_readiness = 20
    monkeypatch.setattr("notify.morning.get_local_date", lambda: TARGET)
    monkeypatch.setattr("notify.morning.get_local_now", lambda: datetime(2026, 7, 6, 12))

    _enqueue_late_material_update(session)
    _enqueue_late_material_update(session)

    rows = session.query(NotificationOutbox).filter_by(event_type="late_material_update").all()
    assert len(rows) == 1
    assert "Skip" in rows[0].payload_json
