"""GarminCoach FastAPI app — Phase 1 (dashboard + sync + workout detail)."""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from db import (
    Activity,
    DailyHealth,
    ExerciseSet,
    MetricSnapshot,
    Sleep,
    SyncState,
    get_session,
    init_db,
)
from sync.garmin_client import client
from sync.scheduler import start_scheduler

app = FastAPI(title="GarminCoach")
app.mount("/static", StaticFiles(directory=str(config.PROJECT_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(config.PROJECT_ROOT / "templates"))


def _asset_version() -> int:
    """Cache-buster: stylesheet mtime, so a CSS edit forces a fresh fetch."""
    try:
        return int(os.path.getmtime(config.PROJECT_ROOT / "static" / "style.css"))
    except OSError:
        return 0


templates.env.globals["asset_version"] = _asset_version


def _humanize(enum_name: str | None) -> str:
    """GOBLET_SQUAT -> 'Goblet Squat'. Garmin exercise enums to Title Case."""
    if not enum_name:
        return ""
    return enum_name.replace("_", " ").title()


templates.env.filters["humanize"] = _humanize

# Sync run-state lives in sync_runner (shared with the scheduler, atomic start).
from sync import sync_runner  # noqa: E402


@app.on_event("startup")
def _startup() -> None:
    init_db()
    # Try to resume a cached Garmin session silently; don't block startup.
    try:
        client.login()
    except Exception:
        pass
    start_scheduler()


# --- helpers --------------------------------------------------------------
def _last_sync_at() -> str | None:
    with get_session() as s:
        row = s.get(SyncState, "last_sync_at")
        return row.value if row else None


def _trend(current, previous, *, lower_is_better: bool) -> str:
    """Arrow comparing current vs previous: 'up' (improved), 'down'
    (worsened), or 'flat' (equal/unknown). For fitness age a LOWER number is
    better, so a drop is an improvement ('up')."""
    if current is None or previous is None or current == previous:
        return "flat"
    improved = (current < previous) if lower_is_better else (current > previous)
    return "up" if improved else "down"


def _age_label(value_date: str | None) -> str | None:
    """How long ago the displayed value was recorded: 'today', '1 day ago',
    'N days ago'. None if the date is missing/unparseable."""
    if not value_date:
        return None
    try:
        age = (date.today() - date.fromisoformat(value_date)).days
    except ValueError:
        return None
    if age <= 0:
        return "today"
    if age == 1:
        return "1 day ago"
    return f"{age} days ago"


def _tile(row, *, key, label, unit, lower_is_better):
    """Build a tile dict from a stored MetricSnapshot row (or None)."""
    if row is None or row.value is None:
        return {"key": key, "label": label, "value": None, "unit": unit,
                "prev": None, "age": None, "trend": "flat"}
    return {
        "key": key, "label": label, "value": row.value, "unit": unit,
        "prev": row.prev_value,
        "age": _age_label(row.value_date),
        "trend": _trend(row.value, row.prev_value, lower_is_better=lower_is_better),
    }


def _fitness_tiles() -> list[dict]:
    """Fitness Age + VO2 max tiles, read from the DB snapshot computed during
    sync — no live Garmin calls, so the dashboard never lags or blanks."""
    with get_session() as s:
        fa = s.get(MetricSnapshot, "fitness_age")
        vo2 = s.get(MetricSnapshot, "vo2max")
        return [
            _tile(fa, key="fitness_age", label="Fitness Age", unit="yrs", lower_is_better=True),
            _tile(vo2, key="vo2max", label="VO₂ max", unit="", lower_is_better=False),
        ]


# --- routes ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    needs_login = not client.is_authenticated()
    since = date.today() - timedelta(days=30)
    with get_session() as s:
        # All workouts in the past month (no row cap).
        activities = (
            s.query(Activity)
            .filter(Activity.start_time >= datetime.combine(since, datetime.min.time()))
            .order_by(Activity.start_time.desc())
            .all()
        )
        health = (
            s.query(DailyHealth)
            .filter(DailyHealth.day >= since)
            .order_by(DailyHealth.day.asc())
            .all()
        )
        sleep = (
            s.query(Sleep).filter(Sleep.day >= since).order_by(Sleep.day.asc()).all()
        )
        # Detach for template use
        activities = [
            {
                "id": a.id,
                "type": a.activity_type,
                "name": a.name,
                "start": a.start_time,
                "duration_min": round((a.duration_s or 0) / 60),
                "calories": a.calories,
                "avg_hr": a.avg_hr,
                "load": a.training_load,
            }
            for a in activities
        ]
        health_series = [
            {
                "day": h.day.isoformat(),
                "rhr": h.resting_hr,
                "hrv": h.hrv_overnight,
                "bb_low": h.body_battery_low,
                "steps": h.steps,
            }
            for h in health
        ]
        sleep_series = [
            {"day": sl.day.isoformat(), "hours": round((sl.total_s or 0) / 3600, 1), "score": sl.score}
            for sl in sleep
        ]

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "needs_login": needs_login,
            "activities": activities,
            "health_series": health_series,
            "sleep_series": sleep_series,
            "fitness_tiles": _fitness_tiles(),
            "last_sync_at": _last_sync_at(),
            "sync_running": sync_runner.is_running(),
            "sync_summary": sync_runner.status["summary"],
        },
    )


