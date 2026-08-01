from datetime import date, datetime, timedelta

import json

from db import Activity, ActivityProgramMatch, DailyHealth, ExerciseSet, NotificationOutbox, PlannedSession, ProgramCursor, ProgramSession, TrainingProgram
from notify.outbox import deliver_notification, enqueue_notification
from notify.weekly_report import (
    WeeklySummaryStaleError, WeeklySummaryValidationError, build_weekly_summary_report,
    render_weekly_summary, validate_week_end, weekly_overnight_ready,
)


END = date(2026, 7, 11)
NOW = datetime(2026, 7, 11, 20)


def _report(session):
    return build_weekly_summary_report(
        session, week_end=END, generated_at=NOW, overnight_today_ready=False,
    )


def _program(session, *, days=2):
    program = TrainingProgram(name="Routine", active=True, status="active", days_per_week=days)
    session.add(program)
    session.flush()
    sessions = [ProgramSession(program_id=program.id, name=f"Day {index}", session_role="coach_strength", is_addon=False)
                for index in (1, 2)]
    session.add_all(sessions)
    session.flush()
    return program, sessions


def test_window_is_exactly_seven_days_and_uses_activity_date(session):
    program, sessions = _program(session)
    session.add_all([
        Activity(id=1, activity_type="strength_training", start_time=datetime(2026, 7, 5)),
        Activity(id=2, activity_type="strength_training", start_time=datetime(2026, 7, 11, 23, 59, 59)),
        Activity(id=3, activity_type="strength_training", start_time=datetime(2026, 7, 12)),
    ])
    session.flush()
    session.add_all([
        ActivityProgramMatch(activity_id=1, program_id=program.id, program_session_id=sessions[0].id,
                             match_method="exact", policy_version="test", matched_at=datetime(2026, 7, 20)),
        ActivityProgramMatch(activity_id=2, program_id=program.id, program_session_id=sessions[1].id,
                             match_method="exact", policy_version="test", matched_at=datetime(2026, 7, 20)),
    ])
    report = _report(session)
    assert (report.week_start, report.week_end) == (date(2026, 7, 5), END)
    assert report.training.program_completed == 2
    assert report.training.total_activities == 2


def test_active_program_target_movement_and_unmatched_strength(session):
    program, sessions = _program(session, days=3)
    session.add_all([
        Activity(id=10, activity_type="strength_training", start_time=datetime(2026, 7, 8), duration_s=3600),
        Activity(id=11, activity_type="running", start_time=datetime(2026, 7, 9), duration_s=float("nan")),
    ])
    session.flush()
    session.add(ActivityProgramMatch(activity_id=10, program_id=program.id, program_session_id=sessions[0].id,
                                     match_method="exact", policy_version="test", matched_at=NOW))
    session.add_all([
        DailyHealth(day=date(2026, 7, 5), steps=0, daily_moderate_intensity_minutes=0),
        DailyHealth(day=date(2026, 7, 6), steps=1200, daily_vigorous_intensity_minutes=15),
    ])
    report = _report(session)
    assert report.training.program_target == 3
    assert report.training.unmatched_strength == 0
    assert report.training.total_duration_minutes == 60
    assert [(d.label, d.count) for d in report.training.activity_domains] == [("running", 1), ("strength", 1)]
    assert report.movement.steps_total == 1200 and report.movement.steps_valid_days == 2
    assert (report.movement.moderate_minutes, report.movement.vigorous_minutes, report.movement.intensity_valid_days) == (0, 15, 2)


def test_strength_highlight_requires_active_same_reps_and_prior_week(session):
    session.add_all([
        Activity(id=20, activity_type="strength_training", start_time=datetime(2026, 7, 2)),
        Activity(id=21, activity_type="strength_training", start_time=datetime(2026, 7, 9)),
    ])
    session.flush()
    session.add_all([
        ExerciseSet(activity_id=20, set_index=1, set_type="ACTIVE", exercise_name="BENCH_PRESS", reps=8, weight_kg=80),
        ExerciseSet(activity_id=21, set_index=1, set_type="ACTIVE", exercise_name="BENCH_PRESS", reps=8, weight_kg=82.5),
        ExerciseSet(activity_id=21, set_index=2, set_type="REST", exercise_name="BENCH_PRESS", reps=8, weight_kg=200),
        ExerciseSet(activity_id=21, set_index=3, set_type="ACTIVE", exercise_name="BENCH_PRESS", reps=5, weight_kg=100),
    ])
    highlight = _report(session).strength_highlights
    assert len(highlight) == 1
    assert (highlight[0].reps, highlight[0].current_weight_kg, highlight[0].delta_kg) == (8, 82.5, 2.5)


