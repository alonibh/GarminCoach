from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

from garminconnect import GarminConnectTooManyRequestsError

from db import Activity, DeviceCapability, ExerciseSet, MetricSnapshot, Sleep, SyncState, Workout
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


def _stage2_strength_ready(session, anchor=None):
    anchor = anchor or date.today()
    _state(session, "stage1_bootstrap_complete", "complete")
    _state(session, "stage2_summary_backfill_complete", "complete")
    _state(session, "stage2_backfill_anchor_day", anchor.isoformat())
    return anchor


def _strength_activity(activity_id, when):
    return Activity(id=activity_id, activity_type="strength_training", start_time=when, duration_s=60)


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


def test_stage1_activity_summary_window_makes_no_hr_zone_calls(session, monkeypatch):
    _wire_common(monkeypatch, session)
    events = []
    _stage1_client(monkeypatch, events)
    monkeypatch.setattr(
        svc.client, "activities_by_date",
        lambda *_: [{"activityId": 1, "duration": 1200, "startTimeLocal": "2026-07-24 08:00:00",
                     "activityType": {"typeKey": "running"}}],
    )
    monkeypatch.setattr(svc.client, "hr_zones", lambda *_: (_ for _ in ()).throw(AssertionError("no HR zones")))
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: {})

    svc.run_sync()

    assert session.get(SyncState, "stage1_bootstrap_activities").value == "complete"


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


def test_stage2_is_scheduled_only_and_runs_one_no_change_wellness_unit(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = date.today()
    upload = datetime(2026, 7, 4, 6, 30, tzinfo=timezone.utc).isoformat(timespec="seconds")
    _state(session, "stage1_bootstrap_complete", "complete")
    for key in svc._RESOURCE_CURSOR_KEYS.values():
        _state(session, key, today.isoformat())
    _state(session, "last_processed_device_upload", upload)
    _state(session, "last_seen_activity_id", "101")
    _state(session, "last_seen_activity_start", "2026-07-03 18:00:00")
    monkeypatch.setattr(svc.client, "device_last_used", lambda: _device_payload(datetime.fromisoformat(upload)))
    monkeypatch.setattr(svc.client, "recent_activities", lambda limit=1: [{"activityId": 101, "startTimeLocal": "2026-07-03 18:00:00"}])
    calls = []
    monkeypatch.setattr(svc, "_sync_sleep", lambda _s, day: calls.append(("sleep", day)) or True)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda _s, day, *, current_optional: calls.append(("health", day, current_optional)) or True)

    svc.run_sync()
    assert calls == []

    summary = svc.run_sync(allow_backfill=True)
    assert summary["skipped"] is True
    assert calls == [("sleep", today - timedelta(days=7)), ("health", today - timedelta(days=7), False)]
    assert session.get(SyncState, "stage2_backfill_anchor_day").value == today.isoformat()


def test_stage2_fresh_journals_start_after_stage1_windows(session, monkeypatch):
    today = date.today()
    anchor = today - timedelta(days=3)
    _state(session, "stage1_bootstrap_complete", "complete")
    _state(session, "stage2_backfill_anchor_day", anchor.isoformat())
    calls = []
    monkeypatch.setattr(svc, "_sync_sleep", lambda _s, day: calls.append(("sleep", day)) or True)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda _s, day, **_kwargs: calls.append(("health", day)) or True)

    svc._run_stage2_summary_backfill(session, today, {"errors": [], "skipped": False})

    assert calls == [("sleep", anchor - timedelta(days=7)), ("health", anchor - timedelta(days=7))]
    assert session.get(SyncState, "stage2_activity_summary_next_gap").value == (anchor - timedelta(days=30)).isoformat()


def test_stage2_only_fetches_older_combined_coverage(session, monkeypatch):
    today = date.today()
    anchor = today - timedelta(days=3)
    _state(session, "stage1_bootstrap_complete", "complete")
    _state(session, "stage2_backfill_anchor_day", anchor.isoformat())
    wellness_days, ranges = [], []
    monkeypatch.setattr(svc, "_sync_sleep", lambda _s, day: wellness_days.append(day) or True)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda _s, day, **_kwargs: True)
    monkeypatch.setattr(svc, "_sync_activities", lambda _s, start, end, **kwargs: ranges.append((start, end, kwargs)) or 0)

    for _ in range(23):
        svc._run_stage2_summary_backfill(session, today, {"errors": [], "skipped": False})

    assert wellness_days == [anchor - timedelta(days=offset) for offset in range(7, 28)]
    assert ranges == [
        (anchor - timedelta(days=59), anchor - timedelta(days=30), {"enrich": False}),
        (anchor - timedelta(days=89), anchor - timedelta(days=60), {"enrich": False}),
    ]
    assert len(wellness_days) == 21
    assert sum((end - start).days + 1 for start, end, _ in ranges) == 60
    assert min(wellness_days) == anchor - timedelta(days=27)
    assert max(wellness_days) == anchor - timedelta(days=7)
    assert session.get(SyncState, "stage2_summary_backfill_complete").value == "complete"


