from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from garminconnect import GarminConnectTooManyRequestsError

from db import Activity, DeviceCapability, MetricSnapshot, Sleep, SyncState, Workout
import pytest
import tenant_context
import sync.sync_service as svc


@pytest.fixture(autouse=True)
def bind_test_tenant():
    with tenant_context.tenant_scope(tenant_context.TenantIdentity("00000000-0000-0000-0000-000000000001")):
        yield


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


def test_parse_dt_accepts_garmin_epoch_milliseconds():
    ts = int(datetime(2026, 7, 4, 4, 30, tzinfo=timezone.utc).timestamp() * 1000)

    parsed = svc._parse_dt(ts)

    assert parsed == datetime(2026, 7, 4, 4, 30)


def test_sync_sleep_accepts_numeric_sleep_timestamps(session, monkeypatch):
    day = date(2026, 7, 4)
    start_ms = int(datetime(2026, 7, 3, 21, 30, tzinfo=timezone.utc).timestamp() * 1000)
    end_ms = int(datetime(2026, 7, 4, 4, 30, tzinfo=timezone.utc).timestamp() * 1000)
    monkeypatch.setattr(
        svc.client,
        "sleep",
        lambda day: {
            "dailySleepDTO": {
                "sleepStartTimestampGMT": start_ms,
                "sleepEndTimestampGMT": end_ms,
                "sleepTimeSeconds": 7 * 3600,
                "sleepScores": {"overall": {"value": 82}},
            }
        },
    )

    svc._sync_sleep(session, day)
    session.commit()

    row = session.get(Sleep, day)
    assert row.sleep_start_time == datetime(2026, 7, 4, 0, 30)
    assert row.sleep_end_time == datetime(2026, 7, 4, 7, 30)
    assert row.total_s == 7 * 3600
    assert row.score == 82


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


