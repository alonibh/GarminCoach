"""Pull data from Garmin into SQLite with idempotent upserts.

Garmin's JSON shapes are loosely documented and occasionally vary, so every
parser is defensive: missing keys -> None rather than a crash. A failed day or
activity is logged and skipped; the sync continues.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import Any, Optional
from sqlalchemy.orm import Session

from coach import coach
from garminconnect import GarminConnectTooManyRequestsError

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
from sync.garmin_client import client
# Activity type substrings that carry per-set strength detail.
_STRENGTH_HINTS = ("strength", "weight")


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


def _parse_dt(s: Optional[str]) -> Optional[datetime]:
    if not s:
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


# --- activities + strength sets ------------------------------------------
def _is_strength(activity_type: str) -> bool:
    t = (activity_type or "").lower()
    return any(h in t for h in _STRENGTH_HINTS)


def _upsert_activity(session, raw: dict) -> Optional[int]:
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
    session.add(act)
    return act_id


def _sync_exercise_sets(session, activity_id: int) -> None:
    """Replace non-edited sets for an activity; preserve user-edited ones."""
    try:
        data = client.exercise_sets(activity_id)
    except GarminConnectTooManyRequestsError:
        raise  # let the circuit breaker handle rate limits
    except Exception:
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
    """Fetch user's pre-defined workouts and their deep step structures."""
    try:
        workouts = client.api.get_workouts()
    except Exception:
        return
        
    import json
    from datetime import datetime
    
    for w_summary in workouts:
        wid = w_summary.get("workoutId")
        if not wid:
            continue
            
        name = w_summary.get("workoutName", "Unnamed Workout")
        sport_type = _g(w_summary, "sportType", "sportTypeKey", default="unknown")
        
        # We only really care about strength, running, cycling, etc., but we can save all
        try:
            full_w = client.api.get_workout_by_id(wid)
            steps_json = json.dumps(full_w.get("workoutSegments", []))
        except Exception:
            steps_json = "[]"
            
        row = session.query(Workout).filter_by(workout_id=wid).first()
        if not row:
            row = Workout(workout_id=wid, created_at=datetime.now())
            
        row.name = name
        row.sport_type = sport_type
        row.steps_json = steps_json
        row.updated_at = datetime.now()
        
        session.add(row)
    session.commit()


def _sync_activities(session: Session, start: date, end: date) -> int:
    raw_list = client.activities_by_date(start, end)
    count = 0
    for raw in raw_list or []:
        act_id = _upsert_activity(session, raw)
        if act_id is None:
            continue
        if _is_strength(_g(raw, "activityType", "typeKey", default="") or ""):
            _sync_exercise_sets(session, act_id)
        count += 1
    return count


# --- daily health + sleep -------------------------------------------------
def _sync_sleep(session, day: date) -> None:
    try:
        data = client.sleep(day)
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        return
    dto = _g(data, "dailySleepDTO", default={}) or {}
    row = session.get(Sleep, day) or Sleep(day=day)
    row.total_s = dto.get("sleepTimeSeconds")
    row.deep_s = dto.get("deepSleepSeconds")
    row.light_s = dto.get("lightSleepSeconds")
    row.rem_s = dto.get("remSleepSeconds")
    row.awake_s = dto.get("awakeSleepSeconds")
    row.score = _g(dto, "sleepScores", "overall", "value")
    row.respiration_avg = dto.get("averageRespirationValue")
    row.sleep_stress_avg = dto.get("avgSleepStress")
    session.add(row)


def _sync_daily_health(session, day: date) -> None:
    row = session.get(DailyHealth, day) or DailyHealth(day=day)

    try:
        hrv = client.hrv(day)
        row.hrv_overnight = _g(hrv, "hrvSummary", "lastNightAvg")
        row.hrv_baseline_low = _g(hrv, "hrvSummary", "baseline", "balancedLow")
        row.hrv_baseline_high = _g(hrv, "hrvSummary", "baseline", "balancedUpper")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass

    try:
        rhr = client.resting_hr(day)
        vals = _g(rhr, "allMetrics", "metricsMap", "WELLNESS_RESTING_HEART_RATE", default=[])
        if vals:
            row.resting_hr = vals[0].get("value")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass

    try:
        stress = client.stress(day)
        row.stress_avg = stress.get("avgStressLevel")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass

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
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass

    try:
        steps = client.daily_steps(day, day)
        if steps:
            row.steps = steps[0].get("totalSteps")
            row.step_goal = steps[0].get("stepGoal")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass

    try:
        summary = client.user_summary(day)
        if summary:
            row.total_kcal = summary.get("totalKilocalories")
            row.active_kcal = summary.get("activeKilocalories")
            row.bmr_kcal = summary.get("bmrKilocalories")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass

    try:
        readiness_data = client.training_readiness(day)
        if isinstance(readiness_data, dict):
            # The exact key varies by device generation, but typically:
            row.training_readiness = readiness_data.get("trainingReadiness") or readiness_data.get("value")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass

    try:
        status_data = client.training_status(day)
        if isinstance(status_data, dict):
            row.training_status = status_data.get("mostRecentTrainingStatus")
    except GarminConnectTooManyRequestsError:
        raise
    except Exception:
        pass

    session.add(row)