def test_stage2_activity_summary_range_makes_no_hr_zone_calls(session, monkeypatch):
    today = date.today()
    anchor = today - timedelta(days=3)
    _state(session, "stage1_bootstrap_complete", "complete")
    _state(session, "stage2_backfill_anchor_day", anchor.isoformat())
    _state(session, "stage2_sleep_next_gap", "complete")
    _state(session, "stage2_daily_health_next_gap", "complete")
    _state(session, "stage2_activity_summary_next_gap", (anchor - timedelta(days=30)).isoformat())
    calls = []
    monkeypatch.setattr(
        svc.client, "activities_by_date",
        lambda start, end: calls.append((start, end)) or [{
            "activityId": 1, "duration": 1200, "startTimeLocal": "2026-06-24 08:00:00",
            "activityType": {"typeKey": "strength_training"},
        }],
    )
    monkeypatch.setattr(svc.client, "hr_zones", lambda *_: (_ for _ in ()).throw(AssertionError("no HR zones")))
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: (_ for _ in ()).throw(AssertionError("no sets")))

    svc._run_stage2_summary_backfill(session, today, {"errors": [], "skipped": False})

    assert len(calls) == 1


def test_stage2_activity_summary_range_429_sets_circuit_breaker(session, monkeypatch):
    today = date.today()
    anchor = today - timedelta(days=3)
    _state(session, "stage1_bootstrap_complete", "complete")
    _state(session, "stage2_backfill_anchor_day", anchor.isoformat())
    _state(session, "stage2_sleep_next_gap", "complete")
    _state(session, "stage2_daily_health_next_gap", "complete")
    activity_gap = anchor - timedelta(days=30)
    _state(session, "stage2_activity_summary_next_gap", activity_gap.isoformat())
    monkeypatch.setattr(
        svc.client, "activities_by_date",
        lambda *_: (_ for _ in ()).throw(GarminConnectTooManyRequestsError("too many")),
    )
    summary = {"errors": [], "skipped": False}

    svc._run_stage2_summary_backfill(session, today, summary)

    assert session.get(SyncState, "garmin_cooldown_until").value
    assert session.get(SyncState, "stage2_activity_summary_next_gap").value == activity_gap.isoformat()
    assert any("Rate limited on Stage 2 activities" in error for error in summary["errors"])


def test_stage2_normalizes_overlap_journals_before_one_actual_unit(session, monkeypatch):
    today = date.today()
    anchor = today - timedelta(days=3)
    _state(session, "stage1_bootstrap_complete", "complete")
    _state(session, "stage2_backfill_anchor_day", anchor.isoformat())
    _state(session, "stage2_sleep_next_gap", (anchor - timedelta(days=2)).isoformat())
    _state(session, "stage2_daily_health_next_gap", anchor.isoformat())
    _state(session, "stage2_activity_summary_next_gap", (anchor - timedelta(days=10)).isoformat())
    calls = []
    monkeypatch.setattr(svc, "_sync_sleep", lambda _s, day: calls.append(("sleep", day)) or True)
    monkeypatch.setattr(svc, "_sync_daily_health", lambda _s, day, **_kwargs: calls.append(("health", day)) or True)
    monkeypatch.setattr(svc, "_sync_activities", lambda *_args, **_kwargs: calls.append(("activities",)) or 0)

    svc._run_stage2_summary_backfill(session, today, {"errors": [], "skipped": False})

    assert calls == [("sleep", anchor - timedelta(days=7)), ("health", anchor - timedelta(days=7))]
    assert session.get(SyncState, "stage2_sleep_next_gap").value == (anchor - timedelta(days=8)).isoformat()
    assert session.get(SyncState, "stage2_daily_health_next_gap").value == (anchor - timedelta(days=8)).isoformat()
    assert session.get(SyncState, "stage2_activity_summary_next_gap").value == (anchor - timedelta(days=30)).isoformat()