def _is_strength(activity_type: str) -> bool:
    return any(h in (activity_type or "").lower() for h in ("strength", "weight"))


def _te_label(raw: str | None) -> str | None:
    """Garmin training-effect message like 'OVERREACHING_17' → 'Overreaching'."""
    if not raw:
        return None
    words = [w for w in raw.split("_") if not w.isdigit()]
    return " ".join(words).title() or None


# Plain-language explanations for the non-obvious metrics (shown via ⓘ hover).
_METRIC_HINTS = {
    "Avg speed": "Average speed over the whole activity, including any time spent stationary.",
    "Max speed": "Fastest instantaneous speed recorded during the activity.",
    "Moving time": "Time spent actually moving — excludes pauses and standing still.",
    "Avg cadence": "Steps per minute — your running rhythm. A running metric; less meaningful for stop-start sports.",
    "Avg stride": "Average distance covered per step.",
    "Elevation": "Total metres climbed (+) and descended (−) during the activity.",
    "Intensity min": "Garmin Intensity Minutes: time in moderate vs vigorous effort zones. Vigorous counts double toward weekly goals.",
    "Training effect": "Garmin's read on what this session trained (e.g. VO₂ Max) and how hard it was on your body (e.g. Overreaching = above your usual load).",
}


def _is_steady_cardio(activity_type: str) -> bool:
    """Running/cycling-style activities where avg speed, cadence and stride are
    meaningful. For stop-start sports (soccer, tennis…) these average in
    standing time or are running-specific, so we hide them."""
    t = (activity_type or "").lower()
    return any(h in t for h in ("run", "cycl", "bik", "walk", "hike"))


def _cardio_stats(act: Activity) -> list[dict]:
    """Cardio stat rows for non-strength activities, using the watch's own
    values (only unit conversions, never invented metrics). Pace is omitted —
    Garmin doesn't report a pace field, and deriving it from average speed is
    misleading for stop-start sports. Avg speed/cadence/stride only show for
    steady cardio (running/cycling), where they're meaningful. Only rows with
    real data are returned."""
    steady = _is_steady_cardio(act.activity_type)
    rows: list[tuple[str, str]] = []
    if act.distance_m:
        rows.append(("Distance", f"{act.distance_m / 1000:.2f} km"))
    if steady and act.avg_speed_mps:
        rows.append(("Avg speed", f"{act.avg_speed_mps * 3.6:.1f} km/h"))
    if act.max_speed_mps:
        rows.append(("Max speed", f"{act.max_speed_mps * 3.6:.1f} km/h"))
    if act.moving_duration_s:
        rows.append(("Moving time", f"{round(act.moving_duration_s / 60)} min"))
    if steady and act.avg_cadence:
        rows.append(("Avg cadence", f"{round(act.avg_cadence)} spm"))
    if steady and act.avg_stride_cm:
        rows.append(("Avg stride", f"{act.avg_stride_cm / 100:.2f} m"))
    if act.elevation_gain_m or act.elevation_loss_m:
        rows.append(("Elevation", f"+{round(act.elevation_gain_m or 0)} / -{round(act.elevation_loss_m or 0)} m"))
    if act.steps:
        rows.append(("Steps", f"{act.steps:,}"))
    if act.lap_count:
        rows.append(("Laps", str(act.lap_count)))
    if act.moderate_intensity_min or act.vigorous_intensity_min:
        rows.append(("Intensity min", f"{act.moderate_intensity_min or 0} mod · {act.vigorous_intensity_min or 0} vig"))
    te = _te_label(act.aerobic_te_msg)
    if act.training_effect_label or te:
        label = (act.training_effect_label or "").replace("_", " ").title()
        rows.append(("Training effect", f"{label}{' · ' + te if te else ''}".strip(" ·")))
    return [{"label": k, "value": v, "hint": _METRIC_HINTS.get(k)} for k, v in rows]


def _hr_zones(activity_id: int) -> list[dict]:
    """Time-in-HR-zone bars for a workout. Live (cached) fetch; returns [] on
    any failure so the page still renders. Each row: zone, low BPM, minutes,
    and pct of the activity's in-zone time (for the bar width)."""
    if not client.is_authenticated():
        return []
    try:
        raw = client.hr_zones(activity_id) or []
    except Exception:
        return []
    total = sum((z.get("secsInZone") or 0) for z in raw)
    if total <= 0:
        return []
    out = []
    for z in raw:
        secs = z.get("secsInZone") or 0
        out.append({
            "zone": z.get("zoneNumber"),
            "low_bpm": round(z.get("zoneLowBoundary")) if z.get("zoneLowBoundary") else None,
            "minutes": round(secs / 60),
            "pct": round(secs / total * 100),
        })
    return out


