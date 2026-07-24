from datetime import datetime, date
import pytest
from db import Activity
from coach.onboarding import _usable_completed_activities
from sync.sync_service import _sync_activities


def test_zero_duration_activities_filtered_from_onboarding():
    valid = Activity(id=1, activity_type="running", start_time=datetime.now(), duration_s=1800)
    zero_dur = Activity(id=2, activity_type="running", start_time=datetime.now(), duration_s=0)
    none_dur = Activity(id=3, activity_type="strength_training", start_time=datetime.now(), duration_s=None)

    usable = _usable_completed_activities([valid, zero_dur, none_dur])
    assert usable == [valid]


def test_sync_activities_ignores_zero_duration(session, monkeypatch):
    class FakeGarmin:
        def activities_by_date(self, start, end):
            return [
                {"activityId": 100, "activityType": {"typeKey": "running"}, "duration": 1800, "startTimeLocal": "2026-07-24 10:00:00"},
                {"activityId": 101, "activityType": {"typeKey": "running"}, "duration": 0, "startTimeLocal": "2026-07-24 10:05:00"},
                {"activityId": 102, "activityType": {"typeKey": "strength_training"}, "duration": None, "startTimeLocal": "2026-07-24 10:10:00"},
            ]
    import sync.sync_service as ss
    monkeypatch.setattr(ss, "client", FakeGarmin())

    count = _sync_activities(session, date(2026, 7, 24), date(2026, 7, 25))
    assert count == 1

    synced = session.query(Activity).all()
    assert len(synced) == 1
    assert synced[0].id == 100
