"""Shared pure training aggregate calculations for weekly and Ask Coach views."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from math import isfinite
import re

from coach.exercises import GARMIN_EXERCISES
from db import ExerciseSet

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACE = re.compile(r"\s+")
_GENERIC = frozenset({"exercise", "unknown", "other", "generic", "strength", "unknown_exercise", "other_exercise", "generic_exercise", "strength_exercise", "unnamed_exercise"})
_CATEGORIES = frozenset(item["category"] for item in GARMIN_EXERCISES.values())


@dataclass(frozen=True)
class StrengthCandidate:
    key: str
    label: str
    reps: int
    current_weight_kg: float
    prior_weight_kg: float
    delta_kg: float


@dataclass(frozen=True)
class StrengthComparisonResult:
    candidates: tuple[StrengthCandidate, ...]
    total_candidates: int


def finite(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if isfinite(value) else None


def nonnegative_integer(value: object) -> int | None:
    value = finite(value)
    return int(value) if value is not None and value >= 0 and value.is_integer() else None


def positive_integer(value: object) -> int | None:
    value = nonnegative_integer(value)
    return value if value and value > 0 else None


def normalize_activity_domain(value: object) -> str:
    token = _SPACE.sub("_", str(value or "").strip().lower().replace("-", "_")).strip("_")
    if "strength" in token or "weight" in token: return "strength"
    if "run" in token: return "running"
    if "cycl" in token or "bike" in token: return "cycling"
    if "walk" in token or "hike" in token: return "walking"
    if "soccer" in token or "football" in token: return "soccer"
    if "swim" in token: return "swimming"
    return "other"


def duration_minutes(activities) -> tuple[int | None, int]:
    values = [finite(row.duration_s) for row in activities]
    valid = [value for value in values if value is not None and value >= 0]
    return (int(round(sum(valid) / 60)) if valid else None, len(valid))


def display_weight_kg(value: object) -> float | None:
    value = finite(value)
    if value is None or value <= 0: return None
    try:
        return float((Decimal(str(value)) * Decimal("4")).quantize(Decimal("1"), rounding=ROUND_HALF_UP) / Decimal("4"))
    except (InvalidOperation, ValueError):
        return None


def _clean(value: object, maximum: int = 48) -> str | None:
    if not isinstance(value, str): return None
    value = _SPACE.sub(" ", _CONTROL.sub(" ", value)).strip()
    return value[:maximum].rstrip() or None


def _source_identity(value: object) -> tuple[str, str] | None:
    cleaned = _clean(value, 96)
    if not cleaned: return None
    token = re.sub(r"[^a-z0-9]+", "_", cleaned.casefold()).strip("_")
    return None if not token or token in _GENERIC else (token, cleaned)


def exact_strength_identity(row: ExerciseSet) -> tuple[str, str] | None:
    """Exact catalog/custom identity used by both presentation surfaces."""
    name, category = _source_identity(row.exercise_name), _source_identity(row.exercise_category)
    if name and category:
        key = f"{category[0].upper()}:{name[0].upper()}"
        item = GARMIN_EXERCISES.get(key)
        return (key, _clean(item.get("label"), 48) or name[1]) if item else (f"custom:{category[0]}:{name[0]}", name[1])
    if name:
        matches = [item for item in GARMIN_EXERCISES.values() if item.get("garmin_name") == name[0].upper()]
        if len(matches) == 1: return matches[0]["key"], _clean(matches[0].get("label"), 48) or name[1]
        return f"custom:name:{name[0]}", name[1]
    if category and category[0].upper() not in _CATEGORIES: return f"custom:category:{category[0]}", category[1]
    return None


def active_work_set(row: ExerciseSet) -> bool:
    return isinstance(row.set_type, str) and row.set_type.strip().upper() in {"ACTIVE", "WORK"}


def stable_strength_candidates(current: dict[tuple[str, int], tuple[float, str]], prior: dict[tuple[str, int], float]) -> tuple[StrengthCandidate, ...]:
    result = []
    for (key, reps), (weight, label) in current.items():
        shown, old = display_weight_kg(weight), display_weight_kg(prior.get((key, reps)))
        if shown is not None and old is not None and shown > old:
            result.append(StrengthCandidate(key, label, reps, shown, old, shown - old))
    return tuple(sorted(result, key=lambda item: (-item.delta_kg, item.label.casefold(), item.reps)))


def build_strength_comparisons(rows, *, current_start: date, current_end: date) -> StrengthComparisonResult:
    """Shared prior-seven-day versus current-window exact strength evidence."""
    prior_start = current_start - timedelta(days=7)
    current, prior = {}, {}
    for row, started in rows:
        if not isinstance(started, datetime) or not active_work_set(row): continue
        identity, weight, reps = exact_strength_identity(row), finite(row.weight_kg), positive_integer(row.reps)
        if identity is None or weight is None or weight <= 0 or reps is None: continue
        key = identity[0], reps
        if current_start <= started.date() <= current_end and (key not in current or weight > current[key][0]): current[key] = weight, identity[1]
        elif prior_start <= started.date() < current_start: prior[key] = max(prior.get(key, 0), weight)
    candidates = stable_strength_candidates(current, prior)
    return StrengthComparisonResult(candidates, len(candidates))
