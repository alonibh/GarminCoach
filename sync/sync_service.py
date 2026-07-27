"""Pull data from Garmin into SQLite with idempotent upserts.

Garmin's JSON shapes are loosely documented and occasionally vary, so every
parser is defensive: missing keys -> None rather than a crash. A failed day or
activity is logged and skipped; the sync continues.
"""
from __future__ import annotations

import logging
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
    ExerciseSet,
    MetricSnapshot,
    Sleep,
    SyncState,
    Workout,
    get_session,
)
from sync.garmin_client import client, normalize_training_readiness
from time_utils import get_local_tz

logger = logging.getLogger(__name__)

def _is_auth_error(exc: Exception) -> bool:
    if isinstance(exc, GarminConnectAuthenticationError):
        return True
    msg = str(exc).lower()
    err_type = type(exc).__name__.lower()
    return "401" in msg or "authentication" in msg or "unauthorized" in msg or "authentication" in err_type

# Activity type substrings that carry per-set strength detail.
_STRENGTH_HINTS = ("strength", "weight")

_RESOURCE_CURSOR_KEYS = {
    "activities": "last_activities_sync_through",
    "sleep": "last_sleep_sync_through",
    "daily_health": "last_daily_health_sync_through",
}

_STAGE1_COMPLETE = "stage1_bootstrap_complete"
_STAGE1_PREFIX = "stage1_bootstrap_"


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


def _parse_state_date(value: Optional[str]) -> Optional[date]:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


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

    # The activities_by_date endpoint doesn't actually include RPE/Feel for historical
    # workouts. We need to fetch the full activity details to reliably get them.
    # To avoid N+1 HTTP calls on every sync, we only fetch if we don't already have it.
    if enrich and (
        ((new_rpe is None or new_feel is None) and (act.rpe is None or act.feel is None))
        or not act.provenance_checked
    ):
        try:
            full_act = client.api.get_activity(act_id)
            if full_act:
                summary_dto = full_act.get("summaryDTO", {})
                new_rpe = summary_dto.get("directWorkoutRpe", new_rpe)
                new_feel = summary_dto.get("directWorkoutFeel", new_feel)
                new_workout_id = _workout_id(full_act) or new_workout_id
                act.provenance_checked = True
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as e:
            logger.warning("Failed to fetch full activity %s for RPE/provenance extraction: %s", act_id, e)

    # Only update if we found something new, otherwise preserve existing
    if new_rpe is not None:
        act.rpe = new_rpe
    if new_feel is not None:
        act.feel = new_feel
    if new_workout_id is not None:
        act.source_workout_id = new_workout_id
        
    if act.hr_zone_seconds is None:
        try:
            zones_raw = client.hr_zones(act_id)
            if zones_raw:
                import json
                secs = [0.0] * 5
                for z in zones_raw:
                    zn = z.get("zoneNumber")
                    if zn is not None and 1 <= zn <= 5:
                        secs[zn - 1] = float(z.get("secsInZone") or 0.0)
                # Only save if there's actual zone data (sum > 0)
                if sum(secs) > 0:
                    act.hr_zone_seconds = json.dumps(secs)
        except GarminConnectTooManyRequestsError:
            raise
        except Exception as e:
            logger.warning("Failed to fetch hr_zones for %s: %s", act_id, e)
            
    session.add(act)
    return act_id


def _sync_exercise_sets(session, activity_id: int) -> None:
    """Replace non-edited sets for an activity; preserve user-edited ones."""
    try:
        data = client.exercise_sets(activity_id)
    except GarminConnectTooManyRequestsError:
        raise  # let the circuit breaker handle rate limits
    except Exception:
        logger.warning("Exercise-sets fetch failed for activity %s", activity_id, exc_info=True)
        return  # a single bad activity shouldn't abort the whole sync
    sets = _g(data, "exerciseSets", default=[]) or []
    if not sets:
        return

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