@app.get("/workout/{activity_id}", response_class=HTMLResponse)
def workout_detail(request: Request, activity_id: int):
    with get_session() as s:
        act = s.get(Activity, activity_id)
        if act is None:
            return HTMLResponse("Not found", status_code=404)

        is_strength = _is_strength(act.activity_type)
        activity = {
            "id": act.id,
            "type": act.activity_type,
            "name": act.name,
            "start": act.start_time,
            "duration_min": round((act.duration_s or 0) / 60),
            "calories": act.calories,
            "avg_hr": act.avg_hr,
            "max_hr": act.max_hr,
            "is_strength": is_strength,
        }

        exercises: list[dict] = []
        cardio: list[dict] = []
        if is_strength:
            sets = (
                s.query(ExerciseSet)
                .filter(ExerciseSet.activity_id == activity_id)
                .order_by(ExerciseSet.set_index.asc())
                .all()
            )
            # Group consecutive working sets by exercise, keeping per-set
            # weight/reps. Rest rows are dropped from the grouped view.
            for st in sets:
                if (st.set_type or "").upper() == "REST" or not st.exercise_name:
                    continue
                if not exercises or exercises[-1]["name"] != st.exercise_name:
                    exercises.append({"name": st.exercise_name, "sets": []})
                exercises[-1]["sets"].append({
                    "id": st.id, "index": st.set_index,
                    "reps": st.reps, "weight_kg": st.weight_kg, "edited": st.edited,
                })
            for ex in exercises:
                vol = sum((x["reps"] or 0) * (x["weight_kg"] or 0) for x in ex["sets"])
                ex["set_count"] = len(ex["sets"])
                ex["total_reps"] = sum((x["reps"] or 0) for x in ex["sets"])
                ex["volume_kg"] = round(vol)
        else:
            cardio = _cardio_stats(act)

    return templates.TemplateResponse(
        request,
        "workout.html",
        {
            "activity": activity,
            "exercises": exercises,
            "cardio": cardio,
            "hr_zones": _hr_zones(activity_id),
        },
    )


@app.post("/set/{set_id}")
def edit_set(
    set_id: int,
    exercise_name: str = Form(""),
    reps: str = Form(""),
    weight_kg: str = Form(""),
):
    """Optional manual correction of a misdetected set (rarely needed)."""
    with get_session() as s:
        st = s.get(ExerciseSet, set_id)
        if st is None:
            return HTMLResponse("Set not found", status_code=404)
        if exercise_name:
            st.exercise_name = exercise_name
        st.reps = int(reps) if reps.strip() else st.reps
        st.weight_kg = float(weight_kg) if weight_kg.strip() else st.weight_kg
        st.edited = True
        aid = st.activity_id
    return RedirectResponse(f"/workout/{aid}", status_code=303)


@app.post("/sync")
def sync_now(full: bool = Form(False)):
    # Can't sync without an authenticated Garmin session — send to login.
    if not client.is_authenticated():
        return RedirectResponse("/login", status_code=303)
    sync_runner.try_start_sync(full)
    return RedirectResponse("/", status_code=303)


@app.get("/sync/status")
def sync_status():
    """JSON endpoint polled by the dashboard while a sync is in progress."""
    return JSONResponse({
        "running": sync_runner.is_running(),
        "summary": sync_runner.status["summary"],
        "last_sync_at": _last_sync_at(),
    })


@app.post("/sync/reset")
def sync_reset():
    """Escape hatch: force-clear a stuck 'syncing' state."""
    sync_runner.reset()
    return RedirectResponse("/", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(
        request, "login.html", {"email": config.GARMIN_EMAIL, "error": None}
    )


@app.post("/login", response_class=HTMLResponse)
def login_submit(request: Request, password: str = Form(...), mfa: str = Form("")):
    """First-time login. Password/MFA entered here, never stored.

    MFA: if your account requires it, paste the code into the MFA field. The
    library calls our prompt callback which returns that value.
    """
    try:
        mfa_value = mfa.strip()
        client.login(password=password, mfa_prompt=lambda: mfa_value)
    except Exception as e:
        msg = str(e)
        if "429" in msg or "rate limit" in msg.lower():
            msg = (
                "Garmin is rate-limiting your IP (HTTP 429) — too many login "
                "attempts. Wait 15–60 minutes, then try again. This is a Garmin "
                "throttle, not a wrong password."
            )
        return templates.TemplateResponse(
            request, "login.html", {"email": config.GARMIN_EMAIL, "error": msg}
        )

    # Only reached if login genuinely authenticated. Kick off initial backfill.
    sync_runner.try_start_sync(full=True)
    return RedirectResponse("/", status_code=303)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.HOST, port=config.PORT, reload=False)
