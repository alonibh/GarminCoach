"""Tests for _sync_workouts pruning logic (sync/sync_service.py).

Covers the reconcile/prune edge cases: normal removal, the transient-failure
guard (never prune), and the genuine "user deleted everything" case (prune to
zero). The Garmin client is faked — no network.
"""
import json
from datetime import datetime

import pytest

import sync.sync_service as svc
from db import Workout


class _FakeApi:
    def __init__(self, workouts, raise_on_list=False, list_is_none=False):
        self._workouts = workouts
        self._raise = raise_on_list
        self._none = list_is_none

    def get_workouts(self):
        if self._raise:
            raise RuntimeError("transient Garmin glitch")
        if self._none:
            return None
        return self._workouts

    def get_workout_by_id(self, wid):
        return {"workoutSegments": []}


def _patch_api(monkeypatch, api):
    # `client.api` is a property that raises until login(); set the backing
    # attribute so the property returns our fake instead.
    monkeypatch.setattr(svc.client, "_api", api, raising=False)


def _summary(wid, name="W", sport="strength_training"):
    return {"workoutId": wid, "workoutName": name, "sportType": {"sportTypeKey": sport}}


def _seed(session, *ids):
    for i in ids:
        session.add(Workout(workout_id=i, name=f"W{i}", sport_type="strength_training",
                            steps_json="[]", created_at=datetime.now()))
    session.commit()


def test_prunes_removed_workout(session, monkeypatch):
    _seed(session, 1, 2, 3)
    _patch_api(monkeypatch, _FakeApi([_summary(1), _summary(2)]))  # 3 removed in Garmin
    svc._sync_workouts(session)
    ids = sorted(w.workout_id for w in session.query(Workout).all())
    assert ids == [1, 2]


def test_user_deleted_everything_prunes_to_zero(session, monkeypatch):
    _seed(session, 1, 2)
    _patch_api(monkeypatch, _FakeApi([]))  # genuine empty: user removed all
    svc._sync_workouts(session)
    assert session.query(Workout).count() == 0


def test_transient_failure_never_prunes(session, monkeypatch):
    _seed(session, 1, 2)
    _patch_api(monkeypatch, _FakeApi(None, raise_on_list=True))  # exception
    svc._sync_workouts(session)
    assert session.query(Workout).count() == 2  # untouched


def test_none_response_never_prunes(session, monkeypatch):
    _seed(session, 1, 2)
    _patch_api(monkeypatch, _FakeApi(None, list_is_none=True))  # None, not []
    svc._sync_workouts(session)
    assert session.query(Workout).count() == 2  # untouched


def test_upsert_adds_new_workout(session, monkeypatch):
    _seed(session, 1)
    _patch_api(monkeypatch, _FakeApi([_summary(1), _summary(5, name="New")]))
    svc._sync_workouts(session)
    ids = sorted(w.workout_id for w in session.query(Workout).all())
    assert ids == [1, 5]