def _sync_workouts(session: Session) -> None:
    """Fetch user's pre-defined workouts and their deep step structures.

    Reconciles local state with Garmin: upserts every workout Garmin returns
    and prunes local rows whose workout no longer exists in Garmin (so deleting
    a template in Garmin Connect removes it here too, instead of accumulating
    every workout the user has ever created).
    """
    try:
        workouts = client.api.get_workouts()
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
            full_w = client.api.get_workout_by_id(wid)
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
        
    count = 0
    strength_ids: list[tuple[datetime, int]] = []
    for raw in raw_list or []:
        raw_dur = raw.get("duration")
        if raw_dur is None or float(raw_dur) <= 0:
            continue
        act_id = _upsert_activity(session, raw, enrich=enrich)
        if act_id is None:
            continue
        if _is_strength(_g(raw, "activityType", "typeKey", default="") or ""):
            when = _parse_dt(raw.get("startTimeLocal") or raw.get("startTimeGMT")) or datetime.min
            strength_ids.append((when, act_id))
        if vo2_values is not None and raw.get("vO2MaxValue") is not None:
            try:
                vo2_values.append(((raw.get("startTimeLocal") or "")[:10], round(float(raw["vO2MaxValue"]), 1)))
            except (TypeError, ValueError):
                pass
        count += 1
    for _, act_id in sorted(strength_ids, reverse=True)[:strength_limit]:
        _sync_exercise_sets(session, act_id)
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


def _sync_daily_health(session, day: date, *, current_optional: bool = True) -> bool:
    row = session.get(DailyHealth, day) or DailyHealth(day=day)
    complete = True

    try:
        hrv = client.hrv(day)
        row.hrv_overnight = _g(hrv, "hrvSummary", "lastNightAvg")
        row.hrv_baseline_low = _g(hrv, "hrvSummary", "baseline", "balancedLow")
        row.hrv_baseline_high = _g(hrv, "hrvSummary", "baseline", "balancedUpper")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("HRV fetch failed for %s", day, exc_info=True)
        complete = False

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
        logger.warning("Resting HR fetch failed for %s", day, exc_info=True)
        complete = False

    try:
        stress = client.stress(day)
        row.stress_avg = stress.get("avgStressLevel")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Stress fetch failed for %s", day, exc_info=True)
        complete = False

    try:
        bb = client.body_battery(day, day)
        if bb:
            levels = [
                v[1]
                for v in (_g(bb[0], "bodyBatteryValuesArray", default=[]) or [])
                if isinstance(v, list) and len(v) > 1 and v[1] is not None
            ]
            if levels:
                row.body_battery_high = max(levels)
                row.body_battery_low = min(levels)
                row.body_battery_current = levels[-1]  # most recent reading
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Body battery fetch failed for %s", day, exc_info=True)
        complete = False

    try:
        steps = client.daily_steps(day, day)
        if steps:
            row.steps = steps[0].get("totalSteps")
            row.step_goal = steps[0].get("stepGoal")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Steps fetch failed for %s", day, exc_info=True)
        complete = False

    try:
        summary = client.user_summary(day)
        if summary:
            row.total_kcal = summary.get("totalKilocalories")
            row.active_kcal = summary.get("activeKilocalories")
            row.bmr_kcal = summary.get("bmrKilocalories")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception as exc:
        if _is_auth_error(exc):
            raise GarminConnectAuthenticationError("Garmin Connect authentication failed (401)") from exc
        logger.warning("Daily summary fetch failed for %s", day, exc_info=True)
        complete = False

    # Readiness and status are current-only facts.  Historical wellness sync
    # deliberately never calls either endpoint.
    if current_optional:
        _sync_current_optional_health(session, day, row)

    session.add(row)
    return complete


def _sync_current_optional_health(session: Session, day: date, row: DailyHealth | None = None) -> None:
    """Fetch current-only optional recovery facts, respecting capability."""
    from metrics.freshness import capability_state, note_capability_observed

    row = row or session.get(DailyHealth, day) or DailyHealth(day=day)
    readiness_capability = capability_state(session)
    if readiness_capability != "unsupported":
        payload = normalize_training_readiness(client.training_readiness(day), day)
        if payload is not None:
            row.training_readiness = payload["trainingReadiness"]
            note_capability_observed(session)

    if capability_state(session, "training_status") == "supported":
        status_data = client.training_status(day)
        if isinstance(status_data, dict):
            row.training_status = status_data.get("mostRecentTrainingStatus")
    session.add(row)


