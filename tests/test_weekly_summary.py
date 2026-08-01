from datetime import date, datetime, timedelta

from db import Activity, ActivityProgramMatch, DailyHealth, ExerciseSet, NotificationOutbox, ProgramSession, TrainingProgram
from notify.outbox import deliver_notification, enqueue_notification
from notify.weekly_report import build_weekly_summary_report, render_weekly_summary


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