# --- orchestration --------------------------------------------------------
def run_sync(full: bool = False) -> dict:
    """Sync new data since last run (or backfill on first run / full=True).

    Returns a summary dict for display in the UI.
    """
    today = date.today()
    summary = {"activities": 0, "days": 0, "errors": []}

    with get_session() as session:
        last = _get_state(session, "last_sync_through")
        if full or not last:
            start = today - timedelta(days=config.INITIAL_BACKFILL_DAYS)
        else:
            # Re-sync the last few days too (data settles after the day ends).
            start = datetime.strptime(last, "%Y-%m-%d").date() - timedelta(days=3)

        try:
            summary["activities"] = _sync_activities(session, start, today)
        except GarminConnectTooManyRequestsError as e:
            summary["errors"].append(f"Rate limited on activities: {e}")
        except Exception as e:
            summary["errors"].append(f"Activities: {e}")

        try:
            _sync_workouts(session)
        except Exception as e:
            summary["errors"].append(f"Workouts: {e}")

        # Circuit breaker: if Garmin is rate-limiting, don't grind through 90
        # days of doomed calls — abort fast with a clear message.
        consecutive_429 = 0
        aborted = False
        last_completed_day = None
        day = start
        while day <= today:
            try:
                _sync_sleep(session, day)
                _sync_daily_health(session, day)
                summary["days"] += 1
                consecutive_429 = 0
                last_completed_day = day
            except GarminConnectTooManyRequestsError as e:
                consecutive_429 += 1
                summary["errors"].append(f"Rate limited at {day}: {e}")
                if consecutive_429 >= 5:
                    summary["errors"].append(
                        "Aborted: Garmin is rate-limiting your IP (429). Wait "
                        "15–60 min, then click Sync now again. Already-synced "
                        "days are saved; sync resumes where it left off."
                    )
                    aborted = True
                    break
                time.sleep(2)
            except Exception as e:
                # A real error on this day: record it but DON'T advance the
                # high-water mark, so the day is retried on the next sync.
                summary["errors"].append(f"{day}: {e}")
            day += timedelta(days=1)

        # Only advance the high-water mark to what we actually synced, so an
        # aborted sync resumes from the right place next time.
        if aborted and last_completed_day:
            _set_state(session, "last_sync_through", last_completed_day.isoformat())
        elif not aborted:
            _set_state(session, "last_sync_through", today.isoformat())
        # Only stamp "last synced" if real data came through, so a sync that
        # failed immediately doesn't look successful in the UI.
        if summary["activities"] or summary["days"]:
            from datetime import timezone
            _set_state(
                session, "last_sync_at", datetime.now(timezone.utc).isoformat(timespec="seconds")
            )

        # Store the watch's last upload time so the dashboard can show both
        # the fetched time and the true device sync time.
        try:
            from datetime import timezone
            dev = client.device_last_used()
            upload_ms = dev.get("lastUsedDeviceUploadTime")
            if upload_ms:
                ts = datetime.fromtimestamp(upload_ms / 1000, tz=timezone.utc).isoformat(timespec="seconds")
                _set_state(session, "device_last_upload", ts)
            else:
                _set_state(session, "device_last_upload", "")
        except Exception:
            pass

    # Snapshot summary metrics (fitness age, VO2 max) so the dashboard reads
    # them instantly without live Garmin calls. Safe to fail.
    try:
        _snapshot_summary_metrics()
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
            coach.generate_daily_suggestion(session)
            coach.generate_nutrition_suggestion(session)
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