def _sync_stage1(session: Session, today: date, summary: dict) -> bool:
    """Fast, resumable first-account bootstrap.  Do not add Stage 2 here."""
    from metrics.freshness import capability_state, note_capability_from_device, note_capability_observed

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

        # 3. Today's Training Readiness, only when supported or unknown.
        if not done("training_readiness"):
            if capability_state(session) != "unsupported":
                payload = normalize_training_readiness(client.training_readiness(today), today)
                row = session.get(DailyHealth, today) or DailyHealth(day=today)
                if payload is not None:
                    row.training_readiness = payload["trainingReadiness"]
                    note_capability_observed(session)
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
            for day in (window_start + timedelta(days=n) for n in range(7)):
                if _sync_daily_health(session, day, current_optional=False) is False:
                    return False
                _advance_resource_cursor(session, "daily_health", day)
            mark("daily_health")
            summary["days"] = 7

        # 5. 30 days of summaries; no per-activity enrichment in Stage 1.
        vo2_values: list[tuple[str, float]] = []
        if not done("activities"):
            summary["activities"] = _sync_activities(
                session, today - timedelta(days=29), today, strength_limit=0,
                enrich=False, vo2_values=vo2_values,
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
                _sync_exercise_sets(session, activity.id)
            mark("strength_sets")

        # 7-8. Current-only slow values.  Never call the historical helpers.
        if not done("fitness_age"):
            fitness_age = client.fitness_age(today) or {}
            value = fitness_age.get("fitnessAge")
            if value is not None:
                _upsert_snapshot(session, "fitness_age", [((fitness_age.get("lastUpdated") or today.isoformat())[:10], round(float(value), 1))])
            mark("fitness_age")
        if not done("vo2max"):
            if vo2_values:
                _upsert_snapshot(session, "vo2max", [max(vo2_values)])
            mark("vo2max")

        # 9. Status has no inferred capability: unknown and unsupported are
        # resolved without a request; supported accounts fetch today's value.
        if not done("training_status"):
            if capability_state(session, "training_status") == "supported":
                status = client.training_status(today)
                if isinstance(status, dict):
                    row = session.get(DailyHealth, today) or DailyHealth(day=today)
                    row.training_status = status.get("mostRecentTrainingStatus")
                    session.add(row)
            mark("training_status")

        _set_state(session, _STAGE1_COMPLETE, "complete")
        return True
    except GarminConnectTooManyRequestsError as exc:
        until = _note_rate_limited(session)
        summary["errors"].append(f"Rate limited during Stage 1: {exc}")
        summary["errors"].append(f"Cooling down until {until.isoformat(timespec='seconds')}.")
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


def _priority_individual_health(session: Session, day: date, device_upload_at: datetime | None) -> None:
    """Fetch only unsupported-device observations used as independent facts."""
    from metrics.freshness import ERROR, FRESH, HRV, MISSING, RESTING_HR, STRESS, record_signal

    row = session.get(DailyHealth, day) or DailyHealth(day=day)
    fetches = (
        (HRV, "get_hrv_data", client.hrv, lambda data: _g(data, "hrvSummary", "lastNightAvg")),
        (RESTING_HR, "get_rhr_day", client.resting_hr, lambda data: (
            (_g(data, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE", default=[]) or [{}])[0].get("value")
        )),
        (STRESS, "get_all_day_stress", client.stress, lambda data: (data or {}).get("avgStressLevel")),
    )
    for signal, endpoint, fetcher, extract in fetches:
        try:
            value = extract(fetcher(day))
            if signal == HRV:
                row.hrv_overnight = value
            elif signal == RESTING_HR:
                row.resting_hr = value
            else:
                row.stress_avg = value
            record_signal(
                session, signal, day, FRESH if value is not None else MISSING, endpoint,
                device_upload_at=device_upload_at,
            )
        except Exception as exc:
            record_signal(
                session, signal, day, ERROR, endpoint,
                device_upload_at=device_upload_at, error_code=_freshness_error_code(exc),
            )
    session.add(row)


def _record_full_sync_freshness(
    session: Session,
    day: date,
    device_upload_iso: str | None,
) -> None:
    """Align today's full-sync facts with the per-signal freshness contract."""
    from metrics.freshness import (
        EXPECTED_PENDING,
        FRESH,
        HRV,
        MISSING,
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
    if capability == "unsupported":
        record_signal(
            session, TRAINING_READINESS, day, UNSUPPORTED, "device_capability",
            fetched_at=fetched_at, device_upload_at=device_upload_at,
        )
        individual = (
            (HRV, health.hrv_overnight if health else None, "get_hrv_data"),
            (RESTING_HR, health.resting_hr if health else None, "get_rhr_day"),
            (STRESS, health.stress_avg if health else None, "get_all_day_stress"),
        )
        for signal, value, endpoint in individual:
            record_signal(
                session, signal, day, FRESH if value is not None else MISSING, endpoint,
                fetched_at=fetched_at, device_upload_at=device_upload_at,
            )
    else:
        record_signal(
            session, TRAINING_READINESS, day,
            FRESH if health and health.training_readiness is not None else MISSING,
            "get_training_readiness", fetched_at=fetched_at, device_upload_at=device_upload_at,
        )


def run_priority_sync() -> dict:
    """Fetch and commit only facts needed by today's morning decision."""
    from metrics.freshness import (
        ERROR,
        EXPECTED_PENDING,
        FRESH,
        MISSING,
        SLEEP,
        SLEEP_SCORE,
        TRAINING_READINESS,
        UNSUPPORTED,
        capability_state,
        morning_freshness,
        note_capability_from_device,
        note_capability_observed,
        record_signal,
    )
    from time_utils import get_local_date, get_local_tz

    target = get_local_date()
    summary = {"priority": True, "day": target.isoformat(), "errors": []}
    fetched_at = datetime.now(timezone.utc).replace(tzinfo=None)
    with get_session() as session:
        device_upload_at = None
        try:
            device = client.device_last_used() or {}
            note_capability_from_device(session, device, observed_at=fetched_at)
            upload_iso = _device_upload_iso_from_payload(device)
            _set_state(session, "device_last_upload", upload_iso)
            parsed_upload = _parse_state_dt(upload_iso)
            device_upload_at = parsed_upload.replace(tzinfo=None) if parsed_upload else None
        except Exception as exc:
            code = _freshness_error_code(exc)
            summary["errors"].append(f"device:{code}")
            for signal, endpoint in ((SLEEP, "get_sleep_data"), (TRAINING_READINESS, "get_training_readiness")):
                record_signal(session, signal, target, ERROR, endpoint, fetched_at=fetched_at, error_code=code)
            _set_state(session, "last_priority_sync_at", fetched_at.isoformat())
            return summary

        sleep_ok = False
        try:
            sleep_ok = _sync_sleep(session, target)
        except Exception as exc:
            code = _freshness_error_code(exc)
            summary["errors"].append(f"sleep:{code}")
            record_signal(
                session, SLEEP, target, ERROR, "get_sleep_data",
                fetched_at=fetched_at, device_upload_at=device_upload_at, error_code=code,
            )
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

        capability = capability_state(session)
        if capability == "unsupported":
            record_signal(
                session, TRAINING_READINESS, target, UNSUPPORTED, "device_capability",
                fetched_at=fetched_at, device_upload_at=device_upload_at,
            )
            _priority_individual_health(session, target, device_upload_at)
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
                    session.add(health)
                    note_capability_observed(session, observed_at=fetched_at)
                    record_signal(
                        session, TRAINING_READINESS, target, FRESH, "get_training_readiness",
                        fetched_at=fetched_at, device_upload_at=device_upload_at,
                    )
                else:
                    record_signal(
                        session, TRAINING_READINESS, target, MISSING, "get_training_readiness",
                        fetched_at=fetched_at, device_upload_at=device_upload_at,
                    )
            except Exception as exc:
                code = _freshness_error_code(exc)
                summary["errors"].append(f"training_readiness:{code}")
                record_signal(
                    session, TRAINING_READINESS, target, ERROR, "get_training_readiness",
                    fetched_at=fetched_at, device_upload_at=device_upload_at, error_code=code,
                )

        _set_state(session, "last_priority_sync_at", fetched_at.isoformat())
        _set_state(session, "overnight_facts_updated_at", fetched_at.isoformat())
        summary.update(morning_freshness(session, target))
    return summary


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


def run_sync(full: bool = False, force: bool = False) -> dict:
    """Sync new data since last run (or backfill on first run / full=True).

    Returns a summary dict for display in the UI.
    """
    today = date.today()
    summary = {"activities": 0, "program_matches": 0, "days": 0, "errors": [], "skipped": False}
    preflight = None

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
                    return summary
            except GarminConnectTooManyRequestsError as e:
                until = _note_rate_limited(session)
                summary["skipped"] = True
                summary["errors"].append(
                    "Rate limited during Garmin preflight. Cooling down until "
                    f"{until.isoformat(timespec='seconds')}: {e}"
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

        try:
            from coach.program_state import reconcile_active_program
            summary["program_matches"] = reconcile_active_program(session)
        except Exception as e:
            summary["errors"].append(f"Program reconciliation: {e}")

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
            health_days = _sync_resource_days(
                session, "daily_health", health_start, today, _sync_daily_health, summary
            )
            if health_days is None:
                return summary
            summary["days"] = max(sleep_days, health_days)
        except GarminConnectAuthenticationError:
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

    # Snapshot summary metrics (fitness age, VO2 max) so the dashboard reads
    # them instantly without live Garmin calls. Safe to fail.
    try:
        _snapshot_summary_metrics()
    except GarminConnectTooManyRequestsError as e:
        with get_session() as session:
            until = _note_rate_limited(session)
        summary["errors"].append(
            f"Rate limited on metric snapshot; cooling down until {until.isoformat(timespec='seconds')}: {e}"
        )
    except Exception as e:
        summary["errors"].append(f"metric snapshot: {e}")

    # Recompute metrics after every sync (no-op until Phase 2 lands).
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
            metrics = session.query(DailyMetrics).filter_by(day=date.today()).first()
            if metrics:
                from notify.rules import check_and_notify_rules
                check_and_notify_rules(metrics)
            coach.generate_daily_suggestion(session)

    except Exception as e:
        summary["errors"].append(f"coach suggestion: {e}")

    return summary


def _last_different(history: list[tuple]) -> tuple:
    """history = (date, value) newest first. Returns (current_date, current,
    prev_date, prev_value) where prev is the most recent value that differs
    from current (or (None, None) if it never changed)."""
    if not history:
        return None, None, None, None
    cur_date, cur = history[0]
    for d, v in history[1:]:
        if v != cur:
            return cur_date, cur, d, v
    return cur_date, cur, None, None


def _fitness_age_history(weeks: int = 16) -> list[tuple]:
    """Weekly (lastUpdated, fitnessAge) snapshots, newest first, de-duped by
    day. get_fitnessage_data accepts any date and returns that day's value."""
    out: list[tuple] = []
    seen: set[str] = set()
    for i in range(0, weeks * 7, 7):
        d = (date.today() - timedelta(days=i)).isoformat()
        try:
            fa = client.api.get_fitnessage_data(d) or {}
        except Exception:
            continue
        val, upd = fa.get("fitnessAge"), (fa.get("lastUpdated") or "")[:10]
        if val is None or not upd or upd in seen:
            continue
        seen.add(upd)
        out.append((upd, round(float(val), 1)))
    out.sort(reverse=True)
    return out


def _vo2max_history(days: int = 365) -> list[tuple]:
    """(date, vo2max) from running activities carrying vO2MaxValue, newest
    first. Garmin attaches VO2 max to qualifying GPS runs, not daily endpoints."""
    try:
        acts = client.api.get_activities_by_date(
            (date.today() - timedelta(days=days)).isoformat(),
            date.today().isoformat(),
        )
    except Exception:
        return []
    out = [
        ((a.get("startTimeLocal") or "")[:10], round(float(a["vO2MaxValue"]), 1))
        for a in (acts or [])
        if a.get("vO2MaxValue")
    ]
    out.sort(reverse=True)
    return out


def _upsert_snapshot(session, metric: str, history: list[tuple]) -> None:
    cur_date, cur, prev_date, prev = _last_different(history)
    if cur is None:
        return  # nothing to store; leave any prior snapshot intact
    row = session.get(MetricSnapshot, metric) or MetricSnapshot(metric=metric)
    row.value, row.value_date = cur, cur_date
    row.prev_value, row.prev_date = prev, prev_date
    row.updated_at = datetime.now()
    session.add(row)

def _target_fitness_age() -> Optional[float]:
    try:
        fa = client.api.get_fitnessage_data(date.today().isoformat()) or {}
        val = fa.get("achievableFitnessAge")
        return round(float(val), 1) if val is not None else None
    except Exception:
        return None


def _snapshot_user_profile(session) -> None:
    try:
        prof = client.api.get_user_profile() or {}
        ud = prof.get("userData", {})
        if ud.get("gender"):
            _set_state(session, "user_gender", ud.get("gender"))
        if ud.get("weight"):
            _set_state(session, "user_weight", str(round(ud.get("weight") / 1000.0, 1)))
        if ud.get("birthDate"):
            _set_state(session, "user_birth_date", ud.get("birthDate"))
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass


def _snapshot_summary_metrics() -> None:
    """Compute + store fitness age and VO2 max snapshots (runs during sync)."""
    if not client.is_authenticated():
        return
    fa_hist = _fitness_age_history()
    vo2_hist = _vo2max_history()
    tfa = _target_fitness_age()
    with get_session() as session:
        _upsert_snapshot(session, "fitness_age", fa_hist)
        _upsert_snapshot(session, "vo2max", vo2_hist)
        _snapshot_user_profile(session)
        if tfa is not None:
            row = session.get(MetricSnapshot, "target_fitness_age") or MetricSnapshot(metric="target_fitness_age")
            row.value = tfa
            row.value_date = date.today().isoformat()
            row.updated_at = datetime.now()
            session.add(row)