def test_renderer_plain_text_bounds_and_footer(session):
    report = _report(session)
    text = render_weekly_summary(report)
    assert text.startswith("Weekly summary")
    assert text.endswith("Informational only; this summary does not change your workout.")
    assert len([line for line in text.splitlines() if line]) <= 18
    assert len(text) <= 3500
    assert "*" not in text and "[" not in text


def test_builder_rejects_future_week_and_does_not_commit(session, monkeypatch):
    committed = []
    monkeypatch.setattr(session, "commit", lambda: committed.append(True))
    try:
        build_weekly_summary_report(session, week_end=END + timedelta(days=1), generated_at=NOW,
                                    overnight_today_ready=False)
    except ValueError:
        pass
    else:
        raise AssertionError("future week must be rejected")
    assert committed == []


def test_outbox_materializes_weekly_as_plain_transport_and_cancels_stale(session, monkeypatch):
    row = enqueue_notification(session, event_type="weekly_summary", due_at=NOW,
                               payload={"week_end": END.isoformat()}, idempotency_key="weekly:2026-07-11")
    sent = []
    monkeypatch.setattr("notify.outbox.send_message", lambda text, reply_markup=None, parse_mode=None: sent.append((text, reply_markup, parse_mode)) or True)
    assert deliver_notification(session, row, NOW) == "sent"
    assert sent[0][1:] == (None, None)
    stale = enqueue_notification(session, event_type="weekly_summary", due_at=NOW,
                                 payload={"week_end": END.isoformat()}, idempotency_key="weekly:stale")
    assert deliver_notification(session, stale, datetime(2026, 7, 18, 9)) == "cancelled"


def test_weekly_modules_do_not_directly_deliver_or_call_external_services():
    weekly = open("notify/weekly.py", encoding="utf-8").read()
    report = open("notify/weekly_report.py", encoding="utf-8").read()
    assert "process_due_notifications" not in weekly
    assert "send_message" not in weekly and "send_message" not in report
    assert "GarminClient" not in report and "Gemini" not in report


def test_week_end_validation_requires_a_current_saturday_and_canonical_date():
    assert validate_week_end(END, local_day=END) == END
    for offset in range(1, 7):
        try:
            validate_week_end(END - timedelta(days=offset), local_day=END)
        except WeeklySummaryValidationError:
            pass
        else:
            raise AssertionError("non-Saturday accepted")
    for value in (datetime(2026, 7, 11), None, "2026-07-11", [], 1, {}):
        try:
            validate_week_end(value, local_day=END)
        except WeeklySummaryValidationError:
            pass
        else:
            raise AssertionError("non-date accepted")
    try:
        validate_week_end(END, local_day=END + timedelta(days=7))
    except WeeklySummaryStaleError:
        pass
    else:
        raise AssertionError("stale Saturday accepted")


def test_cursor_next_lookup_is_read_only_and_stale_cursor_is_omitted(session):
    program, sessions = _program(session)
    cursor = ProgramCursor(program_id=program.id, next_program_session_id=sessions[0].id,
                           last_completed_program_session_id=None, last_completed_activity_id=None,
                           last_completed_at=None, policy_version="unchanged",
                           created_at=NOW, updated_at=NOW)
    session.add(cursor)
    session.commit()
    before = (cursor.program_id, cursor.next_program_session_id, cursor.policy_version, cursor.created_at, cursor.updated_at)
    assert _report(session).next_session_name == "Day 1"
    assert render_weekly_summary(_report(session))
    session.refresh(cursor)
    assert before == (cursor.program_id, cursor.next_program_session_id, cursor.policy_version, cursor.created_at, cursor.updated_at)
    other = TrainingProgram(name="Other", active=False, status="inactive")
    session.add(other)
    session.flush()
    other_session = ProgramSession(program_id=other.id, name="Other day", session_role="coach_strength")
    session.add(other_session)
    session.flush()
    cursor.next_program_session_id = other_session.id
    session.commit()
    assert _report(session).next_session_name is None
    assert cursor.next_program_session_id == other_session.id