def test_forced_sync_bypasses_delta_skip_but_not_full_backfill(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = date.today()
    upload = datetime(2026, 7, 4, 6, 30, tzinfo=timezone.utc).isoformat(timespec="seconds")
    _state(session, "last_sync_through", today.isoformat())
    _state(session, "last_processed_device_upload", upload)
    _state(session, "last_seen_activity_id", "101")
    _state(session, "last_seen_activity_start", "2026-07-03 18:00:00")
    _state(session, "last_workouts_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))

    monkeypatch.setattr(svc.client, "device_last_used", lambda: _device_payload(datetime.fromisoformat(upload)))
    monkeypatch.setattr(
        svc.client,
        "recent_activities",
        lambda limit=1: [{"activityId": 101, "startTimeLocal": "2026-07-03 18:00:00"}],
    )

    seen = {"start": None, "sleep": 0}
    monkeypatch.setattr(svc, "_sync_activities", lambda session, start, end: seen.__setitem__("start", start) or 0)
    monkeypatch.setattr(svc, "_sync_sleep", lambda *args: seen.__setitem__("sleep", seen["sleep"] + 1))
    monkeypatch.setattr(svc, "_sync_daily_health", lambda *args: None)

    summary = svc.run_sync(full=False, force=True)

    assert summary["skipped"] is False
    assert seen["sleep"] > 0
    assert seen["start"] == today - timedelta(days=3)


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


def test_resource_cursors_fall_back_to_legacy_cursor(session):
    legacy = date.today() - timedelta(days=7)
    _state(session, "last_sync_through", legacy.isoformat())

    cursors = {resource: svc._resource_cursor(session, resource) for resource in svc._RESOURCE_CURSOR_KEYS}
    session.commit()

    assert cursors == {resource: legacy for resource in svc._RESOURCE_CURSOR_KEYS}
    for key in svc._RESOURCE_CURSOR_KEYS.values():
        assert session.get(SyncState, key).value == legacy.isoformat()
    assert session.get(SyncState, "last_sync_through").value == legacy.isoformat()


def test_successful_resources_advance_independent_cursors(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = date.today()
    for key in svc._RESOURCE_CURSOR_KEYS.values():
        _state(session, key, (today - timedelta(days=3)).isoformat())
    _state(session, "last_workouts_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    monkeypatch.setattr(svc, "_sync_activities", lambda *args: 2)
    monkeypatch.setattr(svc, "_sync_sleep", lambda *args: True)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda *args: True)

    summary = svc.run_sync(force=True)

    assert summary["activities"] == 2
    assert summary["days"] > 0
    for key in svc._RESOURCE_CURSOR_KEYS.values():
        assert session.get(SyncState, key).value == today.isoformat()


def test_activity_failure_does_not_block_sleep_or_health_cursors(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = date.today()
    activity_cursor = today - timedelta(days=3)
    for resource, key in svc._RESOURCE_CURSOR_KEYS.items():
        _state(session, key, activity_cursor.isoformat())
    _state(session, "last_workouts_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    monkeypatch.setattr(svc, "_sync_activities", lambda *args: (_ for _ in ()).throw(RuntimeError("activity failed")))
    monkeypatch.setattr(svc, "_sync_sleep", lambda *args: True)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda *args: True)

    svc.run_sync(force=True)

    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["activities"]).value == activity_cursor.isoformat()
    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["sleep"]).value == today.isoformat()
    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["daily_health"]).value == today.isoformat()


def test_first_429_stops_remaining_calls_and_preserves_partial_resource_progress(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = date.today()
    cursor = today - timedelta(days=3)
    for key in svc._RESOURCE_CURSOR_KEYS.values():
        _state(session, key, cursor.isoformat())
    _state(session, "last_workouts_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    monkeypatch.setattr(svc, "_sync_activities", lambda *args: 4)
    monkeypatch.setattr(svc, "_sync_sleep", lambda *args: True)
    health_calls = []

    def fail_on_first_new_health_day(_session, day):
        health_calls.append(day)
        if day == today - timedelta(days=1):
            raise GarminConnectTooManyRequestsError("too many")
        return True

    monkeypatch.setattr(svc, "_sync_daily_health", fail_on_first_new_health_day)

    summary = svc.run_sync(force=True)

    assert health_calls[-1] == today - timedelta(days=1)
    assert summary["activities"] == 4
    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["activities"]).value == today.isoformat()
    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["sleep"]).value == today.isoformat()
    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["daily_health"]).value == (today - timedelta(days=2)).isoformat()
    assert session.get(SyncState, "garmin_cooldown_until").value


def _stage1_client(monkeypatch, events):
    """Minimal Garmin fixture that records only bootstrap endpoint ordering."""
    monkeypatch.setattr(svc.client, "device_last_used", lambda: events.append("device") or {})
    monkeypatch.setattr(svc.client, "sleep", lambda day: events.append(("sleep", day)) or {"dailySleepDTO": {}})
    monkeypatch.setattr(svc.client, "training_readiness", lambda day: events.append(("readiness", day)) or {})
    monkeypatch.setattr(svc.client, "hrv", lambda day: events.append(("hrv", day)) or {})
    monkeypatch.setattr(svc.client, "resting_hr", lambda day: events.append(("rhr", day)) or {})
    monkeypatch.setattr(svc.client, "stress", lambda day: events.append(("stress", day)) or {})
    monkeypatch.setattr(svc.client, "body_battery", lambda start, end: events.append(("bb", start)) or [])
    monkeypatch.setattr(svc.client, "daily_steps", lambda start, end: events.append(("steps", start)) or [])
    monkeypatch.setattr(svc.client, "user_summary", lambda day: events.append(("summary", day)) or {})
    monkeypatch.setattr(svc.client, "fitness_age", lambda day: events.append(("fitness", day)) or {"fitnessAge": 35, "lastUpdated": day.isoformat()})
    monkeypatch.setattr(svc.client, "training_status", lambda day: events.append(("status", day)) or {})


