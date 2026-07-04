from contextlib import contextmanager
from datetime import date, datetime, timezone

from garminconnect import GarminConnectTooManyRequestsError

from db import SyncState, Workout
import sync.sync_service as svc


@contextmanager
def _bound_session(session):
    yield session
    session.commit()


def _state(session, key, value):
    session.add(SyncState(key=key, value=value))
    session.commit()


def _wire_common(monkeypatch, session):
    monkeypatch.setattr(svc, "get_session", lambda: _bound_session(session))
    monkeypatch.setattr(svc, "_snapshot_summary_metrics", lambda: None)
    monkeypatch.setattr("metrics.engine.recompute_all", lambda: None)
    monkeypatch.setattr(svc.coach, "generate_daily_suggestion", lambda session: None)


def _device_payload(dt):
    return {"lastUsedDeviceUploadTime": int(dt.timestamp() * 1000)}


def test_delta_sync_skips_when_device_and_activity_unchanged(session, monkeypatch):
    _wire_common(monkeypatch, session)
    upload = datetime(2026, 7, 4, 6, 30, tzinfo=timezone.utc).isoformat(timespec="seconds")
    _state(session, "last_sync_through", date.today().isoformat())
    _state(session, "last_processed_device_upload", upload)
    _state(session, "last_seen_activity_id", "101")
    _state(session, "last_seen_activity_start", "2026-07-03 18:00:00")

    monkeypatch.setattr(svc.client, "device_last_used", lambda: _device_payload(datetime.fromisoformat(upload)))
    monkeypatch.setattr(
        svc.client,
        "recent_activities",
        lambda limit=1: [{"activityId": 101, "startTimeLocal": "2026-07-03 18:00:00"}],
    )

    called = {"activities": 0}
    monkeypatch.setattr(svc, "_sync_activities", lambda *args: called.__setitem__("activities", 1) or 0)

    summary = svc.run_sync(full=False)

    assert summary["skipped"] is True
    assert called["activities"] == 0


def test_new_device_upload_triggers_normal_sync(session, monkeypatch):
    _wire_common(monkeypatch, session)
    old_upload = datetime(2026, 7, 4, 6, 30, tzinfo=timezone.utc).isoformat(timespec="seconds")
    new_upload_dt = datetime(2026, 7, 4, 7, 30, tzinfo=timezone.utc)
    _state(session, "last_sync_through", date.today().isoformat())
    _state(session, "last_processed_device_upload", old_upload)
    _state(session, "last_seen_activity_id", "101")
    _state(session, "last_seen_activity_start", "2026-07-03 18:00:00")
    _state(session, "last_workouts_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    monkeypatch.setattr(svc.client, "device_last_used", lambda: _device_payload(new_upload_dt))
    monkeypatch.setattr(
        svc.client,
        "recent_activities",
        lambda limit=1: [{"activityId": 101, "startTimeLocal": "2026-07-03 18:00:00"}],
    )

    called = {"sleep": 0}
    monkeypatch.setattr(svc, "_sync_activities", lambda *args: 0)
    monkeypatch.setattr(svc, "_sync_sleep", lambda *args: called.__setitem__("sleep", called["sleep"] + 1))
    monkeypatch.setattr(svc, "_sync_daily_health", lambda *args: None)

    summary = svc.run_sync(full=False)

    assert summary["skipped"] is False
    assert called["sleep"] > 0
    assert session.get(SyncState, "last_processed_device_upload").value == new_upload_dt.isoformat(timespec="seconds")


def test_new_latest_activity_triggers_activity_sync(session, monkeypatch):
    _wire_common(monkeypatch, session)
    upload = datetime(2026, 7, 4, 6, 30, tzinfo=timezone.utc).isoformat(timespec="seconds")
    _state(session, "last_sync_through", date.today().isoformat())
    _state(session, "last_processed_device_upload", upload)
    _state(session, "last_seen_activity_id", "101")
    _state(session, "last_seen_activity_start", "2026-07-03 18:00:00")
    _state(session, "last_workouts_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    monkeypatch.setattr(svc.client, "device_last_used", lambda: _device_payload(datetime.fromisoformat(upload)))
    monkeypatch.setattr(
        svc.client,
        "recent_activities",
        lambda limit=1: [{"activityId": 202, "startTimeLocal": "2026-07-04 08:00:00"}],
    )

    called = {"activities": 0}
    monkeypatch.setattr(svc, "_sync_activities", lambda *args: called.__setitem__("activities", 1) or 1)
    monkeypatch.setattr(svc, "_sync_sleep", lambda *args: None)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda *args: None)

    summary = svc.run_sync(full=False)

    assert summary["skipped"] is False
    assert called["activities"] == 1


def test_full_sync_bypasses_delta_preflight(session, monkeypatch):
    _wire_common(monkeypatch, session)
    session.add(Workout(workout_id=1, name="W", sport_type="strength_training", steps_json="[]"))
    session.commit()

    monkeypatch.setattr(svc.client, "device_last_used", lambda: _device_payload(datetime.now(timezone.utc)))
    monkeypatch.setattr(
        svc.client,
        "recent_activities",
        lambda limit=1: [{"activityId": 101, "startTimeLocal": "2026-07-03 18:00:00"}],
    )
    monkeypatch.setattr(svc, "_sync_activities", lambda *args: 0)
    monkeypatch.setattr(svc, "_sync_sleep", lambda *args: None)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda *args: None)
    monkeypatch.setattr(svc, "_sync_workouts", lambda *args: None)

    summary = svc.run_sync(full=True)

    assert summary["skipped"] is False


def test_preflight_429_sets_cooldown(session, monkeypatch):
    _wire_common(monkeypatch, session)
    _state(session, "last_sync_through", date.today().isoformat())
    monkeypatch.setattr(
        svc.client,
        "device_last_used",
        lambda: (_ for _ in ()).throw(GarminConnectTooManyRequestsError("too many")),
    )

    summary = svc.run_sync(full=False)

    assert summary["skipped"] is True
    assert session.get(SyncState, "garmin_cooldown_until").value


def test_cooldown_skips_before_garmin_calls(session, monkeypatch):
    _wire_common(monkeypatch, session)
    future = datetime(2099, 1, 1, tzinfo=timezone.utc).isoformat(timespec="seconds")
    _state(session, "garmin_cooldown_until", future)
    monkeypatch.setattr(svc.client, "device_last_used", lambda: (_ for _ in ()).throw(AssertionError("called")))

    summary = svc.run_sync(full=False)

    assert summary["skipped"] is True