def test_incomplete_excludes_rest_recovery_optional_and_unmatched_is_global(session):
    program, sessions = _program(session)
    old = TrainingProgram(name="Old", active=False, status="inactive")
    session.add(old)
    session.flush()
    optional = ProgramSession(program_id=program.id, name="Optional", session_role="optional_recovery")
    session.add(optional)
    session.flush()
    session.add_all([
        PlannedSession(title="Normal", activity_type="strength_training", intensity="normal", target_date=END, status="planned"),
        PlannedSession(title="Rest", activity_type="rest", intensity="normal", target_date=END, status="planned"),
        PlannedSession(title="Recovery", activity_type="walking", intensity="recovery", target_date=END, status="planned"),
        PlannedSession(title="Optional", activity_type="walking", intensity="normal", target_date=END, status="planned", program_session_id=optional.id),
        PlannedSession(title="Done", activity_type="strength_training", intensity="normal", target_date=END, status="completed"),
    ])
    session.add(Activity(id=90, activity_type="strength_training", start_time=datetime(2026, 7, 10)))
    session.flush()
    session.add(ActivityProgramMatch(activity_id=90, program_id=old.id, program_session_id=sessions[0].id,
                                     match_method="old", policy_version="test", matched_at=NOW))
    report = _report(session)
    assert report.training.incomplete_planned == 1
    assert report.training.program_completed == 0
    assert report.training.unmatched_strength == 0


def test_historical_overnight_and_malformed_payload_cancel_without_telegram(session, monkeypatch):
    assert weekly_overnight_ready(session, week_end=END, local_delivery_day=END + timedelta(days=1)) is True
    sent = []
    monkeypatch.setattr("notify.outbox.send_message", lambda *_args, **_kwargs: sent.append(True) or True)
    for index, payload in enumerate((None, [], "bad", 1, {"week_end": []}, {"week_end": "2026-07-12"})):
        row = NotificationOutbox(event_type="weekly_summary", due_at=NOW, quiet_hour_policy="allow",
                                 payload_json=json.dumps(payload), status="pending", attempts=0,
                                 idempotency_key=f"bad-weekly:{index}", created_at=NOW)
        session.add(row)
        session.flush()
        assert deliver_notification(session, row, NOW) == "cancelled"
    assert sent == []


def test_unknown_exercises_need_specific_identity_and_remain_deterministic(session):
    session.add_all([
        Activity(id=120, activity_type="strength_training", start_time=datetime(2026, 7, 2)),
        Activity(id=121, activity_type="strength_training", start_time=datetime(2026, 7, 9)),
    ])
    session.flush()
    session.add_all([
        ExerciseSet(activity_id=120, set_index=1, set_type="ACTIVE", exercise_name="Exercise", reps=8, weight_kg=50),
        ExerciseSet(activity_id=121, set_index=1, set_type="ACTIVE", exercise_name="Unknown", reps=8, weight_kg=80),
        ExerciseSet(activity_id=120, set_index=2, set_type="ACTIVE", exercise_category="Custom A", exercise_name="Lift", reps=8, weight_kg=50),
        ExerciseSet(activity_id=121, set_index=2, set_type="ACTIVE", exercise_category="Custom B", exercise_name="Lift", reps=8, weight_kg=80),
        ExerciseSet(activity_id=120, set_index=3, set_type="ACTIVE", exercise_category="Custom", exercise_name="Cable Thing", reps=10, weight_kg=20),
        ExerciseSet(activity_id=121, set_index=3, set_type="ACTIVE", exercise_category="Custom", exercise_name="Cable Thing", reps=10, weight_kg=22.5),
    ])
    highlights = _report(session).strength_highlights
    assert [(item.label, item.reps, item.delta_kg) for item in highlights] == [("Cable Thing", 10, 2.5)]


def test_unexpected_weekly_materialization_rolls_back_and_does_not_block_other_row(session, monkeypatch):
    from sqlalchemy import text
    import notify.outbox as outbox

    weekly = enqueue_notification(session, event_type="weekly_summary", due_at=NOW,
                                  payload={"week_end": END.isoformat()}, idempotency_key="weekly:broken")
    other = enqueue_notification(session, event_type="calendar_conflict", due_at=NOW,
                                 payload={"text": "Other notification"}, idempotency_key="other:due")
    original = outbox._materialize

    def materialize_with_real_db_failure(current, row, now):
        if row.id == weekly.id:
            current.execute(text("SELECT * FROM no_such_weekly_table"))
        return original(current, row, now)

    sent = []
    monkeypatch.setattr(outbox, "_materialize", materialize_with_real_db_failure)
    monkeypatch.setattr(outbox, "send_message", lambda text, **_kwargs: sent.append(text) or True)
    assert outbox.deliver_notification(session, weekly, NOW) == "retry"
    session.refresh(weekly)
    assert weekly.status == "pending" and weekly.last_error == "weekly_materialization_failed"
    assert outbox.deliver_notification(session, other, NOW) == "sent"
    assert sent == ["Other notification"]
