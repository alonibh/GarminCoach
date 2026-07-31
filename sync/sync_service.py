"""Pull data from Garmin into SQLite with idempotent upserts.

Garmin's JSON shapes are loosely documented and occasionally vary, so every
parser is defensive: missing keys -> None rather than a crash. A failed day or
activity is logged and skipped; the sync continues.
"""
from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from sqlalchemy.orm import Session

from coach import coach
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

import config
from db import (
    Activity,
    DailyHealth,
    DeviceCapability,
    ExerciseSet,
    MetricSnapshot,
    Sleep,
    SyncState,
    Workout,
    get_session,
)
from sync.endpoint_telemetry import is_auth_error, telemetry_scope
from sync.garmin_client import (
    client,
    normalize_hrv_data,
    normalize_recovery_time,
    normalize_training_readiness,
)
from time_utils import get_local_tz

logger = logging.getLogger(__name__)

# Kept as a local name for existing sync error paths; implementation is shared
# with wrapper telemetry so both classifications stay identical.
_is_auth_error = is_auth_error

# Activity type substrings that carry per-set strength detail.
_STRENGTH_HINTS = ("strength", "weight")

_RESOURCE_CURSOR_KEYS = {
    "activities": "last_activities_sync_through",
    "sleep": "last_sleep_sync_through",
    "daily_health": "last_daily_health_sync_through",
}

_STAGE1_COMPLETE = "stage1_bootstrap_complete"
_STAGE1_PREFIX = "stage1_bootstrap_"
_STAGE2_ANCHOR = "stage2_backfill_anchor_day"
_STAGE2_SLEEP_GAP = "stage2_sleep_next_gap"
_STAGE2_DAILY_HEALTH_GAP = "stage2_daily_health_next_gap"
_STAGE2_ACTIVITY_GAP = "stage2_activity_summary_next_gap"
_STAGE2_SUMMARY_COMPLETE = "stage2_summary_backfill_complete"
_STAGE2_STRENGTH_CANDIDATES = "stage2_strength_candidate_ids"
_STAGE2_STRENGTH_NEXT_INDEX = "stage2_strength_next_index"
_STAGE2_STRENGTH_COMPLETE = "stage2_strength_backfill_complete"
_STAGE2_WELLNESS_DAYS = 28
_STAGE2_ACTIVITY_DAYS = 90
_STAGE2_ACTIVITY_CHUNK_DAYS = 30
_WEEKLY_SLOW_METRICS = "last_weekly_slow_metrics_at"


def _activity_completion_key(kind: str, activity_id: int) -> str:
    """Return a bounded per-activity enrichment completion key."""
    return f"activity_{kind}_checked:{activity_id}"


# --- small helpers --------------------------------------------------------
def _g(d: Any, *keys, default=None):
    """Safe nested get: _g(d, 'a', 'b') == d['a']['b'] or default."""
    cur = d
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return default
    return cur


def _parse_dt(s: Any) -> Optional[datetime]:
    if not s:
        return None

    if isinstance(s, (int, float)):
        # Garmin sometimes returns epoch milliseconds for sleep timestamps.
        # Very small values are seconds; current epoch-ms values are 13 digits.
        ts = float(s)
        if ts > 10_000_000_000:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        except (OSError, OverflowError, ValueError):
            return None

    if not isinstance(s, str):
        return None

    try:
        # Garmin occasionally adds time zone offsets (e.g. +03:00 or Z)
        # We want the literal wall-clock time as a naive datetime.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        pass
        
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _get_state(session, key: str) -> Optional[str]:
    row = session.get(SyncState, key)
    return row.value if row else None


def _set_state(session, key: str, value: str) -> None:
    row = session.get(SyncState, key)
    if row:
        row.value = value
    else:
        session.add(SyncState(key=key, value=value))


def _clear_state(session, key: str) -> None:
    row = session.get(SyncState, key)
    if row:
        session.delete(row)


def _parse_state_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _local_today() -> date:
    return datetime.now(get_local_tz()).date()


def _resource_cursor(session: Session, resource: str) -> Optional[date]:
    """Return a resource cursor, lazily seeding it from the legacy cursor."""
    key = _RESOURCE_CURSOR_KEYS[resource]
    value = _get_state(session, key)
    if value is not None:
        return _parse_state_date(value)

    legacy = _get_state(session, "last_sync_through")
    cursor = _parse_state_date(legacy)
    if cursor:
        # Keep the legacy key for compatibility, but avoid reusing it as normal
        # sync progress once this resource has its own cursor.
        _set_state(session, key, cursor.isoformat())
    return cursor


def _advance_resource_cursor(session: Session, resource: str, through: date) -> None:
    """Move a resource cursor forward only; overlap retries must not rewind it."""
    current = _resource_cursor(session, resource)
    if current is None or through > current:
        _set_state(session, _RESOURCE_CURSOR_KEYS[resource], through.isoformat())


def _resource_start(session: Session, resource: str, today: date, full: bool) -> date:
    cursor = _resource_cursor(session, resource)
    if full or cursor is None:
        return today - timedelta(days=config.INITIAL_BACKFILL_DAYS)
    # Re-sync the last few days too (data settles after the day ends).
    return cursor - timedelta(days=3)


def _stage1_key(name: str) -> str:
    return f"{_STAGE1_PREFIX}{name}"