def test_stage2_preserves_older_and_complete_journals(session, monkeypatch):
    today = date.today()
    anchor = today - timedelta(days=3)
    _state(session, "stage1_bootstrap_complete", "complete")
    _state(session, "stage2_backfill_anchor_day", anchor.isoformat())
    _state(session, "stage2_sleep_next_gap", (anchor - timedelta(days=12)).isoformat())
    _state(session, "stage2_daily_health_next_gap", "complete")
    _state(session, "stage2_activity_summary_next_gap", (anchor - timedelta(days=45)).isoformat())
    sleep_calls = []
    monkeypatch.setattr(svc, "_sync_sleep", lambda _s, day: sleep_calls.append(day) or True)

    svc._run_stage2_summary_backfill(session, today, {"errors": [], "skipped": False})

    assert sleep_calls == [anchor - timedelta(days=12)]
    assert session.get(SyncState, "stage2_daily_health_next_gap").value == "complete"
    assert session.get(SyncState, "stage2_activity_summary_next_gap").value == (anchor - timedelta(days=45)).isoformat()


def test_stage2_429_preserves_independent_wellness_progress(session, monkeypatch):
    today = date.today()
    _state(session, "stage1_bootstrap_complete", "complete")
    monkeypatch.setattr(svc, "_sync_sleep", lambda *_: True)
    monkeypatch.setattr(
        svc, "_sync_daily_health",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(GarminConnectTooManyRequestsError("too many")),
    )

    summary = {"errors": [], "skipped": False}
    svc._run_stage2_summary_backfill(session, today, summary)

    assert session.get(SyncState, "stage2_sleep_next_gap").value == (today - timedelta(days=8)).isoformat()
    assert session.get(SyncState, "stage2_daily_health_next_gap").value == (today - timedelta(days=7)).isoformat()
    assert session.get(SyncState, "garmin_cooldown_until").value
    assert session.get(SyncState, "stage2_summary_backfill_complete") is None


def test_stage2_strength_waits_for_summary_completion(session, monkeypatch):
    today = date.today()
    _state(session, "stage1_bootstrap_complete", "complete")
    _state(session, "stage2_backfill_anchor_day", today.isoformat())
    session.add(_strength_activity(1, datetime.combine(today, datetime.min.time())))
    session.commit()
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: (_ for _ in ()).throw(AssertionError("too early")))

    assert svc._run_stage2_strength_backfill(session, today, {"errors": []}) is False
    assert session.get(SyncState, "stage2_strength_candidate_ids") is None


def test_stage2_strength_candidates_are_fixed_ordered_and_exclude_existing_sets(session):
    anchor = _stage2_strength_ready(session)
    start = datetime.combine(anchor, datetime.min.time())
    activities = [_strength_activity(i, start - timedelta(days=i // 2)) for i in range(1, 25)]
    activities.extend([
        _strength_activity(30, start - timedelta(days=90)),
        Activity(id=31, activity_type="running", start_time=start, duration_s=60),
    ])
    session.add_all(activities)
    session.add(ExerciseSet(activity_id=1, set_index=0, edited=False))
    session.commit()

    candidates = svc._stage2_strength_candidates(session, anchor)

    expected = [
        activity.id for activity in sorted(
            activities[:24], key=lambda activity: (activity.start_time, activity.id), reverse=True
        ) if activity.id != 1
    ][:20]
    assert candidates == expected
    session.add(_strength_activity(99, start + timedelta(hours=1)))
    session.commit()
    assert svc._stage2_strength_candidates(session, anchor) == candidates


def test_stage2_strength_fresh_candidates_exclude_completion_markers(session):
    anchor = _stage2_strength_ready(session)
    start = datetime.combine(anchor, datetime.min.time())
    session.add_all([_strength_activity(1, start), _strength_activity(2, start - timedelta(days=1))])
    _state(session, "activity_strength_sets_checked:1", "complete")

    assert svc._stage2_strength_candidates(session, anchor) == [2]


def test_stage2_strength_persisted_candidate_snapshot_is_not_reselected(session, monkeypatch):
    anchor = _stage2_strength_ready(session)
    start = datetime.combine(anchor, datetime.min.time())
    session.add(_strength_activity(1, start))
    session.commit()
    assert svc._stage2_strength_candidates(session, anchor) == [1]
    _state(session, "activity_strength_sets_checked:1", "complete")
    calls = []
    monkeypatch.setattr(svc.client, "exercise_sets", lambda activity_id: calls.append(activity_id) or {"exerciseSets": []})

    svc._run_stage2_strength_backfill(session, anchor, {"errors": []})
    assert calls == []


def test_stage2_strength_one_successful_request_per_run_and_completion(session, monkeypatch):
    anchor = _stage2_strength_ready(session)
    session.add_all([_strength_activity(2, datetime.combine(anchor, datetime.min.time())), _strength_activity(1, datetime.combine(anchor, datetime.min.time()))])
    session.commit()
    calls = []
    monkeypatch.setattr(svc.client, "exercise_sets", lambda activity_id: calls.append(activity_id) or {"exerciseSets": []})

    svc._run_stage2_strength_backfill(session, anchor, {"errors": []})
    assert calls == [2]
    assert session.get(SyncState, "stage2_strength_next_index").value == "1"
    assert session.get(SyncState, "stage2_strength_backfill_complete") is None
    svc._run_stage2_strength_backfill(session, anchor, {"errors": []})
    assert calls == [2, 1]
    assert session.get(SyncState, "stage2_strength_backfill_complete").value == "complete"


def test_stage2_strength_nonempty_and_empty_successes_both_advance(session, monkeypatch):
    anchor = _stage2_strength_ready(session)
    session.add_all([_strength_activity(2, datetime.combine(anchor, datetime.min.time())), _strength_activity(1, datetime.combine(anchor, datetime.min.time()))])
    session.commit()
    monkeypatch.setattr(svc.client, "exercise_sets", lambda activity_id: {"exerciseSets": [] if activity_id == 1 else [{"weight": 10000, "exercises": [{"name": "Squat"}]}]})

    svc._run_stage2_strength_backfill(session, anchor, {"errors": []})
    assert session.get(SyncState, "stage2_strength_next_index").value == "1"
    assert session.query(ExerciseSet).filter_by(activity_id=2).count() == 1
    svc._run_stage2_strength_backfill(session, anchor, {"errors": []})
    assert session.get(SyncState, "stage2_strength_backfill_complete").value == "complete"


def test_stage2_strength_failures_and_429_retain_same_candidate(session, monkeypatch):
    anchor = _stage2_strength_ready(session)
    session.add(_strength_activity(1, datetime.combine(anchor, datetime.min.time())))
    session.commit()
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: None)
    summary = {"errors": []}
    svc._run_stage2_strength_backfill(session, anchor, summary)
    assert session.get(SyncState, "stage2_strength_next_index").value == "0"
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: (_ for _ in ()).throw(GarminConnectTooManyRequestsError("too many")))
    svc._run_stage2_strength_backfill(session, anchor, summary)
    assert session.get(SyncState, "stage2_strength_next_index").value == "0"
    assert session.get(SyncState, "garmin_cooldown_until").value


def test_stage2_strength_empty_candidates_complete_without_garmin(session, monkeypatch):
    anchor = _stage2_strength_ready(session)
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: (_ for _ in ()).throw(AssertionError("no candidate request")))

    svc._run_stage2_strength_backfill(session, anchor, {"errors": []})
    assert session.get(SyncState, "stage2_strength_candidate_ids").value == "[]"
    assert session.get(SyncState, "stage2_strength_backfill_complete").value == "complete"


