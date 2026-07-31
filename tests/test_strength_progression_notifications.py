from datetime import datetime

from sqlalchemy import create_engine, text
from sqlalchemy import inspect

from coach.strength_progression_integration import (
    RecalculationCause, process_activity_recalculation,
)
from coach.strength_progression_notifications import (
    bridge_pending_progression_notifications, materialize_progression_summary,
    record_material_proposals,
)
from db import (
    Activity, ActivityProgramMatch, ExerciseSet, NotificationOutbox, ProgramSession,
    SessionExercise, StrengthProgressionNotificationBatch,
    StrengthProgressionNotificationReceipt, StrengthProgressionPolicy, SyncState,
    TrainingProgram,
)


def _seed(session):
    policy = StrengthProgressionPolicy(policy_version="strength-progression-v1", global_increment_grams=2500,
        weight_quantum_grams=250, required_consecutive=2, evidence_window_days=35, is_active=True)
    program = TrainingProgram(name="P", active=True, status="active")
    session.add_all((policy, program)); session.flush()
    planned = ProgramSession(program_id=program.id, name="A", sequence_order=0)
    session.add(planned); session.flush()
    exercise = SessionExercise(program_session_id=planned.id, exercise_name="Bench Press", exercise_key="BENCH",
        garmin_category="BENCH", garmin_name="BENCH", sets=2, reps=8, weight_kg=70, order_index=0)
    session.add(exercise); session.flush()
    return program, planned


def _activity(session, identifier, when, program, planned):
    row = Activity(id=identifier, activity_type="strength_training", start_time=when)
    session.add(row); session.flush()
    session.add(ActivityProgramMatch(activity_id=row.id, program_id=program.id, program_session_id=planned.id,
        match_method="test", policy_version="test", matched_at=when))
    for index in range(2):
        session.add(ExerciseSet(activity_id=row.id, set_index=index, set_type="ACTIVE", exercise_category="BENCH",
            exercise_name="BENCH", reps=8, weight_kg=70, edited=False))
    session.add(SyncState(key=f"activity_strength_sets_checked:{row.id}", value="complete"))
    session.flush()
    return row


def test_material_batch_receipt_bridge_and_plain_summary(session):
    program, planned = _seed(session)
    first = _activity(session, 400, datetime(2026, 1, 1), program, planned)
    second = _activity(session, 401, datetime(2026, 1, 8), program, planned)
    process_activity_recalculation(session, first.id, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    report = process_activity_recalculation(session, second.id, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    assert len(report.material_proposal_changes) == 1

    recorded = record_material_proposals(session, boundary_id=report.boundary_id,
        changes=report.material_proposal_changes, now=datetime(2026, 1, 8, 12))
    assert recorded.receipts_created == 1
    assert record_material_proposals(session, boundary_id=report.boundary_id,
        changes=report.material_proposal_changes, now=datetime(2026, 1, 8, 12)).receipts_created == 0
    bridge = bridge_pending_progression_notifications(session, now=datetime(2026, 1, 8, 12))
    assert bridge.bridged_batch_ids == (recorded.batch_id,)
    assert session.query(NotificationOutbox).filter_by(event_type="strength_progression_ready").count() == 1
    assert session.query(StrengthProgressionNotificationReceipt).count() == 1
    batch = session.get(StrengthProgressionNotificationBatch, recorded.batch_id)
    message = materialize_progression_summary(session, batch_id=batch.batch_id, now=datetime(2026, 1, 8, 12))
    assert message and message.parse_mode is None and message.reply_markup is None
    assert "Bench Press: 70 kg → 72.5 kg" in message.text
    assert "GarminCoach → Progression" in message.text


def test_notification_migration_creates_validated_schema_idempotently(tmp_path):
    import db

    engine = create_engine(f"sqlite:///{tmp_path / 'phase4c-upgrade.db'}", future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE durable_phase4c_row (value TEXT)"))
        conn.execute(text("INSERT INTO durable_phase4c_row VALUES ('keep')"))
    db.init_db(engine); db.init_db(engine)
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA integrity_check")).scalar_one() == "ok"
        assert conn.execute(text("PRAGMA foreign_keys")).scalar_one() == 1
        assert conn.execute(text("SELECT value FROM durable_phase4c_row")).scalar_one() == "keep"
        assert conn.execute(text("SELECT COUNT(*) FROM app_migrations WHERE migration_key = :key"), {
            "key": "strength_progression_telegram_notifications_2026_07_31_v1",
        }).scalar_one() == 1
        tables = set(inspect(engine).get_table_names())
        assert {"strength_progression_notification_batches", "strength_progression_notification_receipts"} <= tables
        receipt_indexes = {row[1] for row in conn.execute(text("PRAGMA index_list('strength_progression_notification_receipts')"))}
        assert "ix_strength_progression_notification_receipts_material_fingerprint" in receipt_indexes
    engine.dispose()


def test_notification_migration_does_not_write_marker_when_validation_fails(tmp_path, monkeypatch):
    import db

    engine = create_engine(f"sqlite:///{tmp_path / 'validation-fail.db'}", future=True)
    monkeypatch.setattr(db, "_validate_strength_progression_telegram_notifications",
                        lambda _conn: (_ for _ in ()).throw(RuntimeError("forced")))
    try:
        db.init_db(engine)
    except RuntimeError as exc:
        assert str(exc) == "forced"
    with engine.connect() as conn:
        assert conn.execute(text("SELECT COUNT(*) FROM app_migrations WHERE migration_key = :key"), {
            "key": "strength_progression_telegram_notifications_2026_07_31_v1",
        }).scalar_one() == 0
    engine.dispose()