def _has_meaningful_sync_progress(session: Session) -> bool:
    """Never surprise an existing installation with a new-account bootstrap."""
    # Bootstrap's own partial cursors are not legacy progress.  They are its
    # resume journal and must continue through to the completion marker.
    if session.query(SyncState).filter(SyncState.key.like(f"{_STAGE1_PREFIX}%")).count():
        return False
    if _get_state(session, "last_sync_through") or _get_state(session, "last_sync_at"):
        return True
    if any(_get_state(session, key) for key in _RESOURCE_CURSOR_KEYS.values()):
        return True
    return any((
        session.query(Activity).count(),
        session.query(Sleep).count(),
        session.query(DailyHealth).count(),
    ))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_state_dt(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _is_in_cooldown(session) -> tuple[bool, Optional[datetime]]:
    cooldown_until = _parse_state_dt(_get_state(session, "garmin_cooldown_until"))
    if cooldown_until and cooldown_until > _utc_now():
        return True, cooldown_until
    return False, None


def _note_rate_limited(session) -> datetime:
    until = _utc_now() + timedelta(minutes=config.GARMIN_429_COOLDOWN_MINUTES)
    _set_state(session, "garmin_cooldown_until", until.isoformat(timespec="seconds"))
    return until


def _clear_cooldown(session) -> None:
    if _get_state(session, "garmin_cooldown_until"):
        _set_state(session, "garmin_cooldown_until", "")


def _device_upload_iso_from_payload(dev: dict | None) -> str:
    upload_ms = (dev or {}).get("lastUsedDeviceUploadTime")
    if not upload_ms:
        return ""
    return datetime.fromtimestamp(upload_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")


def _latest_activity_marker(raw: dict | None) -> tuple[str, str]:
    if not raw:
        return "", ""
    act_id = raw.get("activityId")
    start = raw.get("startTimeLocal") or raw.get("startTimeGMT") or ""
    return (str(act_id) if act_id is not None else "", str(start or ""))


def _preflight(session) -> dict:
    """Cheap Garmin metadata check used to avoid heavy syncs."""
    _set_state(session, "last_sync_check_at", _utc_now().isoformat(timespec="seconds"))

    dev = client.device_last_used()
    from metrics.freshness import note_capability_from_device
    note_capability_from_device(session, dev)
    device_upload = _device_upload_iso_from_payload(dev)
    _set_state(session, "device_last_upload", device_upload)

    recent = client.recent_activities(limit=1)
    latest_id, latest_start = _latest_activity_marker((recent or [None])[0])

    previous_upload = _get_state(session, "last_processed_device_upload") or ""
    previous_id = _get_state(session, "last_seen_activity_id") or ""
    previous_start = _get_state(session, "last_seen_activity_start") or ""

    return {
        "device_upload": device_upload,
        "latest_activity_id": latest_id,
        "latest_activity_start": latest_start,
        "device_changed": bool(device_upload and device_upload != previous_upload),
        "activity_changed": bool(
            (latest_id and latest_id != previous_id)
            or (latest_start and latest_start != previous_start)
        ),
    }


def _store_preflight_markers(session, preflight: dict) -> None:
    if preflight.get("device_upload"):
        _set_state(session, "last_processed_device_upload", preflight["device_upload"])
    if preflight.get("latest_activity_id"):
        _set_state(session, "last_seen_activity_id", preflight["latest_activity_id"])
    if preflight.get("latest_activity_start"):
        _set_state(session, "last_seen_activity_start", preflight["latest_activity_start"])


def _workouts_due(session, full: bool) -> bool:
    if full:
        return True
    if session.query(Workout).count() == 0:
        return True
    last = _parse_state_dt(_get_state(session, "last_workouts_sync_at"))
    if not last:
        return True
    return (_utc_now() - last) >= timedelta(hours=config.WORKOUT_SYNC_INTERVAL_HOURS)


# --- activities + strength sets ------------------------------------------
def _is_strength(activity_type: str) -> bool:
    t = (activity_type or "").lower()
    return any(h in t for h in _STRENGTH_HINTS)


def _workout_id(payload: Any) -> Optional[int]:
    """Extract only an explicit Garmin workout-template provenance id."""
    if not isinstance(payload, dict):
        return None
    for key in ("workoutId", "workoutID", "associatedWorkoutId"):
        value = payload.get(key)
        if value is not None:
            try:
                return int(value)
            except (TypeError, ValueError):
                return None
    for key in ("summaryDTO", "metadataDTO", "activityDTO"):
        found = _workout_id(payload.get(key))
        if found is not None:
            return found
    return None


def _parse_sleep_dt(value: Any, *, is_gmt: bool = False) -> Optional[datetime]:
    """Parse a Garmin sleep timestamp into the athlete's local wall-clock time.

    Sleep timestamps are stored as naive local datetimes because the rest of the
    application displays them as wall-clock values. Garmin's GMT fields must
    therefore be converted before persisting them.
    """
    parsed = _parse_dt(value)
    if parsed is None or not is_gmt:
        return parsed
    return parsed.replace(tzinfo=timezone.utc).astimezone(get_local_tz()).replace(tzinfo=None)


def _upsert_activity(session, raw: dict, *, enrich: bool = True) -> Optional[int]:
    act_id = raw.get("activityId")
    if act_id is None:
        return None
    act_id = int(act_id)
    act = session.get(Activity, act_id) or Activity(id=act_id)
    act.activity_type = _g(raw, "activityType", "typeKey", default="") or ""
    act.name = raw.get("activityName")
    act.start_time = _parse_dt(raw.get("startTimeLocal") or raw.get("startTimeGMT"))
    act.duration_s = raw.get("duration")
    act.distance_m = raw.get("distance")
    act.calories = raw.get("calories")
    act.avg_hr = raw.get("averageHR")
    act.max_hr = raw.get("maxHR")
    # Cardio / outdoor fields (populated for soccer, running, cycling…).
    act.moving_duration_s = raw.get("movingDuration")
    act.avg_speed_mps = raw.get("averageSpeed")
    act.max_speed_mps = raw.get("maxSpeed")
    act.avg_cadence = raw.get("averageRunningCadenceInStepsPerMinute")
    act.avg_stride_cm = raw.get("avgStrideLength")
    act.elevation_gain_m = raw.get("elevationGain")
    act.elevation_loss_m = raw.get("elevationLoss")
    act.lap_count = raw.get("lapCount")
    act.steps = raw.get("steps")
    act.moderate_intensity_min = raw.get("moderateIntensityMinutes")
    act.vigorous_intensity_min = raw.get("vigorousIntensityMinutes")
    act.training_effect_label = raw.get("trainingEffectLabel")
    act.aerobic_te_msg = raw.get("aerobicTrainingEffectMessage")
    act.anaerobic_te_msg = raw.get("anaerobicTrainingEffectMessage")
    new_rpe = raw.get("directWorkoutRpe")
    new_feel = raw.get("directWorkoutFeel")
    new_workout_id = _workout_id(raw)
    if new_workout_id is not None:
        act.provenance_checked = True

    # Full details resolve workout provenance plus Garmin-recorded RPE and Feel.
    # A valid response settles that question even when those optional fields are absent.
    if enrich and not act.provenance_checked:
        try:
            full_act = client.activity_detail(act_id)
            if isinstance(full_act, dict):
                summary_dto = full_act.get("summaryDTO", {})
                if not isinstance(summary_dto, dict):
                    summary_dto = {}
                new_rpe = summary_dto.get("directWorkoutRpe", new_rpe)
                new_feel = summary_dto.get("directWorkoutFeel", new_feel)
                new_workout_id = _workout_id(full_act) or new_workout_id
                act.provenance_checked = True
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as e:
            if _is_auth_error(e):
                raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from e
            logger.warning("Failed to fetch full activity %s for RPE/provenance extraction: %s", act_id, e)

    # Only update if we found something new, otherwise preserve existing
    if new_rpe is not None:
        act.rpe = new_rpe
    if new_feel is not None:
        act.feel = new_feel
    if new_workout_id is not None:
        act.source_workout_id = new_workout_id
        
    # Summary-only ranges must remain a single Garmin range request.
    if enrich:
        _sync_hr_zones(session, act)
            
    session.add(act)
    return act_id


def _sync_hr_zones(session: Session, activity: Activity) -> bool:
    """Resolve HR zones once when the summary makes the activity eligible."""
    key = _activity_completion_key("hr_zones", activity.id)
    if activity.hr_zone_seconds is not None:
        _set_state(session, key, "complete")
        return True
    if _get_state(session, key) == "complete":
        return True
    if not activity.duration_s or activity.duration_s <= 0 or (
        activity.avg_hr is None and activity.max_hr is None
    ):
        return False
    try:
        zones_raw = client.hr_zones(activity.id)
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Failed to fetch hr_zones for %s: %s", activity.id, exc)
        return False
    if not isinstance(zones_raw, list):
        logger.warning("Garmin returned an invalid HR-zone response for activity %s", activity.id)
        return False

    secs = [0.0] * 5
    for zone in zones_raw:
        if not isinstance(zone, dict):
            logger.warning("Garmin returned an invalid HR-zone item for activity %s", activity.id)
            return False
        zone_number = zone.get("zoneNumber")
        if zone_number is not None and 1 <= zone_number <= 5:
            secs[zone_number - 1] = float(zone.get("secsInZone") or 0.0)
    if zones_raw and sum(secs) > 0:
        activity.hr_zone_seconds = json.dumps(secs)
    _set_state(session, key, "complete")
    return True


def _sync_exercise_sets(session, activity_id: int) -> bool:
    """Replace non-edited sets and report whether Garmin resolved the activity."""
    key = _activity_completion_key("strength_sets", activity_id)
    if session.query(ExerciseSet.id).filter(ExerciseSet.activity_id == activity_id).first():
        _set_state(session, key, "complete")
        return True
    if _get_state(session, key) == "complete":
        return True
    try:
        data = client.exercise_sets(activity_id)
    except GarminConnectTooManyRequestsError:
        raise  # let the circuit breaker handle rate limits
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Exercise-sets fetch failed for activity %s", activity_id, exc_info=True)
        return False  # normal-sync callers intentionally treat this as non-fatal
    if not isinstance(data, dict):
        logger.warning("Garmin returned an invalid exercise-set response for activity %s", activity_id)
        return False
    sets = _g(data, "exerciseSets", default=[])
    if not isinstance(sets, list):
        logger.warning("Garmin returned invalid exercise sets for activity %s", activity_id)
        return False
    if not sets:
        _set_state(session, key, "complete")
        session.flush()
        from coach.strength_progression_integration import RecalculationCause, request_activity_recalculation
        request_activity_recalculation(session, activity_id, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
        return True

    existing = (
        session.query(ExerciseSet).filter(ExerciseSet.activity_id == activity_id).all()
    )
    edited_idx = {s.set_index for s in existing if s.edited}
    # Wipe only non-edited rows, then re-insert from fresh data.
    for s in existing:
        if not s.edited:
            session.delete(s)

    for i, raw in enumerate(sets):
        if i in edited_idx:
            continue  # leave the user's correction untouched
        ex = (_g(raw, "exercises", default=[]) or [{}])[0]
        # Garmin reports weight in GRAMS — convert to kg for storage/display.
        raw_weight = raw.get("weight")
        weight_kg = round(raw_weight / 1000.0, 2) if raw_weight else None
        session.add(
            ExerciseSet(
                activity_id=activity_id,
                set_index=i,
                set_type=raw.get("setType") or "",
                exercise_category=ex.get("category"),
                exercise_name=ex.get("name"),
                reps=raw.get("repetitionCount"),
                weight_kg=weight_kg,
                duration_s=raw.get("duration"),
                edited=False,
            )
        )
    _set_state(session, key, "complete")
    # This trigger is intentionally only on a successful live resolution, not
    # the older "sets already exist" fast path, so deployment never backfills.
    session.flush()
    from coach.strength_progression_integration import RecalculationCause, request_activity_recalculation
    request_activity_recalculation(session, activity_id, cause=RecalculationCause.STRENGTH_SETS_RESOLVED)
    return True


def _sync_workouts(session: Session) -> None:
    """Fetch user's pre-defined workouts and their deep step structures.

    Reconciles local state with Garmin: upserts every workout Garmin returns
    and prunes local rows whose workout no longer exists in Garmin (so deleting
    a template in Garmin Connect removes it here too, instead of accumulating
    every workout the user has ever created).
    """
    try:
        workouts = client.workout_list()
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        logger.warning("Failed to fetch workout list from Garmin", exc_info=True)
        return
    # A None (rather than []) means the call didn't really succeed — treat it
    # like a failure so we never prune on an ambiguous response.
    if workouts is None:
        logger.warning("Garmin returned no workout list; skipping sync/prune.")
        return

    import json
    from datetime import datetime

    seen_ids = set()
    for w_summary in workouts:
        wid = w_summary.get("workoutId")
        if not wid:
            continue
        seen_ids.add(wid)

        name = w_summary.get("workoutName", "Unnamed Workout")
        sport_type = _g(w_summary, "sportType", "sportTypeKey", default="unknown")

        # We only really care about strength, running, cycling, etc., but we can save all
        try:
            full_w = client.workout_detail(wid)
            steps_json = json.dumps(full_w.get("workoutSegments", []))
        except GarminConnectTooManyRequestsError:
            raise
        except Exception:
            logger.warning("Failed to fetch workout detail for id=%s ('%s')", wid, name, exc_info=True)
            steps_json = "[]"

        row = session.query(Workout).filter_by(workout_id=wid).first()
        if not row:
            row = Workout(workout_id=wid, created_at=datetime.now())

        row.name = name
        row.sport_type = sport_type
        row.steps_json = steps_json
        row.updated_at = datetime.now()

        session.add(row)

    # Prune templates the user removed from Garmin. We only reach here on a
    # successful fetch (failures returned early above), so an empty `seen_ids`
    # genuinely means "the user has zero workouts" and pruning to empty is
    # correct — a transient glitch can't reach this point.
    stale = session.query(Workout).filter(Workout.workout_id.notin_(seen_ids)).all()
    for row in stale:
        logger.info("Pruning workout no longer in Garmin: id=%s ('%s')", row.workout_id, row.name)
        session.delete(row)

    session.commit()


def _sync_activities(
    session: Session, start: date, end: date, *, strength_limit: int | None = None,
    enrich: bool = True, vo2_values: list[tuple[str, float]] | None = None,
) -> int:
    try:
        raw_list = client.activities_by_date(start, end)
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        raise
        
    count = 0
    strength_ids: list[tuple[datetime, int]] = []
    observed_vo2: list[tuple[str, float]] = []
    for raw in raw_list or []:
        raw_dur = raw.get("duration")
        if raw_dur is None or float(raw_dur) <= 0:
            continue
        act_id = _upsert_activity(session, raw, enrich=enrich)
        if act_id is None:
            continue
        activity_domain = _g(raw, "activityType", "typeKey", default="") or ""
        if _is_strength(activity_domain):
            when = _parse_dt(raw.get("startTimeLocal") or raw.get("startTimeGMT")) or datetime.min
            strength_ids.append((when, act_id))
        value = _finite_number(raw.get("vO2MaxValue"))
        try:
            value_date = date.fromisoformat((raw.get("startTimeLocal") or "")[:10]).isoformat()
        except (TypeError, ValueError):
            value_date = None
        if value is not None and value_date is not None:
            observed_vo2.append((value_date, float(value)))
            from metrics.freshness import note_capability_observed
            # Activity summaries expose VO2 max by sport; never generalize a
            # running observation into cycling (or vice versa).
            from metrics.capability_registry import normalize_activity_domain
            domain = normalize_activity_domain(activity_domain)
            if domain:
                note_capability_observed(session, "vo2max", activity_domain=domain)
            if vo2_values is not None:
                vo2_values.append((value_date, float(value)))
        count += 1
    if enrich:
        for _, act_id in sorted(strength_ids, reverse=True)[:strength_limit]:
            _sync_exercise_sets(session, act_id)
    # Stage 1 records its resumable activity marker separately.  Every other
    # normal activity window updates the local snapshot without another Garmin
    # read, and forward-only history rejects older overlap observations.
    if vo2_values is None and observed_vo2:
        _record_snapshot(session, "vo2max", *max(observed_vo2))
    return count


# --- daily health + sleep -------------------------------------------------
def _sync_sleep(session, day: date) -> bool:
    try:
        data = client.sleep(day)
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Sleep fetch failed for %s", day, exc_info=True)
        return False
    dto = _g(data, "dailySleepDTO", default={}) or {}
    row = session.get(Sleep, day) or Sleep(day=day)
    row.sleep_start_time = (
        _parse_sleep_dt(dto.get("sleepStartTimestampLocal"))
        or _parse_sleep_dt(dto.get("startTimeLocal"))
        or _parse_sleep_dt(dto.get("sleepStartTimestamp"))
        or _parse_sleep_dt(dto.get("sleepStartTimestampGMT"), is_gmt=True)
        or _parse_sleep_dt(dto.get("startTimeGMT"), is_gmt=True)
    )
    row.sleep_end_time = (
        _parse_sleep_dt(dto.get("sleepEndTimestampLocal"))
        or _parse_sleep_dt(dto.get("endTimeLocal"))
        or _parse_sleep_dt(dto.get("sleepEndTimestamp"))
        or _parse_sleep_dt(dto.get("sleepEndTimestampGMT"), is_gmt=True)
        or _parse_sleep_dt(dto.get("endTimeGMT"), is_gmt=True)
    )
    row.total_s = dto.get("sleepTimeSeconds")
    row.deep_s = dto.get("deepSleepSeconds")
    row.light_s = dto.get("lightSleepSeconds")
    row.rem_s = dto.get("remSleepSeconds")
    row.awake_s = dto.get("awakeSleepSeconds")
    row.score = _g(dto, "sleepScores", "overall", "value")
    row.respiration_avg = dto.get("averageRespirationValue")
    if row.respiration_avg is None:
        row.respiration_avg = dto.get("avgRespirationValue")
    row.sleep_stress_avg = dto.get("avgSleepStress")
    session.add(row)
    return True


def _parse_daily_summary(payload: object) -> tuple[dict[str, float | int], set[str]] | None:
    """Return verified daily-summary values plus represented metric families."""
    if not isinstance(payload, dict):
        return None
    values: dict[str, float | int] = {}
    families: set[str] = set()
    groups = {
        "resting_hr": (("restingHeartRate", "resting_hr"),),
        "stress": (("averageStressLevel", "stress_avg"),),
        "steps": (("totalSteps", "steps"), ("dailyStepGoal", "step_goal")),
        "body_battery": (
            ("bodyBatteryHighestValue", "body_battery_high"),
            ("bodyBatteryLowestValue", "body_battery_low"),
            ("bodyBatteryChargedValue", "body_battery_charged"),
            ("bodyBatteryDrainedValue", "body_battery_drained"),
        ),
        "calories": (("totalKilocalories", "total_kcal"), ("activeKilocalories", "active_kcal"), ("bmrKilocalories", "bmr_kcal")),
        # DailyStats in garminconnect==0.3.7 explicitly models these exact
        # daily-summary keys.  Do not conflate them with activity detail.
        "intensity_minutes": (
            ("moderateIntensityMinutes", "daily_moderate_intensity_minutes"),
            ("vigorousIntensityMinutes", "daily_vigorous_intensity_minutes"),
        ),
    }
    for family, pairs in groups.items():
        if any(key in payload for key, _ in pairs):
            families.add(family)
        for key, field in pairs:
            value = payload.get(key)
            if family == "intensity_minutes":
                # Garmin's daily counts may legitimately be zero.  Missing or
                # invalid values stay NULL; they must never become zero.
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    values[field] = value
                elif isinstance(value, float) and math.isfinite(value) and value >= 0:
                    values[field] = value
            elif isinstance(value, int) and not isinstance(value, bool):
                values[field] = value
            elif isinstance(value, float) and math.isfinite(value):
                values[field] = value
    return values, families


def _finite_number(value: object) -> float | int | None:
    """Return a Garmin numeric value without accepting booleans or coercions."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    return None


@dataclass(frozen=True)
class _SignalOutcome:
    """The result of this invocation, never a conclusion from stored values."""

    state: str  # fresh, missing, or error
    error_code: str | None = None
    requested: bool = True


def _set_hrv_outcomes(
    session: Session, day: date, overnight: _SignalOutcome, status: _SignalOutcome,
) -> None:
    session.info.setdefault("hrv_request_outcomes", {})[day] = {
        "hrv": overnight,
        "hrv_status": status,
    }


def _set_recovery_outcome(session: Session, day: date, outcome: _SignalOutcome) -> None:
    session.info.setdefault("recovery_time_request_outcomes", {})[day] = outcome


def _set_readiness_outcome(session: Session, day: date, outcome: _SignalOutcome) -> None:
    session.info.setdefault("training_readiness_request_outcomes", {})[day] = outcome


def _recompute_hrv_coverage(session: Session, changed_day: date) -> None:
    """Refresh local seven-night HRV completeness for an edited date and successors."""
    for offset in range(7):
        target = changed_day + timedelta(days=offset)
        row = session.get(DailyHealth, target)
        if row is None:
            continue
        start = target - timedelta(days=6)
        covered = 0
        for candidate in session.query(DailyHealth).filter(
            DailyHealth.day >= start, DailyHealth.day <= target,
        ):
            if _finite_number(candidate.hrv_overnight) is not None:
                covered += 1
        row.hrv_7d_coverage_days = min(7, covered)
        session.add(row)


def _apply_normalized_hrv(session: Session, row: DailyHealth, normalized) -> bool:
    """Persist valid HRV siblings without treating a nightly value as status evidence."""
    if normalized is None or normalized.calendar_date != row.day:
        _set_hrv_outcomes(
            session, row.day, _SignalOutcome("missing"), _SignalOutcome("missing"),
        )
        return False
    # These three fields are one Garmin Status observation.  A valid current
    # summary replaces the group, including optional absences, so stale status
    # text can never accompany a later status-less response.
    row.hrv_weekly_avg = normalized.weekly_avg
    row.hrv_status = normalized.status
    row.hrv_feedback_phrase = normalized.feedback_phrase
    fields = {
        "hrv_overnight": normalized.overnight_avg,
        "hrv_baseline_low": normalized.baseline_low,
        "hrv_baseline_high": normalized.baseline_high,
    }
    for field, value in fields.items():
        if value is not None:
            setattr(row, field, value)
    if normalized.status is not None:
        from metrics.freshness import note_capability_observed
        note_capability_observed(session, "hrv_status")
    session.add(row)
    _recompute_hrv_coverage(session, row.day)
    _set_hrv_outcomes(
        session, row.day,
        _SignalOutcome("fresh" if normalized.overnight_avg is not None else "missing"),
        _SignalOutcome("fresh" if normalized.status is not None else "missing"),
    )
    return normalized.overnight_avg is not None


def _persist_recovery_time(
    session: Session,
    row: DailyHealth,
    normalized_readiness: dict[str, Any] | None,
    *,
    fallback_observed_at: datetime,
) -> bool:
    """Persist only a current, already-selected Recovery Time observation."""
    recovery = normalize_recovery_time(
        normalized_readiness, fallback_observed_at=fallback_observed_at,
    )
    if recovery is None or recovery.calendar_date != row.day:
        _set_recovery_outcome(session, row.day, _SignalOutcome("missing"))
        return False
    _set_recovery_outcome(session, row.day, _SignalOutcome("fresh"))
    # All four columns describe this selected snapshot and must settle together.
    row.recovery_time_source_minutes = recovery.source_minutes
    row.recovery_time_minutes = recovery.effective_minutes
    row.recovery_time_change_phrase = recovery.change_phrase
    row.recovery_time_observed_at = recovery.observed_at
    session.add(row)
    from metrics.freshness import note_capability_observed
    note_capability_observed(session, "recovery_time_connect", observed_at=recovery.observed_at)
    return True


def _sync_daily_health_core(
    session, day: date, *, current_optional: bool = True, optional_context: str = "incremental",
) -> tuple[bool, bool, bool]:
    """Run the mandatory per-day work and report range fallback requirements.

    Range reads deliberately live outside this core so a multi-day window never
    spends one request per day for steps or Body Battery.
    """
    row = session.get(DailyHealth, day) or DailyHealth(day=day)
    complete = True

    try:
        _apply_normalized_hrv(session, row, normalize_hrv_data(client.hrv(day), day))
    except GarminConnectTooManyRequestsError:
        _set_hrv_outcomes(
            session, day, _SignalOutcome("error", "rate_limited"), _SignalOutcome("error", "rate_limited"),
        )
        raise
    except Exception as exc:
        code = "authentication_required" if _is_auth_error(exc) else _freshness_error_code(exc)
        _set_hrv_outcomes(
            session, day, _SignalOutcome("error", code), _SignalOutcome("error", code),
        )
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("HRV fetch failed for %s", day, exc_info=True)
        complete = False

    represented: set[str] = set()
    try:
        summary = client.user_summary(day)
        parsed = _parse_daily_summary(summary)
        if parsed is None:
            complete = False
        else:
            values, represented = parsed
            for field, value in values.items():
                setattr(row, field, value)
            if "body_battery" in represented and any(
                field in values for field in ("body_battery_high", "body_battery_low")
            ):
                from metrics.freshness import note_capability_observed
                note_capability_observed(session, "body_battery")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Daily summary fetch failed for %s", day, exc_info=True)
        complete = False

    if "resting_hr" not in represented:
        try:
            rhr = client.resting_hr(day)
            vals = _g(rhr, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE", default=[])
            if vals:
                row.resting_hr = vals[0].get("value")
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as exc:
            if _is_auth_error(exc):
                raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
            logger.warning("Resting HR fallback failed for %s", day, exc_info=True)
            complete = False
    if "stress" not in represented:
        try:
            stress = client.stress(day)
            row.stress_avg = stress.get("avgStressLevel")
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as exc:
            if _is_auth_error(exc):
                raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
            logger.warning("Stress fallback failed for %s", day, exc_info=True)
            complete = False
    # Readiness and status are current-only facts.  Historical wellness sync
    # deliberately never calls either endpoint.
    if current_optional and day == _local_today():
        _sync_current_optional_health(session, day, row, context=optional_context)

    session.add(row)
    battery_fallback = "body_battery" not in represented
    if battery_fallback:
        from metrics.freshness import capability_state
        battery_fallback = capability_state(session, "body_battery") != "unsupported"
    return complete, "steps" not in represented, battery_fallback


def _range_chunks(days: list[date]) -> list[tuple[date, date]]:
    """Return de-duplicated contiguous Garmin ranges, capped at 28 days."""
    ordered = sorted(set(days))
    if not ordered:
        return []
    chunks: list[tuple[date, date]] = []
    start = end = ordered[0]
    for day in ordered[1:]:
        if day != end + timedelta(days=1) or (day - start).days >= 28:
            chunks.append((start, end))
            start = day
        end = day
    chunks.append((start, end))
    return chunks


def _range_entries(payload: object, requested: set[date], key: str) -> dict[date, dict]:
    """Map a valid list response to requested dates; last duplicate wins.

    The upstream service occasionally repeats a day while a record settles.
    Selecting the final response-order entry is deterministic and ensures the
    latest returned representation is used without assigning malformed rows.
    """
    if not isinstance(payload, list):
        raise ValueError("invalid range response shape")
    mapped: dict[date, dict] = {}
    for item in payload:
        if not isinstance(item, dict):
            continue
        raw_day = item.get(key)
        if not isinstance(raw_day, str):
            continue
        try:
            item_day = date.fromisoformat(raw_day[:10])
        except ValueError:
            continue
        if item_day in requested:
            mapped[item_day] = item
    return mapped


def _apply_steps_range(session: Session, days: list[date]) -> set[date]:
    """Resolve every requested day on a valid response, including no-data."""
    resolved: set[date] = set()
    for start, end in _range_chunks(days):
        requested = {start + timedelta(days=n) for n in range((end - start).days + 1)}
        try:
            entries = _range_entries(client.daily_steps(start, end), requested, "calendarDate")
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as exc:
            if _is_auth_error(exc):
                raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
            logger.warning("Steps range fallback failed for %s through %s", start, end, exc_info=True)
            continue
        for day in requested:
            item = entries.get(day)
            if item is not None:
                row = session.get(DailyHealth, day) or DailyHealth(day=day)
                steps, goal = _finite_number(item.get("totalSteps")), _finite_number(item.get("stepGoal"))
                if steps is not None:
                    row.steps = steps
                if goal is not None:
                    row.step_goal = goal
                session.add(row)
            resolved.add(day)
    return resolved


def _apply_body_battery_range(session: Session, days: list[date]) -> set[date]:
    """Resolve every requested day on a valid response, preserving no-data."""
    resolved: set[date] = set()
    for start, end in _range_chunks(days):
        requested = {start + timedelta(days=n) for n in range((end - start).days + 1)}
        try:
            entries = _range_entries(client.body_battery(start, end), requested, "date")
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as exc:
            if _is_auth_error(exc):
                raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
            logger.warning("Body Battery range fallback failed for %s through %s", start, end, exc_info=True)
            continue
        for day in requested:
            item = entries.get(day)
            if item is not None:
                samples = item.get("bodyBatteryValuesArray")
                values = [
                    value for sample in samples if isinstance(sample, list) and len(sample) > 1
                    if (value := _finite_number(sample[1])) is not None
                ] if isinstance(samples, list) else []
                charged = _finite_number(item.get("charged"))
                drained = _finite_number(item.get("drained"))
                if values or charged is not None or drained is not None:
                    row = session.get(DailyHealth, day) or DailyHealth(day=day)
                    if values:
                        row.body_battery_high = max(values)
                        row.body_battery_low = min(values)
                        row.body_battery_current = values[-1]
                    if charged is not None:
                        row.body_battery_charged = charged
                    if drained is not None:
                        row.body_battery_drained = drained
                    session.add(row)
                    from metrics.freshness import note_capability_observed
                    note_capability_observed(session, "body_battery")
            resolved.add(day)
    return resolved


def _sync_daily_health_window(
    session: Session, start: date, end: date, *, current_optional: bool = True,
    optional_context: str = "incremental",
) -> tuple[int, date | None]:
    """Sync a daily-health window and advance only its resolved prefix."""
    candidates: list[tuple[date, bool, bool]] = []
    day = start
    try:
        while day <= end:
            mandatory, needs_steps, needs_battery = _sync_daily_health_core(
                session, day, current_optional=current_optional, optional_context=optional_context,
            )
            if not mandatory:
                break
            candidates.append((day, needs_steps, needs_battery))
            day += timedelta(days=1)
    except GarminConnectTooManyRequestsError:
        # A 429 must stop immediately, but already complete no-fallback days
        # are safe to retain as progress.  Days awaiting a range response are
        # deliberately left behind for a complete retry.
        for candidate_day, needs_steps, needs_battery in candidates:
            if not needs_steps and not needs_battery:
                _advance_resource_cursor(session, "daily_health", candidate_day)
            else:
                break
        raise
    mandatory_gap = day if day <= end else None

    steps_ok = _apply_steps_range(session, [day for day, needs, _ in candidates if needs])
    battery_ok = _apply_body_battery_range(session, [day for day, _, needs in candidates if needs])
    completed = 0
    first_gap: date | None = None
    for candidate_day, needs_steps, needs_battery in candidates:
        if (needs_steps and candidate_day not in steps_ok) or (needs_battery and candidate_day not in battery_ok):
            first_gap = candidate_day
            break
        _advance_resource_cursor(session, "daily_health", candidate_day)
        completed += 1
    if first_gap is None:
        first_gap = mandatory_gap
    return completed, first_gap


def _sync_daily_health(
    session, day: date, *, current_optional: bool = True, optional_context: str = "incremental",
) -> bool:
    """One-day compatibility wrapper used by Stage 2 and direct callers."""
    completed, _ = _sync_daily_health_window(
        session, day, day, current_optional=current_optional, optional_context=optional_context,
    )
    return completed == 1


def _sync_current_optional_health(
    session: Session, day: date, row: DailyHealth | None = None, *, context: str = "incremental",
) -> None:
    """Fetch current-only optional recovery facts, respecting capability."""
    from metrics.freshness import capability_fetch_decision, note_capability_observed, note_capability_probe

    row = row or session.get(DailyHealth, day) or DailyHealth(day=day)
    decision = capability_fetch_decision(session, "training_readiness", context)
    if decision in {"fetch_supported", "probe_unknown"}:
        current_probe_outcome: str | None = None
        recovery_error_code: str | None = None
        try:
            payload = normalize_training_readiness(client.training_readiness(day), day)
        except GarminConnectTooManyRequestsError:
            _set_readiness_outcome(session, day, _SignalOutcome("error", "rate_limited"))
            _set_recovery_outcome(session, day, _SignalOutcome("error", "rate_limited"))
            if decision == "probe_unknown":
                note_capability_probe(session, "training_readiness", "rate_limited")
            raise
        except GarminConnectAuthenticationError:
            _set_readiness_outcome(session, day, _SignalOutcome("error", "authentication_required"))
            _set_recovery_outcome(session, day, _SignalOutcome("error", "authentication_required"))
            if decision == "probe_unknown":
                note_capability_probe(session, "training_readiness", "authentication_error")
            raise
        except Exception as exc:
            if _is_auth_error(exc):
                _set_readiness_outcome(session, day, _SignalOutcome("error", "authentication_required"))
                _set_recovery_outcome(session, day, _SignalOutcome("error", "authentication_required"))
                if decision == "probe_unknown":
                    note_capability_probe(session, "training_readiness", "authentication_error")
                raise GarminConnectAuthenticationError("Garmin Connect authentication failed") from exc
            recovery_error_code = _freshness_error_code(exc)
            _set_readiness_outcome(session, day, _SignalOutcome("error", recovery_error_code))
            _set_recovery_outcome(session, day, _SignalOutcome("error", recovery_error_code))
            if decision == "probe_unknown":
                current_probe_outcome = "ordinary_error"
                note_capability_probe(session, "training_readiness", current_probe_outcome)
            logger.warning("Training Readiness capability probe failed")
            payload = None
        if payload is not None:
            _set_readiness_outcome(session, day, _SignalOutcome("fresh"))
            row.training_readiness = payload["trainingReadiness"]
            note_capability_observed(session)
            _persist_recovery_time(
                session, row, payload,
                fallback_observed_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
        else:
            if recovery_error_code is None:
                _set_readiness_outcome(session, day, _SignalOutcome("missing"))
                _set_recovery_outcome(session, day, _SignalOutcome("missing"))
            if decision == "probe_unknown" and current_probe_outcome is None:
                note_capability_probe(session, "training_readiness", "empty")

    status_decision = capability_fetch_decision(session, "training_status", context)
    if status_decision in {"fetch_supported", "probe_unknown"}:
        current_probe_outcome = None
        try:
            status_data = client.training_status(day)
        except GarminConnectTooManyRequestsError:
            if status_decision == "probe_unknown":
                note_capability_probe(session, "training_status", "rate_limited")
            raise
        except GarminConnectAuthenticationError:
            if status_decision == "probe_unknown":
                note_capability_probe(session, "training_status", "authentication_error")
            raise
        except Exception as exc:
            if _is_auth_error(exc):
                if status_decision == "probe_unknown":
                    note_capability_probe(session, "training_status", "authentication_error")
                raise GarminConnectAuthenticationError("Garmin Connect authentication failed") from exc
            if status_decision == "probe_unknown":
                current_probe_outcome = "ordinary_error"
                note_capability_probe(session, "training_status", current_probe_outcome)
            logger.warning("Training Status capability probe failed")
            status_data = None
        if isinstance(status_data, dict) and isinstance(status_data.get("mostRecentTrainingStatus"), str):
            row.training_status = status_data.get("mostRecentTrainingStatus")
            if status_decision == "probe_unknown":
                note_capability_observed(session, "training_status")
        elif status_decision == "probe_unknown" and current_probe_outcome is None:
            note_capability_probe(session, "training_status", "empty")
    session.add(row)


def _sync_stage1(session: Session, today: date, summary: dict) -> bool:
    """Fast, resumable first-account bootstrap.  Do not add Stage 2 here."""
    from metrics.freshness import capability_fetch_decision, note_capability_from_device, note_capability_observed, note_capability_probe

    def done(name: str) -> bool:
        return bool(_get_state(session, _stage1_key(name)))

    def mark(name: str) -> None:
        _set_state(session, _stage1_key(name), "complete")

    try:
        # 1. Device upload and capability.  Do not call activity preflight here:
        # its extra request would violate the bootstrap request order.
        if not done("device"):
            device = client.device_last_used() or {}
            note_capability_from_device(session, device)
            upload = _device_upload_iso_from_payload(device)
            _set_state(session, "device_last_upload", upload)
            if upload:
                _set_state(session, "last_processed_device_upload", upload)
            mark("device")

        # 2. Today's sleep.
        if not done("today_sleep"):
            if _sync_sleep(session, today) is False:
                return False
            mark("today_sleep")

        # 3. Today's Training Readiness, supported or one bounded unknown probe.
        if not done("training_readiness"):
            decision = capability_fetch_decision(session, "training_readiness", "stage1")
            if decision in {"fetch_supported", "probe_unknown"}:
                current_probe_outcome: str | None = None
                recovery_error_code: str | None = None
                try:
                    payload = normalize_training_readiness(client.training_readiness(today), today)
                except GarminConnectTooManyRequestsError:
                    _set_readiness_outcome(session, today, _SignalOutcome("error", "rate_limited"))
                    _set_recovery_outcome(session, today, _SignalOutcome("error", "rate_limited"))
                    if decision == "probe_unknown":
                        note_capability_probe(session, "training_readiness", "rate_limited")
                    raise
                except GarminConnectAuthenticationError:
                    _set_readiness_outcome(session, today, _SignalOutcome("error", "authentication_required"))
                    _set_recovery_outcome(session, today, _SignalOutcome("error", "authentication_required"))
                    if decision == "probe_unknown":
                        note_capability_probe(session, "training_readiness", "authentication_error")
                    raise
                except Exception as exc:
                    if _is_auth_error(exc):
                        _set_readiness_outcome(session, today, _SignalOutcome("error", "authentication_required"))
                        _set_recovery_outcome(session, today, _SignalOutcome("error", "authentication_required"))
                        if decision == "probe_unknown":
                            note_capability_probe(session, "training_readiness", "authentication_error")
                        raise GarminConnectAuthenticationError("Garmin Connect authentication failed") from exc
                    recovery_error_code = _freshness_error_code(exc)
                    _set_readiness_outcome(session, today, _SignalOutcome("error", recovery_error_code))
                    _set_recovery_outcome(session, today, _SignalOutcome("error", recovery_error_code))
                    if decision == "probe_unknown":
                        current_probe_outcome = "ordinary_error"
                        note_capability_probe(session, "training_readiness", current_probe_outcome)
                    logger.warning("Stage 1 Training Readiness probe failed")
                    payload = None
                row = session.get(DailyHealth, today) or DailyHealth(day=today)
                if payload is not None:
                    _set_readiness_outcome(session, today, _SignalOutcome("fresh"))
                    row.training_readiness = payload["trainingReadiness"]
                    note_capability_observed(session)
                    _persist_recovery_time(
                        session, row, payload,
                        fallback_observed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                    )
                else:
                    if recovery_error_code is None:
                        _set_readiness_outcome(session, today, _SignalOutcome("missing"))
                        _set_recovery_outcome(session, today, _SignalOutcome("missing"))
                    if decision == "probe_unknown" and current_probe_outcome is None:
                        note_capability_probe(session, "training_readiness", "empty")
                session.add(row)
            mark("training_readiness")  # unsupported and empty current responses are resolved.

        # 4. The seven-day wellness window.  Today's sleep was already fetched;
        # fetch the other six nights, then advance its cursor through today.
        window_start = today - timedelta(days=6)
        if not done("sleep"):
            for day in (window_start + timedelta(days=n) for n in range(6)):
                if _sync_sleep(session, day) is False:
                    return False
                _advance_resource_cursor(session, "sleep", day)
            _advance_resource_cursor(session, "sleep", today)
            mark("sleep")
        if not done("daily_health"):
            completed, first_gap = _sync_daily_health_window(
                session, window_start, today, current_optional=False,
            )
            if first_gap is not None:
                summary["errors"].append(f"daily_health failed at {first_gap}; it will retry from there.")
                return False
            mark("daily_health")
            summary["days"] = completed

        # 5. 30 days of summaries; no per-activity enrichment in Stage 1.
        vo2_values: list[tuple[str, float]] = []
        if not done("activities"):
            summary["activities"] = _sync_activities(
                session, today - timedelta(days=29), today, strength_limit=0,
                enrich=False, vo2_values=vo2_values,
            )
            # Keep the newest activity-summary VO2 value across later Stage 1
            # failures.  "none" is an intentional resolved absence, not a gap.
            latest_vo2 = max(vo2_values) if vo2_values else None
            _set_state(
                session,
                _stage1_key("vo2max_summary"),
                f"{latest_vo2[0]}|{latest_vo2[1]}" if latest_vo2 else "none",
            )
            _advance_resource_cursor(session, "activities", today)
            mark("activities")

        # 6. Set details only for the ten most recent fetched strength activities.
        if not done("strength_sets"):
            strength = (
                session.query(Activity)
                .filter(Activity.activity_type.ilike("%strength%") | Activity.activity_type.ilike("%weight%"))
                .filter(Activity.start_time >= datetime.combine(today - timedelta(days=29), datetime.min.time()))
                .order_by(Activity.start_time.desc())
                .limit(10)
                .all()
            )
            for activity in strength:
                if _sync_exercise_sets(session, activity.id) is False:
                    return False
            mark("strength_sets")

        # 7-8. Current-only slow values.  Never call the historical helpers.
        if not done("fitness_age"):
            _sync_current_fitness_age(session, today, context="stage1")
            mark("fitness_age")
        if not done("vo2max"):
            saved_vo2 = _get_state(session, _stage1_key("vo2max_summary"))
            if saved_vo2 and saved_vo2 != "none":
                value_date, value = saved_vo2.split("|", 1)
                _upsert_snapshot(session, "vo2max", [(value_date, float(value))])
            mark("vo2max")
            _clear_state(session, _stage1_key("vo2max_summary"))

        # 9. Training Status can self-discover with one Stage 1 probe.
        if not done("training_status"):
            decision = capability_fetch_decision(session, "training_status", "stage1")
            if decision in {"fetch_supported", "probe_unknown"}:
                current_probe_outcome = None
                try:
                    status = client.training_status(today)
                except GarminConnectTooManyRequestsError:
                    if decision == "probe_unknown":
                        note_capability_probe(session, "training_status", "rate_limited")
                    raise
                except GarminConnectAuthenticationError:
                    if decision == "probe_unknown":
                        note_capability_probe(session, "training_status", "authentication_error")
                    raise
                except Exception as exc:
                    if _is_auth_error(exc):
                        if decision == "probe_unknown":
                            note_capability_probe(session, "training_status", "authentication_error")
                        raise GarminConnectAuthenticationError("Garmin Connect authentication failed") from exc
                    if decision == "probe_unknown":
                        current_probe_outcome = "ordinary_error"
                        note_capability_probe(session, "training_status", current_probe_outcome)
                    logger.warning("Stage 1 Training Status probe failed")
                    status = None
                if isinstance(status, dict) and isinstance(status.get("mostRecentTrainingStatus"), str):
                    row = session.get(DailyHealth, today) or DailyHealth(day=today)
                    row.training_status = status.get("mostRecentTrainingStatus")
                    session.add(row)
                    note_capability_observed(session, "training_status")
                elif decision == "probe_unknown" and current_probe_outcome is None:
                    note_capability_probe(session, "training_status", "empty")
            mark("training_status")

        _set_state(session, _STAGE1_COMPLETE, "complete")
        return True
    except GarminConnectTooManyRequestsError as exc:
        until = _note_rate_limited(session)
        summary["errors"].append(f"Rate limited during Stage 1: {exc}")
        summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
        return False
    except GarminConnectAuthenticationError:
        summary["skipped"] = True
        summary["code"] = "authentication_required"
        summary["errors"].append("Garmin Connect session expired. Please re-authenticate your Garmin account.")
        return False


# --- orchestration --------------------------------------------------------
def _freshness_error_code(exc: Exception) -> str:
    name = type(exc).__name__.lower()
    if "authentication" in name or "mfa" in name:
        return "authentication_required"
    if isinstance(exc, GarminConnectTooManyRequestsError):
        return "rate_limited"
    if "timeout" in name:
        return "timeout"
    return "endpoint_error"


def _priority_individual_health(
    session: Session, day: date, device_upload_at: datetime | None, *, include_rhr_stress: bool = True,
) -> None:
    """Fetch independent facts; auth/429 stop the priority read circuit."""
    from metrics.freshness import (
        ERROR, FRESH, HRV, HRV_STATUS, MISSING, RESTING_HR, STRESS,
        UNSUPPORTED, capability_state, record_signal,
    )

    row = session.get(DailyHealth, day) or DailyHealth(day=day)
    try:
        normalized = normalize_hrv_data(client.hrv(day), day)
        overnight = _apply_normalized_hrv(session, row, normalized)
        status = normalized.status if normalized else None
        record_signal(session, HRV, day, FRESH if overnight else MISSING, "get_hrv_data", device_upload_at=device_upload_at)
        status_state = FRESH if status else (UNSUPPORTED if capability_state(session, "hrv_status") == "unsupported" else MISSING)
        record_signal(session, HRV_STATUS, day, status_state, "get_hrv_data", device_upload_at=device_upload_at)
    except Exception as exc:
        code = "rate_limited" if isinstance(exc, GarminConnectTooManyRequestsError) else "authentication_required" if isinstance(exc, GarminConnectAuthenticationError) or _is_auth_error(exc) else _freshness_error_code(exc)
        record_signal(session, HRV, day, ERROR, "get_hrv_data", device_upload_at=device_upload_at, error_code=code)
        record_signal(session, HRV_STATUS, day, ERROR, "get_hrv_data", device_upload_at=device_upload_at, error_code=code)
        if isinstance(exc, GarminConnectTooManyRequestsError):
            raise
        if isinstance(exc, GarminConnectAuthenticationError) or _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed") from exc
    if not include_rhr_stress:
        session.add(row)
        return
    fetches = (
        (RESTING_HR, "get_rhr_day", client.resting_hr, lambda data: (
            (_g(data, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE", default=[]) or [{}])[0].get("value")
        )),
        (STRESS, "get_all_day_stress", client.stress, lambda data: (data or {}).get("avgStressLevel")),
    )
    for signal, endpoint, fetcher, extract in fetches:
        try:
            value = extract(fetcher(day))
            if signal == RESTING_HR:
                row.resting_hr = value
            else:
                row.stress_avg = value
            record_signal(
                session, signal, day, FRESH if value is not None else MISSING, endpoint,
                device_upload_at=device_upload_at,
            )
        except Exception as exc:
            if isinstance(exc, GarminConnectTooManyRequestsError):
                record_signal(session, signal, day, ERROR, endpoint, device_upload_at=device_upload_at, error_code="rate_limited")
                raise
            if isinstance(exc, GarminConnectAuthenticationError) or _is_auth_error(exc):
                record_signal(session, signal, day, ERROR, endpoint, device_upload_at=device_upload_at, error_code="authentication_required")
                raise GarminConnectAuthenticationError("Garmin Connect authentication failed") from exc
            record_signal(
                session, signal, day, ERROR, endpoint,
                device_upload_at=device_upload_at, error_code=_freshness_error_code(exc),
            )
    session.add(row)


def _persist_current_request_outcomes(
    session: Session, day: date, *, device_upload_at: datetime | None = None,
) -> None:
    """Persist only outcomes from current-day endpoint requests before a stop."""
    from metrics.freshness import ERROR, RECOVERY_TIME, TRAINING_READINESS, UNSUPPORTED, capability_state, record_signal

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    hrv_outcomes = session.info.get("hrv_request_outcomes", {}).get(day, {})
    for signal, endpoint in (("hrv", "get_hrv_data"), ("hrv_status", "get_hrv_data")):
        outcome = hrv_outcomes.get(signal)
        if outcome and outcome.requested:
            record_signal(session, signal, day, outcome.state, endpoint, fetched_at=now, device_upload_at=device_upload_at, error_code=outcome.error_code)
    readiness = session.info.get("training_readiness_request_outcomes", {}).get(day)
    if readiness and readiness.requested:
        record_signal(session, TRAINING_READINESS, day, readiness.state, "get_training_readiness", fetched_at=now, device_upload_at=device_upload_at, error_code=readiness.error_code)
    recovery = session.info.get("recovery_time_request_outcomes", {}).get(day)
    if recovery and recovery.requested:
        state = UNSUPPORTED if capability_state(session, "recovery_time_connect") == "unsupported" else recovery.state
        record_signal(session, RECOVERY_TIME, day, state, "get_training_readiness", fetched_at=now, device_upload_at=device_upload_at, error_code=recovery.error_code if state != UNSUPPORTED else None)


def _record_full_sync_freshness(
    session: Session,
    day: date,
    device_upload_iso: str | None,
) -> None:
    """Align today's full-sync facts with the per-signal freshness contract."""
    from metrics.freshness import (
        ERROR,
        EXPECTED_PENDING,
        FRESH,
        HRV,
        HRV_STATUS,
        MISSING,
        RECOVERY_TIME,
        RESTING_HR,
        SLEEP,
        SLEEP_SCORE,
        STRESS,
        TRAINING_READINESS,
        UNSUPPORTED,
        capability_state,
        record_signal,
    )
    from time_utils import get_local_tz

    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    parsed_upload = _parse_state_dt(device_upload_iso)
    device_upload_at = parsed_upload.replace(tzinfo=None) if parsed_upload else None
    sleep = session.get(Sleep, day)
    health = session.get(DailyHealth, day)

    sleep_state = FRESH if sleep and sleep.total_s and sleep.total_s > 0 else MISSING
    if sleep_state == FRESH and sleep.sleep_end_time and device_upload_at:
        sleep_end = sleep.sleep_end_time
        if sleep_end.tzinfo is None:
            sleep_end = get_local_tz().localize(sleep_end)
        sleep_end_utc = sleep_end.astimezone(timezone.utc).replace(tzinfo=None)
        if device_upload_at < sleep_end_utc:
            sleep_state = EXPECTED_PENDING
    record_signal(
        session, SLEEP, day, sleep_state, "get_sleep_data",
        fetched_at=fetched_at, device_upload_at=device_upload_at,
    )
    record_signal(
        session, SLEEP_SCORE, day,
        FRESH if sleep and sleep.score is not None and sleep_state == FRESH else MISSING,
        "get_sleep_data", fetched_at=fetched_at, device_upload_at=device_upload_at,
    )

    capability = capability_state(session)
    hrv_status_capability = capability_state(session, "hrv_status")
    recovery_time_capability = capability_state(session, "recovery_time_connect")
    hrv_outcomes = session.info.get("hrv_request_outcomes", {}).get(day, {})
    hrv_outcome = hrv_outcomes.get("hrv", _SignalOutcome("missing", requested=False))
    hrv_status_outcome = hrv_outcomes.get("hrv_status", _SignalOutcome("missing", requested=False))
    recovery_outcome = session.info.get("recovery_time_request_outcomes", {}).get(
        day, _SignalOutcome("missing", requested=False),
    )
    readiness_outcome = session.info.get("training_readiness_request_outcomes", {}).get(
        day, _SignalOutcome("missing", requested=False),
    )
    record_signal(
        session, HRV, day, hrv_outcome.state, "get_hrv_data",
        fetched_at=fetched_at, device_upload_at=device_upload_at,
        error_code=hrv_outcome.error_code,
    )
    if capability == "unsupported":
        record_signal(
            session, TRAINING_READINESS, day,
            UNSUPPORTED, "device_capability",
            fetched_at=fetched_at, device_upload_at=device_upload_at,
        )
    elif readiness_outcome.requested:
        record_signal(
            session, TRAINING_READINESS, day,
            readiness_outcome.state,
            "get_training_readiness", fetched_at=fetched_at, device_upload_at=device_upload_at,
            error_code=readiness_outcome.error_code,
        )
    else:
        record_signal(
            session, TRAINING_READINESS, day, MISSING,
            "capability_probe_not_due" if capability == "unknown" else "get_training_readiness",
            fetched_at=fetched_at, device_upload_at=device_upload_at,
        )

    if capability in {"unsupported", "unknown"}:
        individual = (
            (RESTING_HR, health.resting_hr if health else None, "get_rhr_day"),
            (STRESS, health.stress_avg if health else None, "get_all_day_stress"),
        )
        for signal, value, endpoint in individual:
            record_signal(
                session, signal, day, FRESH if value is not None else MISSING, endpoint,
                fetched_at=fetched_at, device_upload_at=device_upload_at,
            )
    record_signal(
        session, HRV_STATUS, day,
        hrv_status_outcome.state if hrv_status_outcome.state == ERROR else (
            UNSUPPORTED if hrv_status_capability == "unsupported" else hrv_status_outcome.state
        ),
        "get_hrv_data", fetched_at=fetched_at, device_upload_at=device_upload_at,
        error_code=hrv_status_outcome.error_code,
    )
    record_signal(
        session, RECOVERY_TIME, day,
        UNSUPPORTED if recovery_time_capability == "unsupported" else recovery_outcome.state,
        "get_training_readiness", fetched_at=fetched_at, device_upload_at=device_upload_at,
        error_code=recovery_outcome.error_code if recovery_time_capability != "unsupported" else None,
    )


def _run_priority_sync() -> dict:
    """Fetch and commit only facts needed by today's morning decision."""
    from metrics.freshness import (
        ERROR,
        EXPECTED_PENDING,
        FRESH,
        HRV_STATUS,
        MISSING,
        RECOVERY_TIME,
        SLEEP,
        SLEEP_SCORE,
        TRAINING_READINESS,
        UNSUPPORTED,
        capability_state,
        capability_fetch_decision,
        morning_freshness,
        note_capability_from_device,
        note_capability_observed,
        note_capability_probe,
        record_signal,
    )
    from time_utils import get_local_date, get_local_tz

    target = get_local_date()
    summary = {"priority": True, "day": target.isoformat(), "errors": []}
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as session:
        device_upload_at = None
        def finalize() -> dict:
            _set_state(session, "last_priority_sync_at", fetched_at.isoformat())
            _set_state(session, "overnight_facts_updated_at", fetched_at.isoformat())
            summary.update(morning_freshness(session, target))
            return summary

        def stop(endpoint: str, exc: Exception, *, probe: bool = False, record_freshness: bool = True, signals: tuple[str, ...] | None = None) -> dict:
            auth = isinstance(exc, GarminConnectAuthenticationError) or _is_auth_error(exc)
            limited = isinstance(exc, GarminConnectTooManyRequestsError)
            code = "authentication_required" if auth else "rate_limited" if limited else _freshness_error_code(exc)
            if probe:
                note_capability_probe(
                    session, TRAINING_READINESS,
                    "authentication_error" if auth else "rate_limited" if limited else "ordinary_error",
                    observed_at=fetched_at,
                )
            if limited:
                _note_rate_limited(session)
            if auth:
                summary["code"] = "authentication_required"
                summary["skipped"] = True
            summary["errors"].append(f"{endpoint}:{code}")
            if record_freshness:
                affected = signals or ((SLEEP,) if endpoint == "sleep" else (TRAINING_READINESS,))
                for signal in affected:
                    if signal == RECOVERY_TIME and capability_state(session, "recovery_time_connect") == "unsupported":
                        record_signal(session, signal, target, UNSUPPORTED, "device_capability", fetched_at=fetched_at, device_upload_at=device_upload_at)
                    else:
                        record_signal(
                            session, signal, target, ERROR,
                            "get_sleep_data" if signal == SLEEP else "get_training_readiness",
                            fetched_at=fetched_at, device_upload_at=device_upload_at, error_code=code,
                        )
            return finalize()
        try:
            device = client.device_last_used() or {}
            note_capability_from_device(session, device, observed_at=fetched_at)
            upload_iso = _device_upload_iso_from_payload(device)
            _set_state(session, "device_last_upload", upload_iso)
            parsed_upload = _parse_state_dt(upload_iso)
            device_upload_at = parsed_upload.replace(tzinfo=None) if parsed_upload else None
        except Exception as exc:
            code = _freshness_error_code(exc)
            if isinstance(exc, GarminConnectTooManyRequestsError):
                _note_rate_limited(session)
            if isinstance(exc, GarminConnectAuthenticationError) or _is_auth_error(exc):
                summary["code"] = "authentication_required"
                summary["skipped"] = True
                code = "authentication_required"
            summary["errors"].append(f"device:{code}")
            for signal, endpoint in ((SLEEP, "get_sleep_data"), (TRAINING_READINESS, "get_training_readiness")):
                record_signal(session, signal, target, ERROR, endpoint, fetched_at=fetched_at, error_code=code)
            return finalize()

        sleep_ok = False
        try:
            sleep_ok = _sync_sleep(session, target)
        except Exception as exc:
            if isinstance(exc, (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError)) or _is_auth_error(exc):
                return stop("sleep", exc)
            code = _freshness_error_code(exc)
            summary["errors"].append(f"sleep:{code}")
            record_signal(session, SLEEP, target, ERROR, "get_sleep_data", fetched_at=fetched_at, device_upload_at=device_upload_at, error_code=code)
        sleep = session.get(Sleep, target)
        if sleep_ok and sleep and sleep.total_s and sleep.total_s > 0:
            state = FRESH
            if sleep.sleep_end_time and device_upload_at:
                local_end = get_local_tz().localize(sleep.sleep_end_time).astimezone(timezone.utc).replace(tzinfo=None)
                if device_upload_at < local_end:
                    state = EXPECTED_PENDING
            record_signal(
                session, SLEEP, target, state, "get_sleep_data",
                fetched_at=fetched_at, device_upload_at=device_upload_at,
            )
            record_signal(
                session, SLEEP_SCORE, target, FRESH if sleep.score is not None else MISSING,
                "get_sleep_data", fetched_at=fetched_at, device_upload_at=device_upload_at,
            )
        elif not summary["errors"]:
            record_signal(
                session, SLEEP, target, MISSING, "get_sleep_data",
                fetched_at=fetched_at, device_upload_at=device_upload_at,
            )
            record_signal(
                session, SLEEP_SCORE, target, MISSING, "get_sleep_data",
                fetched_at=fetched_at, device_upload_at=device_upload_at,
            )

        readiness_observed = False
        capability = capability_state(session)
        decision = capability_fetch_decision(session, "training_readiness", "priority")
        if decision in {"skip_unsupported", "skip_unknown_not_due"}:
            record_signal(
                session, TRAINING_READINESS, target,
                UNSUPPORTED if capability == "unsupported" else MISSING,
                "device_capability" if capability == "unsupported" else "capability_probe_not_due",
                fetched_at=fetched_at, device_upload_at=device_upload_at,
            )
            recovery_capability = capability_state(session, "recovery_time_connect")
            record_signal(
                session, RECOVERY_TIME, target,
                UNSUPPORTED if recovery_capability == "unsupported" else MISSING,
                "device_capability" if recovery_capability == "unsupported" else "capability_probe_not_due",
                fetched_at=fetched_at, device_upload_at=device_upload_at,
            )
            try:
                _priority_individual_health(session, target, device_upload_at)
            except (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError) as exc:
                return stop("individual_health", exc, record_freshness=False)
        else:
            try:
                payload = normalize_training_readiness(
                    client.training_readiness(target),
                    target,
                )
                value = payload["trainingReadiness"] if payload is not None else None
                health = session.get(DailyHealth, target) or DailyHealth(day=target)
                if value is not None:
                    health.training_readiness = int(value)
                    recovery_observed = _persist_recovery_time(
                        session, health, payload, fallback_observed_at=fetched_at,
                    )
                    session.add(health)
                    note_capability_observed(session, observed_at=fetched_at)
                    readiness_observed = True
                    record_signal(
                        session, TRAINING_READINESS, target, FRESH, "get_training_readiness",
                        fetched_at=fetched_at, device_upload_at=device_upload_at,
                    )
                    record_signal(
                        session, RECOVERY_TIME, target,
                        FRESH if recovery_observed else MISSING,
                        "get_training_readiness", fetched_at=fetched_at,
                        device_upload_at=device_upload_at,
                    )
                else:
                    if decision == "probe_unknown":
                        note_capability_probe(session, "training_readiness", "empty", observed_at=fetched_at)
                    record_signal(
                        session, TRAINING_READINESS, target, MISSING, "get_training_readiness",
                        fetched_at=fetched_at, device_upload_at=device_upload_at,
                    )
                    record_signal(
                        session, RECOVERY_TIME, target, MISSING, "get_training_readiness",
                        fetched_at=fetched_at, device_upload_at=device_upload_at,
                    )
                    if decision == "probe_unknown":
                        try:
                            _priority_individual_health(session, target, device_upload_at)
                        except (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError) as exc:
                            return stop("individual_health", exc, record_freshness=False)
            except Exception as exc:
                if isinstance(exc, (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError)) or _is_auth_error(exc):
                    return stop("training_readiness", exc, probe=decision == "probe_unknown", signals=(TRAINING_READINESS, RECOVERY_TIME))
                code = _freshness_error_code(exc)
                if decision == "probe_unknown":
                    probe_outcome = "rate_limited" if code == "rate_limited" else "authentication_error" if code == "authentication_required" else "ordinary_error"
                    note_capability_probe(session, "training_readiness", probe_outcome, observed_at=fetched_at)
                summary["errors"].append(f"training_readiness:{code}")
                record_signal(
                    session, TRAINING_READINESS, target, ERROR, "get_training_readiness",
                    fetched_at=fetched_at, device_upload_at=device_upload_at, error_code=code,
                )
                record_signal(
                    session, RECOVERY_TIME, target, ERROR, "get_training_readiness",
                    fetched_at=fetched_at, device_upload_at=device_upload_at, error_code=code,
                )
                if decision == "probe_unknown":
                    try:
                        _priority_individual_health(session, target, device_upload_at)
                    except (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError) as inner:
                        return stop("individual_health", inner, record_freshness=False)

        if readiness_observed:
            try:
                _priority_individual_health(
                    session, target, device_upload_at, include_rhr_stress=False,
                )
            except (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError) as exc:
                return stop("hrv", exc, record_freshness=False)

        return finalize()


def _persist_endpoint_telemetry(payload: dict) -> None:
    try:
        with get_session() as session:
            _set_state(session, "last_garmin_endpoint_telemetry", json.dumps(payload, separators=(",", ":")))
    except Exception:
        logger.warning("Unable to persist Garmin endpoint telemetry")


def run_priority_sync() -> dict:
    summary: dict | None = None
    with telemetry_scope("priority") as collector:
        try:
            summary = _run_priority_sync()
            return summary
        finally:
            payload = collector.finish()
            if summary is not None:
                summary["endpoint_telemetry"] = payload
            _persist_endpoint_telemetry(payload)
            logger.info("garmin_endpoint_telemetry %s", json.dumps(payload, separators=(",", ":")))


def _sync_resource_days(
    session: Session, resource: str, start: date, today: date, sync_day, summary: dict
) -> Optional[int]:
    """Sync one daily resource in order, preserving its cursor at the first gap."""
    completed = 0
    day = start
    while day <= today:
        try:
            if sync_day(session, day) is False:
                summary["errors"].append(f"{resource} failed at {day}; it will retry from there.")
                break
            _advance_resource_cursor(session, resource, day)
            completed += 1
        except GarminConnectAuthenticationError:
            raise
        except GarminConnectTooManyRequestsError as exc:
            until = _note_rate_limited(session)
            summary["errors"].append(f"Rate limited on {resource} at {day}: {exc}")
            summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
            return None
        except Exception as exc:
            summary["errors"].append(f"{resource} failed at {day}: {exc}")
            break
        day += timedelta(days=1)
    return completed


def _stage2_gap(session: Session, key: str, anchor: date, first_day: date) -> Optional[date]:
    """Return a normalized Stage 2 next-gap day, seeding its journal once.

    Stage 1 already owns the recent window, so deployed journals that still
    point into that overlap are fast-forwarded without making a Garmin call.
    """
    value = _get_state(session, key)
    if value == "complete":
        return None
    if value:
        gap = _parse_state_date(value)
        if first_day < gap <= anchor:
            _set_state(session, key, first_day.isoformat())
            return first_day
        return gap
    _set_state(session, key, first_day.isoformat())
    return first_day


def _advance_stage2_gap(session: Session, key: str, day: date, target: date) -> None:
    next_day = day - timedelta(days=1)
    _set_state(session, key, "complete" if next_day < target else next_day.isoformat())


def _run_stage2_summary_backfill(session: Session, today: date, summary: dict) -> bool:
    """Run exactly one resumable, summary-only Stage 2 unit.

    This journal intentionally does not inspect stored rows: a successful call is
    recorded per resource, which makes resume behavior deterministic after partial
    failures and keeps this separate from normal incremental cursors.
    """
    if _get_state(session, _STAGE1_COMPLETE) != "complete":
        return False
    if _get_state(session, _STAGE2_SUMMARY_COMPLETE) == "complete":
        return False

    anchor = _parse_state_date(_get_state(session, _STAGE2_ANCHOR))
    if anchor is None:
        anchor = today
        _set_state(session, _STAGE2_ANCHOR, anchor.isoformat())
    wellness_target = anchor - timedelta(days=_STAGE2_WELLNESS_DAYS - 1)
    activity_target = anchor - timedelta(days=_STAGE2_ACTIVITY_DAYS - 1)
    wellness_first_day = anchor - timedelta(days=7)
    activity_first_day = anchor - timedelta(days=30)
    sleep_gap = _stage2_gap(session, _STAGE2_SLEEP_GAP, anchor, wellness_first_day)
    health_gap = _stage2_gap(session, _STAGE2_DAILY_HEALTH_GAP, anchor, wellness_first_day)
    activity_gap = _stage2_gap(session, _STAGE2_ACTIVITY_GAP, anchor, activity_first_day)

    # Wellness is newest-first across both resources.  A newer unresolved
    # health gap must be retried before sleep is allowed to move further back.
    if sleep_gap is not None or health_gap is not None:
        day = max(d for d in (sleep_gap, health_gap) if d is not None)
        try:
            if sleep_gap == day:
                if _sync_sleep(session, day) is False:
                    summary["errors"].append(f"Stage 2 sleep failed at {day}; it will retry.")
                else:
                    _advance_stage2_gap(session, _STAGE2_SLEEP_GAP, day, wellness_target)
            if health_gap == day:
                if _sync_daily_health(session, day, current_optional=False) is False:
                    summary["errors"].append(f"Stage 2 daily health failed at {day}; it will retry.")
                else:
                    _advance_stage2_gap(session, _STAGE2_DAILY_HEALTH_GAP, day, wellness_target)
        except GarminConnectTooManyRequestsError as exc:
            until = _note_rate_limited(session)
            summary["errors"].append(f"Rate limited on Stage 2 wellness at {day}: {exc}")
            summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
        except GarminConnectAuthenticationError:
            summary["skipped"] = True
            summary["code"] = "authentication_required"
            summary["errors"].append("Garmin Connect session expired. Please re-authenticate your Garmin account.")
        except Exception as exc:
            summary["errors"].append(f"Stage 2 wellness failed at {day}: {exc}")
        return True

    if activity_gap is not None:
        start = max(activity_target, activity_gap - timedelta(days=_STAGE2_ACTIVITY_CHUNK_DAYS - 1))
        try:
            _sync_activities(session, start, activity_gap, enrich=False)
            _advance_stage2_gap(session, _STAGE2_ACTIVITY_GAP, start, activity_target)
            if _get_state(session, _STAGE2_ACTIVITY_GAP) == "complete":
                _set_state(session, _STAGE2_SUMMARY_COMPLETE, "complete")
        except GarminConnectTooManyRequestsError as exc:
            until = _note_rate_limited(session)
            summary["errors"].append(f"Rate limited on Stage 2 activities: {exc}")
            summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
        except GarminConnectAuthenticationError:
            summary["skipped"] = True
            summary["code"] = "authentication_required"
            summary["errors"].append("Garmin Connect session expired. Please re-authenticate your Garmin account.")
        except Exception as exc:
            summary["errors"].append(f"Stage 2 activities failed: {exc}")
        return True

    _set_state(session, _STAGE2_SUMMARY_COMPLETE, "complete")
    return True


def _stage2_strength_candidates(session: Session, anchor: date) -> list[int]:
    """Return the immutable Stage 2 strength candidate snapshot, creating it once."""
    saved = _get_state(session, _STAGE2_STRENGTH_CANDIDATES)
    if saved is not None:
        try:
            candidate_ids = json.loads(saved)
            if isinstance(candidate_ids, list) and all(isinstance(value, int) for value in candidate_ids):
                return candidate_ids
        except (TypeError, ValueError, json.JSONDecodeError):
            logger.warning("Invalid Stage 2 strength candidate journal; retaining no new candidates.")
            return []

    start = datetime.combine(anchor - timedelta(days=_STAGE2_ACTIVITY_DAYS - 1), datetime.min.time())
    end = datetime.combine(anchor + timedelta(days=1), datetime.min.time())
    completion_prefix = "activity_strength_sets_checked:"
    resolved_ids = {
        int(row.key.removeprefix(completion_prefix))
        for row in session.query(SyncState).filter(
            SyncState.key.like(f"{completion_prefix}%"), SyncState.value == "complete"
        )
        if row.key.removeprefix(completion_prefix).isdigit()
    }
    activities = (
        session.query(Activity.id)
        .filter(Activity.activity_type.ilike("%strength%") | Activity.activity_type.ilike("%weight%"))
        .filter(Activity.start_time >= start, Activity.start_time < end)
        .filter(~Activity.sets.any())
        .filter(~Activity.id.in_(resolved_ids) if resolved_ids else True)
        .order_by(Activity.start_time.desc(), Activity.id.desc())
        .limit(20)
        .all()
    )
    candidate_ids = [activity_id for (activity_id,) in activities]
    _set_state(session, _STAGE2_STRENGTH_CANDIDATES, json.dumps(candidate_ids))
    _set_state(session, _STAGE2_STRENGTH_NEXT_INDEX, "0")
    return candidate_ids


def _run_stage2_strength_backfill(session: Session, today: date, summary: dict) -> bool:
    """Resolve at most one fixed Stage 2 strength candidate per scheduled sync."""
    if (
        _get_state(session, _STAGE1_COMPLETE) != "complete"
        or _get_state(session, _STAGE2_SUMMARY_COMPLETE) != "complete"
        or _get_state(session, _STAGE2_STRENGTH_COMPLETE) == "complete"
    ):
        return False

    anchor = _parse_state_date(_get_state(session, _STAGE2_ANCHOR))
    if anchor is None:
        # Legacy installations may have the summary marker but no anchor.  Seed
        # it once so this strength journal still has a fixed, auditable window.
        anchor = today
        _set_state(session, _STAGE2_ANCHOR, anchor.isoformat())
    candidate_ids = _stage2_strength_candidates(session, anchor)
    try:
        next_index = int(_get_state(session, _STAGE2_STRENGTH_NEXT_INDEX) or "0")
    except ValueError:
        next_index = 0
        _set_state(session, _STAGE2_STRENGTH_NEXT_INDEX, "0")

    if next_index >= len(candidate_ids):
        _set_state(session, _STAGE2_STRENGTH_COMPLETE, "complete")
        return True

    activity_id = candidate_ids[next_index]
    try:
        if _sync_exercise_sets(session, activity_id) is False:
            summary["errors"].append(
                f"Stage 2 strength sets failed for activity {activity_id}; it will retry."
            )
            return True
    except GarminConnectTooManyRequestsError as exc:
        until = _note_rate_limited(session)
        summary["errors"].append(f"Rate limited on Stage 2 strength activity {activity_id}: {exc}")
        summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
        return True

    next_index += 1
    _set_state(session, _STAGE2_STRENGTH_NEXT_INDEX, str(next_index))
    if next_index >= len(candidate_ids):
        _set_state(session, _STAGE2_STRENGTH_COMPLETE, "complete")
    return True


def _maybe_run_weekly_slow_metrics(
    session: Session, today: date, summary: dict, *, full: bool, force: bool, allow_backfill: bool,
) -> bool:
    if not _slow_metrics_due(session, today, full, force, allow_backfill):
        return True
    try:
        _run_weekly_slow_metrics(session, today)
        return True
    except GarminConnectTooManyRequestsError as exc:
        until = _note_rate_limited(session)
        summary["errors"].append(f"Rate limited on weekly slow metrics: {exc}")
        summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
    except GarminConnectAuthenticationError:
        summary["skipped"] = True
        summary["code"] = "authentication_required"
        summary["errors"].append("Garmin Connect session expired. Please re-authenticate your Garmin account.")
    return False


def _run_sync(full: bool = False, force: bool = False, allow_backfill: bool = False) -> dict:
    """Sync new data since last run (or backfill on first run / full=True).

    Returns a summary dict for display in the UI.
    """
    today = _local_today()
    summary = {"activities": 0, "program_matches": 0, "days": 0, "errors": [], "skipped": False}
    preflight = None

    def drain_progression() -> None:
        """Retry bounded local derived work on every safe successful path."""
        if _is_in_cooldown(session)[0] or summary.get("code") == "authentication_required":
            return
        try:
            from coach.strength_progression_integration import process_pending_activity_recalculations
            from coach.strength_progression_notifications import (
                bridge_pending_progression_notifications, record_material_proposals,
            )
            report = process_pending_activity_recalculations(session, limit=50)
            if report.boundary_id and report.material_proposal_changes:
                recorded = record_material_proposals(session, boundary_id=report.boundary_id,
                    changes=report.material_proposal_changes, now=_utc_now().replace(tzinfo=None))
                if recorded.batch_id:
                    # Intent remains durable if this small bridge savepoint fails.
                    try:
                        with session.begin_nested():
                            bridge_pending_progression_notifications(session, now=_utc_now().replace(tzinfo=None),
                                limit=1, batch_ids=(recorded.batch_id,))
                    except Exception:
                        logger.exception("strength progression notification bridge failed")
        except Exception:
            # The integration normally contains per-activity failures.  This
            # guard keeps an unexpected local retry error out of Garmin sync.
            logger.exception("strength progression retry batch failed")

    with get_session() as session:
        in_cooldown, cooldown_until = _is_in_cooldown(session)
        if in_cooldown:
            summary["skipped"] = True
            summary["errors"].append(
                "Garmin is in local cooldown after a 429 until "
                f"{cooldown_until.isoformat(timespec='seconds') if cooldown_until else 'later'}."
            )
            return summary

        # A clean account gets the bounded usable bootstrap.  Legacy cursors,
        # resource cursors, or existing synced rows are all treated as real
        # progress, so upgrading an installation never restarts history.
        if (
            not full
            and not _get_state(session, _STAGE1_COMPLETE)
            and not _has_meaningful_sync_progress(session)
        ):
            _sync_stage1(session, today, summary)
            if summary["activities"] or summary["days"]:
                _set_state(session, "last_sync_at", _utc_now().isoformat(timespec="seconds"))
                _clear_cooldown(session)
            drain_progression()
            return summary

        resource_cursors = {
            resource: _resource_cursor(session, resource)
            for resource in _RESOURCE_CURSOR_KEYS
        }
        has_prior_progress = any(resource_cursors.values())
        if not full and not force:
            try:
                preflight = _preflight(session)
                if has_prior_progress and not preflight["device_changed"] and not preflight["activity_changed"]:
                    summary["skipped"] = True
                    summary["reason"] = "No new Garmin device upload or activity since the last sync."
                    if allow_backfill:
                        if _get_state(session, _STAGE2_SUMMARY_COMPLETE) == "complete":
                            _run_stage2_strength_backfill(session, today, summary)
                        else:
                            _run_stage2_summary_backfill(session, today, summary)
                        if _is_in_cooldown(session)[0] or summary.get("code") == "authentication_required":
                            return summary
                    drain_progression()
                    if not _maybe_run_weekly_slow_metrics(
                        session, today, summary, full=full, force=force, allow_backfill=allow_backfill,
                    ):
                        return summary
                    return summary
            except GarminConnectTooManyRequestsError as e:
                until = _note_rate_limited(session)
                summary["skipped"] = True
                summary["errors"].append(
                    "Rate limited during Garmin preflight. Cooling down until "
                    f"{until.isoformat(timespec='seconds')}: {e}"
                )
                return summary
            except GarminConnectAuthenticationError:
                summary["skipped"] = True
                summary["code"] = "authentication_required"
                summary["errors"].append(
                    "Garmin Connect session expired. Please re-authenticate your Garmin account."
                )
                return summary
            except Exception as e:
                summary["errors"].append(f"Preflight: {e}")

        activity_start = _resource_start(session, "activities", today, full)
        sleep_start = _resource_start(session, "sleep", today, full)
        health_start = _resource_start(session, "daily_health", today, full)

        try:
            summary["activities"] = _sync_activities(session, activity_start, today)
            _advance_resource_cursor(session, "activities", today)
        except GarminConnectTooManyRequestsError as e:
            until = _note_rate_limited(session)
            summary["errors"].append(f"Rate limited on activities: {e}")
            summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
            return summary
        except Exception as e:
            summary["errors"].append(f"Activities: {e}")
            if _is_auth_error(e):
                summary["skipped"] = True
                summary["code"] = "authentication_required"
                return summary

        try:
            from coach.program_state import reconcile_active_program
            summary["program_matches"] = reconcile_active_program(session)
        except Exception as e:
            summary["errors"].append(f"Program reconciliation: {e}")

        # Drain only explicitly dirtied local activities; no historical scan.
        drain_progression()

        if _workouts_due(session, full):
            try:
                _sync_workouts(session)
                _set_state(session, "last_workouts_sync_at", _utc_now().isoformat(timespec="seconds"))
            except GarminConnectTooManyRequestsError as e:
                until = _note_rate_limited(session)
                summary["errors"].append(f"Rate limited on workouts: {e}")
                summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
                return summary
            except Exception as e:
                summary["errors"].append(f"Workouts: {e}")

        try:
            sleep_days = _sync_resource_days(
                session, "sleep", sleep_start, today, _sync_sleep, summary
            )
            if sleep_days is None:
                return summary
            health_days, health_gap = _sync_daily_health_window(
                session, health_start, today, current_optional=True,
                optional_context="full" if full else "scheduled" if allow_backfill else "incremental",
            )
            if health_gap is not None:
                summary["errors"].append(f"daily_health failed at {health_gap}; it will retry from there.")
            summary["days"] = max(sleep_days, health_days)
        except GarminConnectTooManyRequestsError as exc:
            upload = _parse_state_dt(_get_state(session, "device_last_upload"))
            _persist_current_request_outcomes(
                session, today, device_upload_at=upload.replace(tzinfo=None) if upload else None,
            )
            until = _note_rate_limited(session)
            summary["errors"].append(f"Rate limited on daily_health: {exc}")
            summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
            return summary
        except GarminConnectAuthenticationError:
            upload = _parse_state_dt(_get_state(session, "device_last_upload"))
            _persist_current_request_outcomes(
                session, today, device_upload_at=upload.replace(tzinfo=None) if upload else None,
            )
            if config.MULTI_USER_ENABLED:
                try:
                    from tenant_context import current_tenant
                    from sync.garmin_registry import get_garmin_registry
                    tenant = current_tenant()
                    if tenant:
                        get_garmin_registry().evict(tenant.user_id)
                except Exception:
                    pass
            summary["skipped"] = True
            summary["code"] = "authentication_required"
            summary["errors"].append(
                "Garmin Connect session expired. Please re-authenticate your Garmin account."
            )
            return summary

        # Stage 2 is scheduled-only and always follows current work.  Full and
        # forced requests deliberately retain their existing semantics.
        if allow_backfill and not full and not force:
            if _get_state(session, _STAGE2_SUMMARY_COMPLETE) == "complete":
                _run_stage2_strength_backfill(session, today, summary)
            else:
                _run_stage2_summary_backfill(session, today, summary)
            if _is_in_cooldown(session)[0] or summary.get("code") == "authentication_required":
                return summary

        if not _maybe_run_weekly_slow_metrics(
            session, today, summary, full=full, force=force, allow_backfill=allow_backfill,
        ):
            return summary

        # Only stamp "last synced" if real data came through, so a sync that
        # failed immediately doesn't look successful in the UI.
        if summary["activities"] or summary["days"]:
            _set_state(
                session, "last_sync_at", _utc_now().isoformat(timespec="seconds")
            )
            _clear_cooldown(session)

        # Store the watch's last upload time so the dashboard can show both
        # the fetched time and the true device sync time.
        try:
            if preflight is None:
                preflight = _preflight(session)
            if summary["activities"] or summary["days"]:
                _store_preflight_markers(session, preflight)
            if (
                _resource_cursor(session, "sleep") == today
                and _resource_cursor(session, "daily_health") == today
            ):
                _record_full_sync_freshness(
                    session,
                    today,
                    preflight.get("device_upload") if preflight else _get_state(session, "device_last_upload"),
                )
        except GarminConnectTooManyRequestsError as exc:
            until = _note_rate_limited(session)
            summary["errors"].append(f"Rate limited during Garmin preflight: {exc}")
            summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
            return summary
        except Exception:
            pass

    # Recompute local derived metrics after every sync.
    try:
        from metrics.engine import recompute_all

        recompute_all()
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as e:
        summary["errors"].append(f"metrics recompute: {e}")
        import traceback, logging
        logging.getLogger(__name__).error("recompute_all failed: %s", traceback.format_exc())

    # Generate daily proactive coaching suggestion
    try:
        with get_session() as session:
            from db import DailyMetrics
            metrics = session.query(DailyMetrics).filter_by(day=_local_today()).first()
            if metrics:
                from notify.rules import check_and_notify_rules
                check_and_notify_rules(metrics)
            coach.generate_daily_suggestion(session)

    except Exception as e:
        summary["errors"].append(f"coach suggestion: {e}")

    return summary


def run_sync(full: bool = False, force: bool = False, allow_backfill: bool = False) -> dict:
    run_kind = "full" if full else "manual" if force else "scheduled" if allow_backfill else "incremental"
    summary: dict | None = None
    with telemetry_scope(run_kind) as collector:
        try:
            summary = _run_sync(full=full, force=force, allow_backfill=allow_backfill)
            return summary
        finally:
            payload = collector.finish()
            if summary is not None:
                summary["endpoint_telemetry"] = payload
            _persist_endpoint_telemetry(payload)
            logger.info("garmin_endpoint_telemetry %s", json.dumps(payload, separators=(",", ":")))


def _record_snapshot(session: Session, metric: str, observed_date: str, value: object) -> None:
    """Persist a current metric observation without rewriting local history.

    A sync overlap is often older than the stored observation.  Those rows must
    not replace newer data or manufacture a historical scan; changed newer
    values promote the old current observation to the existing previous slot.
    """
    numeric = _finite_number(value)
    try:
        observed = date.fromisoformat(observed_date[:10]).isoformat()
    except (AttributeError, TypeError, ValueError):
        return
    if numeric is None:
        return
    row = session.get(MetricSnapshot, metric) or MetricSnapshot(metric=metric)
    existing = _parse_state_date(row.value_date)
    incoming = date.fromisoformat(observed)
    if existing is not None and incoming < existing:
        return
    if existing is None:
        row.value, row.value_date = float(numeric), observed
    elif incoming == existing:
        # Same-day observations settle the current fact but retain history.
        row.value, row.value_date = float(numeric), observed
    elif row.value == float(numeric):
        row.value_date = observed
    else:
        row.prev_value, row.prev_date = row.value, row.value_date
        row.value, row.value_date = float(numeric), observed
    row.updated_at = datetime.now()
    session.add(row)


def _upsert_snapshot(session: Session, metric: str, history: list[tuple]) -> None:
    """Compatibility entry point for existing Stage 1 callers and tests."""
    if not history:
        return
    for observed_date, value in sorted(history):
        _record_snapshot(session, metric, observed_date, value)


def _sync_current_fitness_age(session: Session, today: date, *, context: str = "incremental") -> bool:
    """Read one current Fitness Age payload and use both supported values."""
    from metrics.freshness import capability_fetch_decision, note_capability_observed, note_capability_probe
    decision = capability_fetch_decision(session, "fitness_age", context)
    if decision in {"skip_unsupported", "skip_unknown_not_due"}:
        return True
    try:
        payload = client.fitness_age(today)
    except GarminConnectTooManyRequestsError:
        if decision == "probe_unknown":
            note_capability_probe(session, "fitness_age", "rate_limited")
        raise
    except GarminConnectAuthenticationError:
        if decision == "probe_unknown":
            note_capability_probe(session, "fitness_age", "authentication_error")
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            if decision == "probe_unknown":
                note_capability_probe(session, "fitness_age", "authentication_error")
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed") from exc
        if decision == "probe_unknown":
            note_capability_probe(session, "fitness_age", "ordinary_error")
        logger.warning("Fitness Age capability probe failed")
        return True
    if not isinstance(payload, dict):
        if decision == "probe_unknown":
            note_capability_probe(session, "fitness_age", "empty")
        return True  # valid empty/current response settles a weekly attempt
    observed = payload.get("lastUpdated")
    if not isinstance(observed, str) or _parse_state_date(observed[:10]) is None:
        observed = today.isoformat()
    _record_snapshot(session, "fitness_age", observed, payload.get("fitnessAge"))
    _record_snapshot(session, "target_fitness_age", observed, payload.get("achievableFitnessAge"))
    if _finite_number(payload.get("fitnessAge")) is not None:
        note_capability_observed(session, "fitness_age")
    elif decision == "probe_unknown":
        note_capability_probe(session, "fitness_age", "empty")
    return True


def _sync_current_training_status(session: Session, today: date, *, context: str) -> None:
    """Fetch recognized current status only when supported or due to probe."""
    from metrics.freshness import capability_fetch_decision, note_capability_observed, note_capability_probe
    decision = capability_fetch_decision(session, "training_status", context)
    if decision in {"skip_unsupported", "skip_unknown_not_due"}:
        return
    try:
        payload = client.training_status(today)
    except GarminConnectTooManyRequestsError:
        if decision == "probe_unknown":
            note_capability_probe(session, "training_status", "rate_limited")
        raise
    except GarminConnectAuthenticationError:
        if decision == "probe_unknown":
            note_capability_probe(session, "training_status", "authentication_error")
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            if decision == "probe_unknown":
                note_capability_probe(session, "training_status", "authentication_error")
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed") from exc
        if decision == "probe_unknown":
            note_capability_probe(session, "training_status", "ordinary_error")
        logger.warning("Training Status capability probe failed")
        return
    status = payload.get("mostRecentTrainingStatus") if isinstance(payload, dict) else None
    if isinstance(status, str) and status.strip():
        row = session.get(DailyHealth, today) or DailyHealth(day=today)
        row.training_status = status
        session.add(row)
        note_capability_observed(session, "training_status")
    elif decision == "probe_unknown":
        note_capability_probe(session, "training_status", "empty")


def _snapshot_user_profile(session) -> bool:
    try:
        prof = client.user_profile() or {}
        ud = prof.get("userData", {})
        if ud.get("gender"):
            _set_state(session, "user_gender", ud.get("gender"))
        if ud.get("weight"):
            _set_state(session, "user_weight", str(round(ud.get("weight") / 1000.0, 1)))
        if ud.get("birthDate"):
            _set_state(session, "user_birth_date", ud.get("birthDate"))
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Weekly user-profile refresh failed", exc_info=True)
        return False
    return True


def _slow_metrics_due(session: Session, today: date, full: bool, force: bool, allow_backfill: bool) -> bool:
    if force:
        return False
    if full:
        return True
    if not allow_backfill:
        return False
    previous = _parse_state_date(_get_state(session, _WEEKLY_SLOW_METRICS))
    return previous is None or (today - previous).days >= 7


def _run_weekly_slow_metrics(session: Session, today: date, *, context: str = "scheduled") -> None:
    """Bounded weekly current-value work: one Fitness Age and one profile read."""
    try:
        _sync_current_fitness_age(session, today, context=context)
        _sync_current_training_status(session, today, context=context)
        if not _snapshot_user_profile(session):
            return
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Weekly slow metric refresh failed", exc_info=True)
        return
    _set_state(session, _WEEKLY_SLOW_METRICS, today.isoformat())


def _snapshot_summary_metrics() -> None:
    """Retired compatibility shim: slow metrics are no longer historical scans."""
    return None