def test_stage2_strength_is_excluded_from_manual_priority_force_and_full(session, monkeypatch):
    _wire_common(monkeypatch, session)
    today = _stage2_strength_ready(session)
    for key in svc._RESOURCE_CURSOR_KEYS.values():
        _state(session, key, today.isoformat())
    _state(session, "last_workouts_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    calls = []
    monkeypatch.setattr(svc, "_run_stage2_strength_backfill", lambda *_: calls.append("strength"))
    monkeypatch.setattr(svc, "_preflight", lambda *_: {"device_changed": False, "activity_changed": False, "device_upload": None})
    monkeypatch.setattr(svc, "_sync_activities", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(svc, "_sync_resource_days", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(svc, "_workouts_due", lambda *_: False)

    svc.run_sync()
    monkeypatch.setattr(svc, "run_priority_sync", lambda: {})
    svc.run_priority_sync()
    svc.run_sync(force=True, allow_backfill=True)
    svc.run_sync(full=True, allow_backfill=True)
    assert calls == []


def test_stage1_strength_marker_waits_for_set_request_success(session, monkeypatch):
    today = date.today()
    for name in ("device", "today_sleep", "training_readiness", "sleep", "daily_health", "activities"):
        _state(session, f"stage1_bootstrap_{name}", "complete")
    session.add(_strength_activity(1, datetime.combine(today, datetime.min.time())))
    session.commit()
    monkeypatch.setattr(svc, "_sync_exercise_sets", lambda *_: False)

    assert svc._sync_stage1(session, today, {"activities": 0, "days": 0, "errors": []}) is False
    assert session.get(SyncState, "stage1_bootstrap_strength_sets") is None


def test_exercise_set_sync_protects_edited_rows_and_returns_success(session, monkeypatch):
    activity = _strength_activity(1, datetime.now())
    session.add(activity)
    session.flush()
    session.add(ExerciseSet(activity_id=1, set_index=0, exercise_name="Edited", edited=True))
    session.commit()
    monkeypatch.setattr(svc.client, "exercise_sets", lambda *_: {"exerciseSets": [{"weight": 20000, "exercises": [{"name": "Garmin"}]}]})

    assert svc._sync_exercise_sets(session, 1) is True
    assert session.query(ExerciseSet).filter_by(activity_id=1, set_index=0).one().exercise_name == "Edited"