def test_stage1_order_windows_and_strength_limit(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = date.today()
    events = []
    _stage1_client(monkeypatch, events)
    activities = [
        {"activityId": n, "duration": 1200, "startTimeLocal": f"{(today - timedelta(days=n)).isoformat()} 08:00:00",
         "activityType": {"typeKey": "strength_training" if n < 12 else "running"},
         "vO2MaxValue": 45 if n == 12 else None}
        for n in range(15)
    ]
    monkeypatch.setattr(svc.client, "activities_by_date", lambda start, end: events.append(("activities", start, end)) or activities)
    monkeypatch.setattr(svc.client, "exercise_sets", lambda activity_id: events.append(("sets", activity_id)) or {})
    session.add(DeviceCapability(metric="training_status", support_state="supported", evidence_source="test", updated_at=datetime.now()))
    session.commit()

    svc.run_sync()

    assert events[0] == "device"
    assert events[1] == ("sleep", today)
    assert events[2] == ("readiness", today)
    activity_event = next(event for event in events if event[0] == "activities")
    assert activity_event[1:] == (today - timedelta(days=29), today)
    sleep_days = [event[1] for event in events if event[0] == "sleep"]
    assert sleep_days == [today] + [today - timedelta(days=n) for n in range(6, 0, -1)]
    assert not any(event[0] in {"readiness", "status"} and event[1] != today for event in events if isinstance(event, tuple))
    assert [event[1] for event in events if event[0] == "sets"] == list(range(10))
    assert events.index(activity_event) < events.index(("sets", 0)) < events.index(("fitness", today)) < events.index(("status", today))
    assert session.get(SyncState, "stage1_bootstrap_complete").value == "complete"
    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["sleep"]).value == today.isoformat()
    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["daily_health"]).value == today.isoformat()
    assert session.get(SyncState, svc._RESOURCE_CURSOR_KEYS["activities"]).value == today.isoformat()


def test_stage1_skips_existing_progress_and_resumes_partial(session, monkeypatch):
    _wire_common(monkeypatch, session)
    _state(session, "last_sync_through", (date.today() - timedelta(days=1)).isoformat())
    starts = []
    monkeypatch.setattr(svc.client, "device_last_used", lambda: {})
    monkeypatch.setattr(svc.client, "recent_activities", lambda *_: [])
    monkeypatch.setattr(svc, "_sync_activities", lambda _s, start, _e, **_kwargs: starts.append(start) or 0)
    monkeypatch.setattr(svc, "_sync_sleep", lambda *_: True)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda *_, **__: True)
    svc.run_sync(force=True)
    assert starts != [date.today() - timedelta(days=29)]

    session.query(SyncState).delete()
    session.commit()
    events = []
    _stage1_client(monkeypatch, events)
    _state(session, "stage1_bootstrap_device", "complete")
    _state(session, "stage1_bootstrap_today_sleep", "complete")
    _state(session, "stage1_bootstrap_training_readiness", "complete")
    monkeypatch.setattr(svc.client, "activities_by_date", lambda *_: [])
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: {})
    svc.run_sync()
    assert "device" not in events
    assert ("sleep", date.today()) not in events
    assert session.get(SyncState, "stage1_bootstrap_complete").value == "complete"


def test_stage1_429_stops_and_completion_waits_for_remaining_work(session, monkeypatch):
    _wire_common(monkeypatch, session)
    events = []
    _stage1_client(monkeypatch, events)
    monkeypatch.setattr(
        svc.client, "training_readiness",
        lambda *_: (_ for _ in ()).throw(GarminConnectTooManyRequestsError("too many")),
    )

    summary = svc.run_sync()

    assert session.get(SyncState, "stage1_bootstrap_today_sleep").value == "complete"
    assert session.get(SyncState, "stage1_bootstrap_complete") is None
    assert session.get(SyncState, "garmin_cooldown_until").value
    assert not any(event[0] == "hrv" for event in events if isinstance(event, tuple))


