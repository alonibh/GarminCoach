from datetime import datetime, date
import pytest
from db import Activity, ExerciseSet
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


def test_summary_only_activity_sync_uses_only_the_range_request(session, monkeypatch):
    calls = []

    class FakeGarmin:
        def activities_by_date(self, start, end):
            calls.append(("range", start, end))
            return [
                {"activityId": 100, "activityType": {"typeKey": "running"}, "duration": 1800,
                 "startTimeLocal": "2026-07-24 10:00:00", "distance": 5000},
                {"activityId": 101, "activityType": {"typeKey": "strength_training"}, "duration": 2400,
                 "startTimeLocal": "2026-07-24 11:00:00", "calories": 300},
            ]

        def hr_zones(self, activity_id):
            calls.append(("hr_zones", activity_id))
            raise AssertionError("summary sync must not request HR zones")

        def exercise_sets(self, activity_id):
            calls.append(("exercise_sets", activity_id))
            raise AssertionError("summary sync must not request exercise sets")

        class api:
            @staticmethod
            def get_activity(activity_id):
                calls.append(("get_activity", activity_id))
                raise AssertionError("summary sync must not request activity details")

    import sync.sync_service as ss
    monkeypatch.setattr(ss, "client", FakeGarmin())

    assert _sync_activities(session, date(2026, 7, 24), date(2026, 7, 25), enrich=False) == 2
    assert calls == [("range", date(2026, 7, 24), date(2026, 7, 25))]
    assert {row.id for row in session.query(Activity).all()} == {100, 101}


def test_summary_only_upsert_preserves_existing_enrichment(session, monkeypatch):
    existing = Activity(
        id=100, activity_type="running", rpe=8, feel=4, source_workout_id=77,
        provenance_checked=True, hr_zone_seconds="[1, 2, 3, 4, 5]",
    )
    session.add(existing)
    session.add(ExerciseSet(activity_id=100, set_index=0, set_type="ACTIVE", exercise_name="Squat", reps=5))
    session.commit()

    class FakeGarmin:
        def activities_by_date(self, start, end):
            return [{"activityId": 100, "activityType": {"typeKey": "running"}, "duration": 1800,
                     "startTimeLocal": "2026-07-24 10:00:00", "distance": 5000}]

        def hr_zones(self, activity_id):
            raise AssertionError("summary sync must not request HR zones")

        def exercise_sets(self, activity_id):
            raise AssertionError("summary sync must not request exercise sets")

        class api:
            @staticmethod
            def get_activity(activity_id):
                raise AssertionError("summary sync must not request activity details")

    import sync.sync_service as ss
    monkeypatch.setattr(ss, "client", FakeGarmin())

    _sync_activities(session, date(2026, 7, 24), date(2026, 7, 24), enrich=False)
    session.commit()
    saved = session.get(Activity, 100)
    assert (saved.rpe, saved.feel, saved.source_workout_id, saved.hr_zone_seconds) == (8, 4, 77, "[1, 2, 3, 4, 5]")
    assert session.query(ExerciseSet).filter_by(activity_id=100).count() == 1


def test_enriched_activity_sync_fetches_missing_hr_zones(session, monkeypatch):
    calls = []

    class FakeGarmin:
        def activities_by_date(self, start, end):
            return [{"activityId": 100, "activityType": {"typeKey": "running"}, "duration": 1800,
                     "startTimeLocal": "2026-07-24 10:00:00", "directWorkoutRpe": 5,
                     "directWorkoutFeel": 3, "workoutId": 77, "averageHR": 140}]

        def hr_zones(self, activity_id):
            calls.append(activity_id)
            return [{"zoneNumber": 1, "secsInZone": 60}]

        def exercise_sets(self, activity_id):
            return {}

    import sync.sync_service as ss
    monkeypatch.setattr(ss, "client", FakeGarmin())

    _sync_activities(session, date(2026, 7, 24), date(2026, 7, 24), strength_limit=0)
    assert calls == [100]
    assert session.get(Activity, 100).hr_zone_seconds == "[60.0, 0.0, 0.0, 0.0, 0.0]"
