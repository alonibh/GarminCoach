"""Local immutable history for slow Garmin metrics; this module performs no I/O."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
import hashlib
import json
import math
import re
from typing import Iterable

from sqlalchemy.orm import Session

from db import MetricCapability, SlowMetricObservation, SyncState
from metrics.capability_registry import ACCOUNT_SCOPE_KEY


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
class NumericObservationInput:
    metric: str
    scope_kind: str
    scope_key: str
    observed_on: date
    observed_at: datetime | None
    value: object
    source_kind: str
    source_key: str
    created_at: datetime
    source_order: int | None = None


@dataclass(frozen=True)
class BatchRecordItem:
    observation: NumericObservationInput
    result: RecordObservationResult


@dataclass(frozen=True)
class BatchRecordResult:
    items: tuple[BatchRecordItem, ...]


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


def _clean_text(value: object, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= maximum and not _CONTROL_RE.search(value) else None


def _valid_datetime(value: object) -> bool:
    return isinstance(value, datetime) and value.tzinfo is None


def _valid_day(value: object) -> bool:
    return type(value) is date


def _valid_scope(metric: str, kind: str, key: str) -> bool:
    if metric in {"fitness_age", "target_fitness_age"}:
        return kind == "account" and key == ACCOUNT_SCOPE_KEY
    if metric == "vo2max":
        return kind == "activity" and key in {"running", "cycling", "legacy_unverified"}
    return metric == "training_status" and kind == "device" and bool(key) and not _CONTROL_RE.search(key)


def _canonical_id(**fields: object) -> str:
    payload = json.dumps(fields, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _order_key(observed_on: date, observed_at: datetime | None, source_order: int | None, source_key: str):
    return (observed_on, observed_at or datetime.min, source_order if source_order is not None else -1, source_key)


def _row_order_key(row: SlowMetricObservation):
    return _order_key(row.observed_on, row.observed_at, _source_order_from_key(row.source_key), row.source_key)


def _source_order_from_key(source_key: str) -> int | None:
    # Canonical activity keys begin activity:<integer>:; other source keys sort
    # only by their stable complete key.
    match = re.match(r"^activity:(\d+):", source_key)
    return int(match.group(1)) if match else None


def _normal_numeric(value: NumericObservationInput, *, as_of_day: date | None) -> NumericObservationInput | None:
    scope_key = _clean_text(value.scope_key, 96)
    source_kind = _clean_text(value.source_kind, 48)
    source_key = _clean_text(value.source_key, 192)
    if (not _valid_day(value.observed_on) or (as_of_day is not None and value.observed_on > as_of_day)
            or (value.observed_at is not None and not _valid_datetime(value.observed_at))
            or not _valid_datetime(value.created_at) or scope_key is None or source_kind is None or source_key is None
            or not _valid_scope(value.metric, value.scope_kind, scope_key)
            or isinstance(value.value, bool) or not isinstance(value.value, (int, float))
            or (value.source_order is not None and (isinstance(value.source_order, bool) or not isinstance(value.source_order, int) or value.source_order < 0))):
        return None
    numeric = float(value.value)
    if value.metric not in _NUMERIC_BOUNDS or not math.isfinite(numeric) or not 0 < numeric <= _NUMERIC_BOUNDS[value.metric]:
        return None
    return NumericObservationInput(value.metric, value.scope_kind, scope_key, value.observed_on, value.observed_at,
                                   numeric, source_kind, source_key, value.created_at, value.source_order)


def _source_row(session: Session, value: NumericObservationInput | tuple) -> SlowMetricObservation | None:
    metric, kind, key, source_kind, source_key = value.metric, value.scope_kind, value.scope_key, value.source_kind, value.source_key
    return session.query(SlowMetricObservation).filter_by(metric=metric, scope_kind=kind, scope_key=key,
                                                           source_kind=source_kind, source_key=source_key).one_or_none()


def _record_numeric_normalized(session: Session, value: NumericObservationInput) -> RecordObservationResult:
    existing = _source_row(session, value)
    if existing:
        same = (existing.observed_on == value.observed_on and existing.observed_at == value.observed_at
                and existing.numeric_value == value.value and existing.text_value is None)
        return RecordObservationResult(RecordObservationOutcome.DUPLICATE_SOURCE if same else RecordObservationOutcome.CONFLICT,
                                       existing.observation_id)
    head = session.query(SlowMetricObservation).filter_by(metric=value.metric, scope_kind=value.scope_kind,
                                                           scope_key=value.scope_key).order_by(
        SlowMetricObservation.observed_on.desc(), SlowMetricObservation.observed_at.desc(),
        SlowMetricObservation.source_key.desc()).first()
    if head and _order_key(value.observed_on, value.observed_at, value.source_order, value.source_key) < _row_order_key(head):
        return RecordObservationResult(RecordObservationOutcome.OLDER_THAN_HEAD)
    if head and head.numeric_value == value.value and head.text_value is None:
        return RecordObservationResult(RecordObservationOutcome.UNCHANGED, head.observation_id)
    observation_id = _canonical_id(metric=value.metric, scope_kind=value.scope_kind, scope_key=value.scope_key,
                                   observed_on=value.observed_on.isoformat(), observed_at=value.observed_at.isoformat() if value.observed_at else None,
                                   numeric_value=value.value, source_kind=value.source_kind, source_key=value.source_key)
    session.add(SlowMetricObservation(observation_id=observation_id, metric=value.metric, scope_kind=value.scope_kind,
                                      scope_key=value.scope_key, observed_on=value.observed_on, observed_at=value.observed_at,
                                      numeric_value=value.value, text_value=None, source_kind=value.source_kind,
                                      source_key=value.source_key, created_at=value.created_at))
    return RecordObservationResult(RecordObservationOutcome.RECORDED, observation_id)


def record_numeric_observation_batch(session: Session, *, observations: Iterable[NumericObservationInput],
                                     as_of_day: date) -> BatchRecordResult:
    """Validate, partition and process a batch in canonical oldest-first order.

    No caller-owned transaction is committed. Duplicate source conflicts are
    detected before that source is mutated, so response order cannot affect the
    durable series or the final compatibility candidate.
    """
    if not _valid_day(as_of_day):
        return BatchRecordResult(tuple(BatchRecordItem(item, RecordObservationResult(RecordObservationOutcome.INVALID)) for item in observations))
    validated: list[tuple[NumericObservationInput, NumericObservationInput | None]] = []
    for item in observations:
        if not isinstance(item, NumericObservationInput):
            continue
        validated.append((item, _normal_numeric(item, as_of_day=as_of_day)))
    results: list[BatchRecordItem] = [BatchRecordItem(raw, RecordObservationResult(RecordObservationOutcome.INVALID)) for raw, normalized in validated if normalized is None]
    groups: dict[tuple[str, str, str], list[NumericObservationInput]] = {}
    for _, normalized in validated:
        if normalized is not None:
            groups.setdefault((normalized.metric, normalized.scope_kind, normalized.scope_key), []).append(normalized)
    for values in groups.values():
        by_source: dict[tuple[str, str], list[NumericObservationInput]] = {}
        for value in values:
            by_source.setdefault((value.source_kind, value.source_key), []).append(value)
        conflict_ids: set[int] = set()
        duplicate_ids: set[int] = set()
        for entries in by_source.values():
            canonical = {(x.observed_on, x.observed_at, x.value, x.source_order) for x in entries}
            if len(canonical) > 1:
                conflict_ids.update(id(x) for x in entries)
            elif len(entries) > 1:
                duplicate_ids.update(id(x) for x in entries[1:])
        for value in sorted(values, key=lambda x: _order_key(x.observed_on, x.observed_at, x.source_order, x.source_key)):
            if id(value) in conflict_ids:
                result = RecordObservationResult(RecordObservationOutcome.CONFLICT)
            elif id(value) in duplicate_ids:
                result = RecordObservationResult(RecordObservationOutcome.DUPLICATE_SOURCE)
            else:
                result = _record_numeric_normalized(session, value)
            results.append(BatchRecordItem(value, result))
    return BatchRecordResult(tuple(results))


def record_numeric_observation(session: Session, *, metric: str, scope_kind: str, scope_key: str, observed_on: date,
                               observed_at: datetime | None, value: object, source_kind: str, source_key: str,
                               created_at: datetime, as_of_day: date | None = None) -> RecordObservationResult:
    item = NumericObservationInput(metric, scope_kind, scope_key, observed_on, observed_at, value, source_kind, source_key, created_at)
    normalized = _normal_numeric(item, as_of_day=as_of_day)
    return RecordObservationResult(RecordObservationOutcome.INVALID) if normalized is None else _record_numeric_normalized(session, normalized)


def record_text_observation(session: Session, *, metric: str, scope_kind: str, scope_key: str, observed_on: date,
                            observed_at: datetime | None, value: object, source_kind: str, source_key: str,
                            created_at: datetime, as_of_day: date | None = None) -> RecordObservationResult:
    scope_key, source_kind, source_key, text_value = (_clean_text(scope_key, 96), _clean_text(source_kind, 48),
                                                       _clean_text(source_key, 192), _clean_text(value, 64))
    if (metric != "training_status" or not _valid_day(observed_on) or (as_of_day is not None and observed_on > as_of_day)
            or (observed_at is not None and not _valid_datetime(observed_at)) or not _valid_datetime(created_at)
            or scope_key is None or source_kind is None or source_key is None or text_value is None
            or not _valid_scope(metric, scope_kind, scope_key)):
        return RecordObservationResult(RecordObservationOutcome.INVALID)
    existing = _source_row(session, type("Source", (), {"metric": metric, "scope_kind": scope_kind, "scope_key": scope_key,
                                                           "source_kind": source_kind, "source_key": source_key})())
    if existing:
        same = existing.observed_on == observed_on and existing.observed_at == observed_at and existing.text_value == text_value and existing.numeric_value is None
        return RecordObservationResult(RecordObservationOutcome.DUPLICATE_SOURCE if same else RecordObservationOutcome.CONFLICT, existing.observation_id)
    head = session.query(SlowMetricObservation).filter_by(metric=metric, scope_kind=scope_kind, scope_key=scope_key).order_by(
        SlowMetricObservation.observed_on.desc(), SlowMetricObservation.observed_at.desc(), SlowMetricObservation.source_key.desc()).first()
    if head and _order_key(observed_on, observed_at, None, source_key) < _row_order_key(head):
        return RecordObservationResult(RecordObservationOutcome.OLDER_THAN_HEAD)
    if head and head.text_value == text_value and head.numeric_value is None:
        return RecordObservationResult(RecordObservationOutcome.UNCHANGED, head.observation_id)
    observation_id = _canonical_id(metric=metric, scope_kind=scope_kind, scope_key=scope_key, observed_on=observed_on.isoformat(),
                                   observed_at=observed_at.isoformat() if observed_at else None, text_value=text_value,
                                   source_kind=source_kind, source_key=source_key)
    session.add(SlowMetricObservation(observation_id=observation_id, metric=metric, scope_kind=scope_kind, scope_key=scope_key,
                                      observed_on=observed_on, observed_at=observed_at, numeric_value=None, text_value=text_value,
                                      source_kind=source_kind, source_key=source_key, created_at=created_at))
    return RecordObservationResult(RecordObservationOutcome.RECORDED, observation_id)


def _numeric_history(session: Session, metric: str, scope_key: str, as_of_day: date) -> ScopedNumericHistory:
    kind = "account" if metric != "vo2max" else "activity"
    rows = session.query(SlowMetricObservation).filter(SlowMetricObservation.metric == metric,
        SlowMetricObservation.scope_kind == kind, SlowMetricObservation.scope_key == scope_key,
        SlowMetricObservation.observed_on <= as_of_day, SlowMetricObservation.observed_on >= as_of_day.fromordinal(as_of_day.toordinal() - 364),
    ).order_by(SlowMetricObservation.observed_on.desc(), SlowMetricObservation.observed_at.desc(), SlowMetricObservation.source_key.desc()).limit(60).all()
    rows.sort(key=_row_order_key)
    points = tuple(NumericHistoryPoint(row.observed_on, float(row.numeric_value), row.scope_key, row.source_kind) for row in rows if row.numeric_value is not None)
    cap_metric = "fitness_age" if metric == "target_fitness_age" else metric
    cap = session.get(MetricCapability, (cap_metric, kind, scope_key))
    return ScopedNumericHistory(metric, kind, scope_key, points[-1].value if points else None,
                                points[-2].value if len(points) > 1 else None, points,
                                (cap.override_state or cap.support_state) if cap else "unknown", scope_key == "legacy_unverified")


def build_slow_metric_history_report(session: Session, *, as_of_day: date) -> SlowMetricHistoryReport:
    if not _valid_day(as_of_day):
        raise ValueError("as_of_day must be a date")
    fitness, target = _numeric_history(session, "fitness_age", "account", as_of_day), _numeric_history(session, "target_fitness_age", "account", as_of_day)
    running, cycling, legacy = (_numeric_history(session, "vo2max", key, as_of_day) for key in ("running", "cycling", "legacy_unverified"))
    model, display = session.get(SyncState, "garmin_device_model_key"), session.get(SyncState, "garmin_device_display_name")
    device_key = model.value if model and model.value else None
    cap = session.get(MetricCapability, ("training_status", "device", device_key)) if device_key else None
    state = (cap.override_state or cap.support_state) if cap else "unknown"
    if not device_key:
        training = TrainingStatusHistory("NO_DEVICE_IDENTITY", None, None, "unknown", None, None, ())
    else:
        rows = session.query(SlowMetricObservation).filter(SlowMetricObservation.metric == "training_status",
            SlowMetricObservation.scope_kind == "device", SlowMetricObservation.scope_key == device_key,
            SlowMetricObservation.observed_on <= as_of_day, SlowMetricObservation.observed_on >= as_of_day.fromordinal(as_of_day.toordinal() - 364),
        ).order_by(SlowMetricObservation.observed_on.desc(), SlowMetricObservation.observed_at.desc(), SlowMetricObservation.source_key.desc()).limit(60).all()
        rows.sort(key=_row_order_key)
        changes = tuple(StatusHistoryPoint(row.observed_on, row.text_value or "", device_key) for row in rows)
        presentation = "SUPPORTED_WITH_DATA" if state == "supported" and changes else "SUPPORTED_NO_DATA" if state == "supported" else "UNSUPPORTED" if state == "unsupported" else "UNKNOWN"
        training = TrainingStatusHistory(presentation, device_key, display.value if display else None, state,
                                         changes[-1].status if changes else None, changes[-1].observed_on if changes else None, changes[-12:])
    return SlowMetricHistoryReport(as_of_day, fitness, target, running, cycling, legacy, training)