def test_stage1_unsupported_optional_metrics_complete_without_calls(session, monkeypatch):
    _wire_common(monkeypatch, session)
    events = []
    _stage1_client(monkeypatch, events)
    monkeypatch.setattr(svc.client, "activities_by_date", lambda *_: [])
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: {})
    session.add(DeviceCapability(metric="training_readiness", support_state="unsupported", evidence_source="test", updated_at=datetime.now()))
    session.add(DeviceCapability(metric="training_status", support_state="unsupported", evidence_source="test", updated_at=datetime.now()))
    session.commit()

    svc.run_sync()

    assert not any(event[0] in {"readiness", "status"} for event in events if isinstance(event, tuple))
    assert session.get(SyncState, "stage1_bootstrap_complete").value == "complete"


def test_stage1_resume_persists_activity_summary_vo2_without_refetch(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = date.today()
    events = []
    _stage1_client(monkeypatch, events)
    activity_calls = []
    activities = [{
        "activityId": 1, "duration": 1200,
        "startTimeLocal": f"{(today - timedelta(days=2)).isoformat()} 08:00:00",
        "activityType": {"typeKey": "running"}, "vO2MaxValue": 47.2,
    }]
    monkeypatch.setattr(
        svc.client, "activities_by_date",
        lambda start, end: activity_calls.append((start, end)) or activities,
    )
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: {})
    monkeypatch.setattr(
        svc.client, "fitness_age",
        lambda *_: (_ for _ in ()).throw(GarminConnectTooManyRequestsError("too many")),
    )

    svc.run_sync()

    assert session.get(SyncState, "stage1_bootstrap_activities").value == "complete"
    assert session.get(SyncState, "stage1_bootstrap_vo2max_summary").value == f"{(today - timedelta(days=2)).isoformat()}|47.2"
    assert session.get(SyncState, "stage1_bootstrap_vo2max") is None
    svc._set_state(session, "garmin_cooldown_until", "")
    session.commit()
    monkeypatch.setattr(svc.client, "fitness_age", lambda day: {"fitnessAge": 35, "lastUpdated": day.isoformat()})

    svc.run_sync()

    assert len(activity_calls) == 1
    snapshot = session.get(MetricSnapshot, "vo2max")
    assert (snapshot.value_date, snapshot.value) == ((today - timedelta(days=2)).isoformat(), 47.2)
    assert session.get(SyncState, "stage1_bootstrap_vo2max").value == "complete"
    assert session.get(SyncState, "stage1_bootstrap_vo2max_summary") is None


def test_stage1_no_activity_summary_vo2_resolves_without_extra_request(session, monkeypatch):
    _wire_common(monkeypatch, session)
    events = []
    _stage1_client(monkeypatch, events)
    calls = []
    monkeypatch.setattr(svc.client, "activities_by_date", lambda *args: calls.append(args) or [])
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: {})

    svc.run_sync()

    assert len(calls) == 1
    assert session.get(MetricSnapshot, "vo2max") is None
    assert session.get(SyncState, "stage1_bootstrap_vo2max").value == "complete"
    assert session.get(SyncState, "stage1_bootstrap_vo2max_summary") is None


def test_stage1_vo2_marker_waits_for_snapshot_handling(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = date.today()
    _state(session, "stage1_bootstrap_device", "complete")
    _state(session, "stage1_bootstrap_today_sleep", "complete")
    _state(session, "stage1_bootstrap_training_readiness", "complete")
    _state(session, "stage1_bootstrap_sleep", "complete")
    _state(session, "stage1_bootstrap_daily_health", "complete")
    _state(session, "stage1_bootstrap_activities", "complete")
    _state(session, "stage1_bootstrap_strength_sets", "complete")
    _state(session, "stage1_bootstrap_fitness_age", "complete")
    _state(session, "stage1_bootstrap_vo2max_summary", f"{today.isoformat()}|47.2")
    monkeypatch.setattr(svc, "_upsert_snapshot", lambda *_: (_ for _ in ()).throw(RuntimeError("snapshot failed")))

    with pytest.raises(RuntimeError, match="snapshot failed"):
        svc._sync_stage1(session, today, {"activities": 0, "days": 0, "errors": []})

    assert session.get(SyncState, "stage1_bootstrap_vo2max") is None
    assert session.get(SyncState, "stage1_bootstrap_vo2max_summary").value == f"{today.isoformat()}|47.2"
