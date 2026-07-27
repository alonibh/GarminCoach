from datetime import date, datetime
from contextlib import contextmanager

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectTooManyRequestsError

from db import Activity, ExerciseSet, SyncState
import sync.sync_service as svc


def _running(activity_id=1, **overrides):
    data = {
        "activityId": activity_id,
        "activityType": {"typeKey": "running"},
        "duration": 1800,
        "startTimeLocal": "2026-07-24 10:00:00",
        "averageHR": 140,
    }
    data.update(overrides)
    return data


def _sync_once(session, monkeypatch, fake):
    monkeypatch.setattr(svc, "client", fake)
    return svc._sync_activities(session, date(2026, 7, 24), date(2026, 7, 24), strength_limit=0)


def test_full_detail_empty_valid_response_is_not_repeated(session, monkeypatch):
    calls = []
    class Fake:
        def activities_by_date(self, *_): return [_running()]
        def hr_zones(self, *_): return []
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        class api:
            @staticmethod
            def get_activity(activity_id): calls.append(activity_id); return {"summaryDTO": {}}
    fake = Fake()
    _sync_once(session, monkeypatch, fake)
    _sync_once(session, monkeypatch, fake)
    assert calls == [1]
    assert session.get(Activity, 1).provenance_checked is True


@pytest.mark.parametrize("response", [None, [], "invalid"])
def test_invalid_full_detail_response_remains_retryable(session, monkeypatch, response):
    calls = []
    class Fake:
        def activities_by_date(self, *_): return [_running()]
        def hr_zones(self, *_): return []
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        class api:
            @staticmethod
            def get_activity(activity_id): calls.append(activity_id); return response
    fake = Fake()
    _sync_once(session, monkeypatch, fake)
    _sync_once(session, monkeypatch, fake)
    assert calls == [1, 1]
    assert session.get(Activity, 1).provenance_checked is not True


def test_failed_full_detail_response_remains_retryable(session, monkeypatch):
    calls = []
    class Fake:
        def activities_by_date(self, *_): return [_running()]
        def hr_zones(self, *_): return []
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        class api:
            @staticmethod
            def get_activity(activity_id):
                calls.append(activity_id)
                raise RuntimeError("temporary failure")
    fake = Fake()
    _sync_once(session, monkeypatch, fake)
    _sync_once(session, monkeypatch, fake)
    assert calls == [1, 1]
    assert session.get(Activity, 1).provenance_checked is not True


def test_summary_workout_provenance_skips_full_detail(session, monkeypatch):
    class Fake:
        def activities_by_date(self, *_): return [_running(workoutId=8)]
        def hr_zones(self, *_): return []
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        class api:
            @staticmethod
            def get_activity(_): raise AssertionError("already resolved by summary")
    _sync_once(session, monkeypatch, Fake())
    assert session.get(Activity, 1).source_workout_id == 8


def test_hr_zone_successes_are_persisted_and_not_repeated(session, monkeypatch):
    calls = []
    class Fake:
        def activities_by_date(self, *_): return [_running()]
        def hr_zones(self, activity_id): calls.append(activity_id); return [{"zoneNumber": 1, "secsInZone": 60}]
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        class api:
            @staticmethod
            def get_activity(_): return {}
    fake = Fake()
    _sync_once(session, monkeypatch, fake)
    _sync_once(session, monkeypatch, fake)
    assert calls == [1]
    assert session.get(Activity, 1).hr_zone_seconds == "[60.0, 0.0, 0.0, 0.0, 0.0]"
    assert session.get(SyncState, "activity_hr_zones_checked:1").value == "complete"


def test_empty_or_failed_hr_zones_completion_behavior(session, monkeypatch):
    calls = []
    class Fake:
        def activities_by_date(self, *_): return [_running()]
        def hr_zones(self, activity_id):
            calls.append(activity_id)
            return [] if len(calls) == 1 else None
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        class api:
            @staticmethod
            def get_activity(_): return {}
    fake = Fake()
    _sync_once(session, monkeypatch, fake)
    _sync_once(session, monkeypatch, fake)
    assert calls == [1]
    assert session.get(SyncState, "activity_hr_zones_checked:1").value == "complete"
    session.delete(session.get(SyncState, "activity_hr_zones_checked:1"))
    _sync_once(session, monkeypatch, fake)
    assert calls == [1, 1]
    assert session.get(SyncState, "activity_hr_zones_checked:1") is None


def test_no_hr_data_makes_no_zone_request(session, monkeypatch):
    class Fake:
        def activities_by_date(self, *_): return [_running(averageHR=None, maximumHR=None)]
        def hr_zones(self, *_): raise AssertionError("no HR makes zones ineligible")
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        class api:
            @staticmethod
            def get_activity(_): return {}
    _sync_once(session, monkeypatch, Fake())


def test_strength_sets_empty_and_local_rows_seed_completion(session, monkeypatch):
    calls = []
    monkeypatch.setattr(svc.client, "exercise_sets", lambda activity_id: calls.append(activity_id) or {"exerciseSets": []})
    assert svc._sync_exercise_sets(session, 1) is True
    assert svc._sync_exercise_sets(session, 1) is True
    assert calls == [1]
    assert session.get(SyncState, "activity_strength_sets_checked:1").value == "complete"
    session.add(ExerciseSet(activity_id=2, set_index=0, exercise_name="Edited", edited=True))
    session.flush()
    assert svc._sync_exercise_sets(session, 2) is True
    assert calls == [1]
    assert session.get(SyncState, "activity_strength_sets_checked:2").value == "complete"


def test_failed_strength_sets_remain_retryable_and_auth_propagates(session, monkeypatch):
    monkeypatch.setattr(svc.client, "exercise_sets", lambda _: None)
    assert svc._sync_exercise_sets(session, 1) is False
    assert session.get(SyncState, "activity_strength_sets_checked:1") is None
    monkeypatch.setattr(svc.client, "exercise_sets", lambda _: (_ for _ in ()).throw(GarminConnectAuthenticationError("401")))
    with pytest.raises(GarminConnectAuthenticationError):
        svc._sync_exercise_sets(session, 1)


def test_enrichment_authentication_error_reaches_authentication_required_flow(session, monkeypatch):
    @contextmanager
    def bound_session():
        yield session
        session.commit()

    monkeypatch.setattr(svc, "get_session", bound_session)
    session.add(SyncState(key="stage1_bootstrap_complete", value="complete"))
    session.commit()
    monkeypatch.setattr(svc, "_sync_activities", lambda *_args, **_kwargs: (_ for _ in ()).throw(GarminConnectAuthenticationError("401")))
    summary = svc.run_sync(force=True)

    assert summary["code"] == "authentication_required"
    assert summary["skipped"] is True


def test_first_429_stops_later_enrichment(session, monkeypatch):
    calls = []
    class Fake:
        def activities_by_date(self, *_): return [_running(1), _running(2)]
        def hr_zones(self, activity_id): calls.append(activity_id); raise GarminConnectTooManyRequestsError("limit")
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        def activity_detail(self, activity_id): return self.api.get_activity(activity_id)
        class api:
            @staticmethod
            def get_activity(_): return {}
    with pytest.raises(GarminConnectTooManyRequestsError):
        _sync_once(session, monkeypatch, Fake())
    assert calls == [1]
    assert session.get(SyncState, "activity_hr_zones_checked:1") is None
