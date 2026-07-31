"""Local, immutable history for slow Garmin-provided metrics.

This module deliberately has no Garmin, web, notification, coach, calendar, or
workout dependency.  Callers own their transaction and decide compatibility
cache updates after an accepted outcome.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import hashlib
import json
import math
import re

from sqlalchemy import or_
from sqlalchemy.orm import Session

from db import MetricCapability, SlowMetricObservation, SyncState
from metrics.capability_registry import ACCOUNT_SCOPE_KEY, UNKNOWN_DEVICE_SCOPE_KEY


class RecordObservationOutcome(StrEnum):
    RECORDED = "RECORDED"
    DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
    UNCHANGED = "UNCHANGED"
    OLDER_THAN_HEAD = "OLDER_THAN_HEAD"
    CONFLICT = "CONFLICT"
    INVALID = "INVALID"


@dataclass(frozen=True)
class RecordObservationResult:
    outcome: RecordObservationOutcome
    observation_id: str | None = None


@dataclass(frozen=True)
class NumericHistoryPoint:
    observed_on: date
    value: float
    scope_key: str
    source_kind: str


@dataclass(frozen=True)
class StatusHistoryPoint:
    observed_on: date
    status: str
    device_scope_key: str


@dataclass(frozen=True)
class ScopedNumericHistory:
    metric: str
    scope_kind: str
    scope_key: str
    current_value: float | None
    previous_value: float | None
    points: tuple[NumericHistoryPoint, ...]
    capability_state: str
    legacy_unverified: bool


@dataclass(frozen=True)
class TrainingStatusHistory:
    state: str
    device_scope_key: str | None
    device_display_name: str | None
    capability_state: str
    current_status: str | None
    current_day: date | None
    changes: tuple[StatusHistoryPoint, ...]


@dataclass(frozen=True)
class SlowMetricHistoryReport:
    as_of_day: date
    fitness_age: ScopedNumericHistory
    target_fitness_age: ScopedNumericHistory
    vo2_running: ScopedNumericHistory
    vo2_cycling: ScopedNumericHistory
    vo2_legacy: ScopedNumericHistory
    training_status: TrainingStatusHistory


_NUMERIC_BOUNDS = {"fitness_age": 120.0, "target_fitness_age": 120.0, "vo2max": 100.0}
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _canonical_id(**fields: object) -> str:
    encoded = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _valid_scope(metric: str, scope_kind: str, scope_key: str) -> bool:
    if metric in {"fitness_age", "target_fitness_age"}:
        return scope_kind == "account" and scope_key == ACCOUNT_SCOPE_KEY
    if metric == "vo2max":
        return scope_kind == "activity" and scope_key in {"running", "cycling", "legacy_unverified"}
    return metric == "training_status" and scope_kind == "device" and bool(scope_key.strip())


def _source_existing(session: Session, *, metric: str, scope_kind: str, scope_key: str, source_kind: str, source_key: str):
    return session.query(SlowMetricObservation).filter_by(
        metric=metric, scope_kind=scope_kind, scope_key=scope_key,
        source_kind=source_kind, source_key=source_key,
    ).one_or_none()


def _sort_key(row: SlowMetricObservation) -> tuple[date, datetime, str]:
    return (row.observed_on, row.observed_at or datetime.min, row.source_key)


def _record(
    session: Session, *, metric: str, scope_kind: str, scope_key: str,
    observed_on: date, observed_at: datetime | None, numeric_value: float | None,
    text_value: str | None, source_kind: str, source_key: str, created_at: datetime,
) -> RecordObservationResult:
    if not _valid_scope(metric, scope_kind, scope_key) or not isinstance(observed_on, date):
        return RecordObservationResult(RecordObservationOutcome.INVALID)
    if not isinstance(source_kind, str) or not source_kind or len(source_kind) > 48:
        return RecordObservationResult(RecordObservationOutcome.INVALID)
    if not isinstance(source_key, str) or not source_key or len(source_key) > 192:
        return RecordObservationResult(RecordObservationOutcome.INVALID)
    existing = _source_existing(session, metric=metric, scope_kind=scope_kind, scope_key=scope_key,
                                source_kind=source_kind, source_key=source_key)
    if existing:
        same = (existing.numeric_value == numeric_value and existing.text_value == text_value and
                existing.observed_on == observed_on and existing.observed_at == observed_at)
        return RecordObservationResult(RecordObservationOutcome.DUPLICATE_SOURCE if same else RecordObservationOutcome.CONFLICT,
                                       existing.observation_id)
    head = session.query(SlowMetricObservation).filter_by(
        metric=metric, scope_kind=scope_kind, scope_key=scope_key,
    ).order_by(SlowMetricObservation.observed_on.desc(), SlowMetricObservation.observed_at.desc(),
               SlowMetricObservation.source_key.desc()).first()
    candidate_key = (observed_on, observed_at or datetime.min, source_key)
    if head and candidate_key < _sort_key(head):
        return RecordObservationResult(RecordObservationOutcome.OLDER_THAN_HEAD)
    value = numeric_value if numeric_value is not None else text_value
    if head and head.numeric_value == numeric_value and head.text_value == text_value:
        return RecordObservationResult(RecordObservationOutcome.UNCHANGED, head.observation_id)
    observation_id = _canonical_id(metric=metric, scope_kind=scope_kind, scope_key=scope_key,
                                   observed_on=observed_on.isoformat(), observed_at=observed_at.isoformat() if observed_at else None,
                                   numeric_value=numeric_value, text_value=text_value,
                                   source_kind=source_kind, source_key=source_key)
    session.add(SlowMetricObservation(
        observation_id=observation_id, metric=metric, scope_kind=scope_kind, scope_key=scope_key,
        observed_on=observed_on, observed_at=observed_at, numeric_value=numeric_value,
        text_value=text_value, source_kind=source_kind, source_key=source_key, created_at=created_at,
    ))
    return RecordObservationResult(RecordObservationOutcome.RECORDED, observation_id)


def record_numeric_observation(session: Session, *, metric: str, scope_kind: str, scope_key: str,
                               observed_on: date, observed_at: datetime | None, value: object,
                               source_kind: str, source_key: str, created_at: datetime) -> RecordObservationResult:
    if metric not in _NUMERIC_BOUNDS or isinstance(value, bool) or not isinstance(value, (int, float)):
        return RecordObservationResult(RecordObservationOutcome.INVALID)
    numeric = float(value)
    if not math.isfinite(numeric) or not (0 < numeric <= _NUMERIC_BOUNDS[metric]):
        return RecordObservationResult(RecordObservationOutcome.INVALID)
    return _record(session, metric=metric, scope_kind=scope_kind, scope_key=scope_key, observed_on=observed_on,
                   observed_at=observed_at, numeric_value=numeric, text_value=None,
                   source_kind=source_kind, source_key=source_key, created_at=created_at)


def record_text_observation(session: Session, *, metric: str, scope_kind: str, scope_key: str,
                            observed_on: date, observed_at: datetime | None, value: object,
                            source_kind: str, source_key: str, created_at: datetime) -> RecordObservationResult:
    if metric != "training_status" or not isinstance(value, str):
        return RecordObservationResult(RecordObservationOutcome.INVALID)
    normalized = value.strip()
    if not normalized or len(normalized) > 64 or _CONTROL_RE.search(normalized):
        return RecordObservationResult(RecordObservationOutcome.INVALID)
    return _record(session, metric=metric, scope_kind=scope_kind, scope_key=scope_key, observed_on=observed_on,
                   observed_at=observed_at, numeric_value=None, text_value=normalized,
                   source_kind=source_kind, source_key=source_key, created_at=created_at)


def _numeric_history(session: Session, metric: str, scope_key: str, as_of_day: date) -> ScopedNumericHistory:
    rows = session.query(SlowMetricObservation).filter(
        SlowMetricObservation.metric == metric, SlowMetricObservation.scope_kind == ("account" if metric != "vo2max" else "activity"),
        SlowMetricObservation.scope_key == scope_key, SlowMetricObservation.observed_on <= as_of_day,
        SlowMetricObservation.observed_on >= as_of_day.fromordinal(max(date.min.toordinal(), as_of_day.toordinal() - 364)),
    ).order_by(SlowMetricObservation.observed_on.desc(), SlowMetricObservation.observed_at.desc(), SlowMetricObservation.source_key.desc()).limit(60).all()
    rows.sort(key=_sort_key)
    points = tuple(NumericHistoryPoint(r.observed_on, float(r.numeric_value), r.scope_key, r.source_kind) for r in rows if r.numeric_value is not None)
    current = points[-1].value if points else None
    previous = points[-2].value if len(points) > 1 else None
    cap = session.get(MetricCapability, (metric, "account" if metric != "vo2max" else "activity", scope_key))
    return ScopedNumericHistory(metric, "account" if metric != "vo2max" else "activity", scope_key, current, previous,
                                points, (cap.override_state or cap.support_state) if cap else "unknown", scope_key == "legacy_unverified")


def build_slow_metric_history_report(session: Session, *, as_of_day: date) -> SlowMetricHistoryReport:
    fitness = _numeric_history(session, "fitness_age", "account", as_of_day)
    target = _numeric_history(session, "target_fitness_age", "account", as_of_day)
    running = _numeric_history(session, "vo2max", "running", as_of_day)
    cycling = _numeric_history(session, "vo2max", "cycling", as_of_day)
    legacy = _numeric_history(session, "vo2max", "legacy_unverified", as_of_day)
    model = session.get(SyncState, "garmin_device_model_key")
    device_key = model.value if model and model.value else None
    display = session.get(SyncState, "garmin_device_display_name")
    cap = session.get(MetricCapability, ("training_status", "device", device_key)) if device_key else None
    cap_state = (cap.override_state or cap.support_state) if cap else "unknown"
    if not device_key:
        training = TrainingStatusHistory("NO_DEVICE_IDENTITY", None, None, "unknown", None, None, ())
    else:
        rows = session.query(SlowMetricObservation).filter(
            SlowMetricObservation.metric == "training_status", SlowMetricObservation.scope_kind == "device",
            SlowMetricObservation.scope_key == device_key, SlowMetricObservation.observed_on <= as_of_day,
            SlowMetricObservation.observed_on >= as_of_day.fromordinal(max(date.min.toordinal(), as_of_day.toordinal() - 364)),
        ).order_by(SlowMetricObservation.observed_on.desc(), SlowMetricObservation.observed_at.desc(), SlowMetricObservation.source_key.desc()).limit(60).all()
        rows.sort(key=_sort_key)
        changes = tuple(StatusHistoryPoint(r.observed_on, r.text_value or "", device_key) for r in rows)
        state = "SUPPORTED_WITH_DATA" if cap_state == "supported" and changes else "SUPPORTED_NO_DATA" if cap_state == "supported" else "UNSUPPORTED" if cap_state == "unsupported" else "UNKNOWN"
        training = TrainingStatusHistory(state, device_key, display.value if display else None, cap_state,
                                         changes[-1].status if changes else None, changes[-1].observed_on if changes else None, changes[-12:])
    return SlowMetricHistoryReport(as_of_day, fitness, target, running, cycling, legacy, training)
