"""GarminCoach FastAPI app — dashboard + sync + workout detail."""
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
    DailyMetrics,
    ExerciseSet,
    MetricSnapshot,
    Sleep,
    SyncState,
    Goal,
    CoachMessage,
    get_session,
    init_db,
)
from metrics.engine import acwr_label
from sync.garmin_client import client
from sync.scheduler import start_scheduler
from coach.coach import handle_chat

app = FastAPI(title="GarminCoach")
app.mount("/static", StaticFiles(directory=str(config.PROJECT_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(config.PROJECT_ROOT / "templates"))


import hashlib
import hmac
import json
import secrets
import time as _time
from fastapi.responses import Response
from starlette.middleware.base import BaseHTTPMiddleware

# --- Cookie-based session auth (replaces Basic Auth) ----------------------
# The session cookie is HMAC-signed (SHA-256) so it can't be forged, and
# carries an expiry timestamp so it auto-expires after SESSION_MAX_AGE_DAYS.
# The browser keeps it across restarts (max_age is set on the cookie).

_COOKIE_NAME = "gc_session"
_MAX_AGE_S = config.SESSION_MAX_AGE_DAYS * 86400  # days → seconds

# Paths that don't require auth.
_PUBLIC_PREFIXES = ("/static", "/app-login", "/favicon")


def _sign_session(username: str) -> str:
    """Create a signed session token: base64(json payload) + '.' + hex(hmac)."""
    payload = json.dumps({"u": username, "t": int(_time.time())})
    sig = hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    import base64
    b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    return f"{b64}.{sig}"


def _verify_session(token: str) -> str | None:
    """Verify a session token. Returns the username if valid, None otherwise."""
    try:
        import base64
        b64, sig = token.rsplit(".", 1)
        payload = base64.urlsafe_b64decode(b64).decode()
        expected = hmac.new(config.SESSION_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        data = json.loads(payload)
        # Check expiry.
        if int(_time.time()) - data.get("t", 0) > _MAX_AGE_S:
            return None
        return data.get("u")
    except Exception:
        return None


class CookieAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        # If no APP_USERNAME is set, auth is disabled — let everything through.
        if not (config.APP_USERNAME or "").strip():
            return await call_next(request)

        # Skip auth for public paths.
        path = request.url.path
        if any(path.startswith(p) for p in _PUBLIC_PREFIXES):
            return await call_next(request)

        # Check session cookie.
        token = request.cookies.get(_COOKIE_NAME)
        if token and _verify_session(token):
            return await call_next(request)

        # Not authenticated → redirect to login page.
        return RedirectResponse(f"/app-login?next={path}", status_code=303)

app.add_middleware(CookieAuthMiddleware)


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
def _has_tz(iso_str: str) -> bool:
    """True if an ISO timestamp already carries a timezone (trailing 'Z' or a
    ±HH:MM offset). Parsing is the reliable test — string-sniffing the offset
    false-matches on hyphens inside the date portion."""
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).tzinfo is not None
    except ValueError:
        return False


def _ensure_utc_iso(val: str | None) -> str | None:
    """Normalize a stored timestamp so the client always gets a tz-aware string.
    Legacy rows were written as naive UTC; tag those with 'Z'."""
    if not val:
        return val
    return val if _has_tz(val) else val + "Z"


def _time_ago(iso_str: str | None) -> str | None:
    """Convert an ISO datetime string to a human-readable 'X ago' label."""
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(_ensure_utc_iso(iso_str).replace("Z", "+00:00"))
        # dt is now tz-aware; compare against an aware 'now' in the same tz.
        now = datetime.now(dt.tzinfo)

        time_str = dt.strftime("%H:%M")
        if dt.date() == now.date():
            return f"Today at {time_str}"
        elif dt.date() == (now.date() - timedelta(days=1)):
            return f"Yesterday at {time_str}"
        else:
            return dt.strftime("%b %d at %H:%M")
    except Exception:
        return iso_str


def _last_sync_at() -> str | None:
    with get_session() as s:
        row = s.get(SyncState, "last_sync_at")
        return _ensure_utc_iso(row.value if row else None)


def _device_last_upload() -> str | None:
    with get_session() as s:
        row = s.get(SyncState, "device_last_upload")
        return _ensure_utc_iso(row.value if row else None)


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


# VO₂max fitness-category floors (ml/kg/min), as (Fair, Good, Excellent,
# Superior) lower bounds — i.e. the 40th / 60th / 80th / 95th percentiles.
# Source: The Cooper Institute normative tables, reprinted verbatim in the
# Garmin Forerunner 935 owner's manual ("Data reprinted with permission from
# The Cooper Institute"); also ACSM Guidelines 11th ed. Table 4.7.
# Keyed by sex (True=male) then (age_low, age_high) inclusive decade band.
COOPER_VO2_NORMS: dict[bool, dict[tuple[int, int], tuple[float, float, float, float]]] = {
    True: {  # male
        (20, 29): (41.7, 45.4, 51.1, 55.4),
        (30, 39): (40.5, 44.0, 48.3, 54.0),
        (40, 49): (38.5, 42.4, 46.4, 52.5),
        (50, 59): (35.6, 39.2, 43.4, 48.9),
        (60, 69): (32.3, 35.5, 39.5, 45.7),
        (70, 79): (29.4, 32.3, 36.7, 42.1),
    },
    False: {  # female
        (20, 29): (36.1, 39.5, 43.9, 49.6),
        (30, 39): (34.4, 37.8, 42.4, 47.4),
        (40, 49): (33.0, 36.3, 39.7, 45.3),
        (50, 59): (30.1, 33.0, 36.7, 41.1),
        (60, 69): (27.5, 30.0, 33.0, 37.8),
        (70, 79): (25.9, 28.1, 30.9, 36.7),
    },
}


def _cooper_norms(age: int, is_male: bool) -> tuple[float, float, float, float]:
    """Pick the Cooper Institute boundary tuple for an age/sex. Ages below 20
    use the 20–29 band; above 79 use 70–79."""
    bands = COOPER_VO2_NORMS[is_male]
    a = max(20, min(79, age))
    for (lo, hi), b in bands.items():
        if lo <= a <= hi:
            return b
    return bands[(20, 29)]


def _vo2_max_details(
    val: float | None, age: int | None = None, is_male: bool | None = None
) -> tuple[float | None, str]:
    """Gauge percentage and category label for VO₂max against the Cooper
    Institute norms. Without a known age/sex we cannot pick the right band, so
    we return no label (the raw value is shown alone) rather than fabricating a
    bucket for a default 28-year-old male."""
    if val is None:
        return None, ""
    if age is None or is_male is None:
        return None, ""

    b = _cooper_norms(age, is_male)

    b1, b2, b3, b4 = b
    
    # We want each zone to be 20% of the gauge width visually.
    min_val = b1 - 5.0
    max_val = b4 + 5.0
    
    if val < b1:
        label = "Poor"
        pct = (val - min_val) / (b1 - min_val) * 20
    elif val < b2:
        label = "Fair"
        pct = 20 + (val - b1) / (b2 - b1) * 20
    elif val < b3:
        label = "Good"
        pct = 40 + (val - b2) / (b3 - b2) * 20
    elif val < b4:
        label = "Excellent"
        pct = 60 + (val - b3) / (b4 - b3) * 20
    else:
        label = "Superior"
        pct = 80 + (val - b4) / (max_val - b4) * 20
        
    return min(100.0, max(0.0, pct)), label


def _fitness_tiles() -> list[dict]:
    """Fitness Age + VO2 max tiles, read from the DB snapshot computed during
    sync — no live Garmin calls, so the dashboard never lags or blanks."""
    with get_session() as s:
        fa = s.get(MetricSnapshot, "fitness_age")
        vo2 = s.get(MetricSnapshot, "vo2max")
        tfa = s.get(MetricSnapshot, "target_fitness_age")
        
        # Dynamic profile config
        gender_st = s.get(SyncState, "user_gender")
        weight_st = s.get(SyncState, "user_weight")
        bd_st = s.get(SyncState, "user_birth_date")
        
        # Profile fields may be absent; keep them None rather than guessing, so
        # the VO₂max category isn't computed against a fabricated default.
        is_male = (gender_st.value.upper() == "MALE") if gender_st and gender_st.value else None
        weight_str = weight_st.value if weight_st and weight_st.value else ""
        gender_str = ("Male" if is_male else "Female") if is_male is not None else ""

        age = None
        if bd_st and bd_st.value:
            try:
                bd = date.fromisoformat(bd_st.value[:10])
                today = date.today()
                age = today.year - bd.year - ((today.month, today.day) < (bd.month, bd.day))
            except Exception:
                pass
        
        fa_tile = _tile(fa, key="fitness_age", label="Fitness Age", unit="yrs", lower_is_better=True)
        fa_tile["prev"] = None  # Hide 'from X' text but keep trend arrow
        if tfa and tfa.value:
            fa_tile["age"] = f"Target: {tfa.value}"
        if fa and fa.value_date:
            try:
                lbl = _age_label(fa.value_date[:10])
                if lbl:
                    fa_tile["updated_str"] = f"Updated {lbl}"
            except Exception:
                pass
        fa_tile["hint"] = "Garmin's estimate of how old your body performs. Lower is better — a 30-year-old with a fitness age of 22 has above-average cardiovascular fitness."
        
        vo2_val = vo2.value if vo2 else None
        vo2_pct, vo2_label = _vo2_max_details(vo2_val, age=age, is_male=is_male)
        
        vo2_updated = ""
        if vo2 and vo2.value_date:
            try:
                lbl = _age_label(vo2.value_date[:10])
                if lbl:
                    vo2_updated = f"Updated {lbl}"
            except Exception:
                pass
        
        desc = []
        if gender_str:
            desc.append(gender_str)
        if age is not None:
            desc.append(f"{age} yrs")
        if weight_str:
            desc.append(f"{weight_str} kg")
            
        vo2_tile = {
            "key": "vo2max", "label": "VO₂ max", "value": vo2_val, "unit": "ml/kg/min",
            "is_gauge": True,
            "bar_pct": vo2_pct,
            "age": vo2_label,
            "desc": " | ".join(desc),
            "updated_str": vo2_updated,
            "hint": "Maximum oxygen uptake. Higher = better aerobic capacity. Measured from qualifying GPS runs with heart rate."
        }
        
        return [fa_tile, vo2_tile]


def _readiness_tiles() -> list[dict]:
    """Readiness + ACWR tiles from the latest DailyMetrics row."""
    with get_session() as s:
        today = date.today()
        # Latest row (today or most recent day with data).
        latest = (
            s.query(DailyMetrics)
            .filter(DailyMetrics.day <= today)
            .order_by(DailyMetrics.day.desc())
            .first()
        )
        # Previous day for trend arrows.
        prev = None
        if latest:
            prev = (
                s.query(DailyMetrics)
                .filter(DailyMetrics.day < latest.day)
                .order_by(DailyMetrics.day.desc())
                .first()
            )

        # Readiness tile.
        r_val = latest.readiness if latest else None
        
        r_desc = ""
        if r_val is not None:
            if r_val >= 70:
                r_desc = "Ready to push."
            elif r_val >= 40:
                r_desc = "Moderate recovery."
            else:
                r_desc = "Prioritize recovery."

        readiness_tile = {
            "key": "readiness", "label": "Readiness",
            "value": int(r_val) if r_val is not None else None,
            "unit": "",
            "prev": None,
            "age": _age_label(latest.day.isoformat()) if latest else None,
            "trend": None,
            "desc": r_desc,
            "color": ("green" if r_val and r_val >= 70
                      else "yellow" if r_val and r_val >= 40
                      else "red" if r_val is not None
                      else None),
            "bar_pct": int(r_val) if r_val is not None else None,
            "hint": "Daily recovery score (0–100) based on your overnight HRV, resting heart rate, sleep duration, and Body Battery — all compared to your own 60-day personal baselines, not population averages. Green (≥70) = ready to push, yellow (40–69) = moderate, red (<40) = prioritize recovery.",
        }

        # ACWR tile.
        a_val = latest.acwr if latest else None
        # Color zones: green (balanced), yellow (ramping/detraining), red (spike).
        a_color = None
        a_desc = ""
        if a_val is not None:
            if a_val < 0.8:
                a_color = "yellow"
                a_desc = "Doing less than usual."
            elif a_val <= 1.3:
                a_color = "green"
                a_desc = "Steady progression, low injury risk."
            elif a_val <= 1.5:
                a_color = "yellow"
                a_desc = "Building up load."
            else:
                a_color = "red"
                a_desc = "Sharp increase, higher injury risk."
        # Bar position: map ACWR 0–2.0 to 0–100%, capped.
        a_bar_pct = min(100, int(a_val / 2.0 * 100)) if a_val is not None else None
        acwr_tile = {
            "key": "acwr", "label": "ACWR",
            "value": a_val,
            "unit": "",
            "is_gauge": True,
            "age": acwr_label(a_val),
            "desc": a_desc,
            "color": a_color,
            "bar_pct": a_bar_pct,
            "hint": "Acute:Chronic Workload Ratio — your last 7 days of training load divided by your last 28 days. Balanced (0.8–1.3) = steady progression. Ramping (1.3–1.5) = building up. Spike (>1.5) = sharp increase, higher injury risk. Detraining (<0.8) = doing less than usual.",
        }

        return [readiness_tile, acwr_tile]


# --- routes ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    needs_login = not client.is_authenticated()
    since = date.today() - timedelta(days=90)
    with get_session() as s:
        goal_row = s.get(Goal, 1)
        active_goal = goal_row.goal if goal_row and goal_row.goal else None
        
        today = date.today()
        suggestion = s.query(CoachMessage).filter_by(role="suggestion").order_by(CoachMessage.created_at.desc()).first()
        coach_suggestion = suggestion if suggestion and suggestion.created_at and suggestion.created_at.date() == today else None
        
        nutr = s.query(CoachMessage).filter_by(role="nutrition").order_by(CoachMessage.created_at.desc()).first()
        nutrition_suggestion = nutr if nutr and nutr.created_at and nutr.created_at.date() == today else None

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
                "hrv_baseline_low": h.hrv_baseline_low,
                "hrv_baseline_high": h.hrv_baseline_high,
                "bb_low": h.body_battery_low,
                "steps": h.steps,
                "step_goal": h.step_goal,
                "total_kcal": h.total_kcal,
                "active_kcal": h.active_kcal,
                "bmr_kcal": h.bmr_kcal,
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
            "readiness_tiles": _readiness_tiles(),
            "last_sync_at": _last_sync_at(),
            "device_last_upload": _device_last_upload(),
            "sync_running": sync_runner.is_running(),
            "sync_summary": sync_runner.status["summary"],
            "active_goal": active_goal,
            "coach_suggestion": coach_suggestion,
            "nutrition_suggestion": nutrition_suggestion,
        },
    )





def _is_strength(activity_type: str) -> bool:
    return any(h in (activity_type or "").lower() for h in ("strength", "weight"))


# Number of prior sessions to form the e1RM progression baseline (HEURISTIC —
# no source fixes the window; a small rolling best is less noisy than 1 session).
_E1RM_BASELINE_SESSIONS = 5
# e1RM equations are only validated up to ~12 reps; above that, ignore the set.
_E1RM_MAX_REPS = 12


def _epley_1rm(weight_kg: float | None, reps: int | None) -> float | None:
    """Estimated 1-rep-max via Epley (1985): e1RM = w·(1 + reps/30); = w at
    reps≤1 (the set is itself a 1RM test). Returns None for bodyweight (w=0) or
    rep counts above the validated range."""
    if not weight_kg or weight_kg <= 0 or reps is None or reps < 1:
        return None
    if reps > _E1RM_MAX_REPS:
        return None
    if reps == 1:
        return weight_kg
    return weight_kg * (1 + reps / 30.0)


def _session_e1rm(sets) -> float | None:
    """Best (max) e1RM across the working sets of one exercise in a session.
    *sets* items expose ``reps``/``weight_kg`` either as attrs or dict keys."""
    best = None
    for st in sets:
        reps = st["reps"] if isinstance(st, dict) else st.reps
        wkg = st["weight_kg"] if isinstance(st, dict) else st.weight_kg
        e = _epley_1rm(wkg, reps)
        if e is not None and (best is None or e > best):
            best = e
    return best


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
                # Volume load (tonnage) = Σ(reps × weight) — the standard
                # strength-science "volume load" (Schoenfeld et al. 2021).
                vol = sum((x["reps"] or 0) * (x["weight_kg"] or 0) for x in ex["sets"])
                ex["set_count"] = len(ex["sets"])
                ex["total_reps"] = sum((x["reps"] or 0) for x in ex["sets"])
                ex["volume_kg"] = round(vol)
                # Estimated 1RM (Epley) — normalizes progress across rep schemes.
                cur_e1rm = _session_e1rm(ex["sets"])
                ex["e1rm_kg"] = round(cur_e1rm, 1) if cur_e1rm else None

            # Strength progression: compare each exercise's estimated 1RM against
            # the best e1RM over the last few sessions (a rolling baseline is far
            # less noisy than a single-prior-session comparison, and is robust to
            # rep-scheme changes that confound raw weight/volume deltas).
            if act.start_time:
                for ex in exercises:
                    prev_sets = (
                        s.query(ExerciseSet)
                        .join(Activity)
                        .filter(
                            ExerciseSet.exercise_name == ex["name"],
                            Activity.start_time < act.start_time,
                            ExerciseSet.set_type != "REST",
                        )
                        .order_by(Activity.start_time.desc())
                        .all()
                    )
                    if not prev_sets:
                        ex["delta_vol"] = None
                        ex["delta_best"] = None
                        continue
                    # Group prior sets by activity (newest first), then take the
                    # best e1RM from each of the last N sessions as the baseline.
                    by_session: dict[int, list] = {}
                    order: list[int] = []
                    for ps in prev_sets:
                        if ps.activity_id not in by_session:
                            by_session[ps.activity_id] = []
                            order.append(ps.activity_id)
                        by_session[ps.activity_id].append(ps)

                    prev_e1rms = []
                    for aid in order[:_E1RM_BASELINE_SESSIONS]:
                        e = _session_e1rm(by_session[aid])
                        if e is not None:
                            prev_e1rms.append(e)

                    cur_e1rm = ex.get("e1rm_kg")
                    base_e1rm = max(prev_e1rms) if prev_e1rms else None
                    delta_b = (
                        round(cur_e1rm - base_e1rm, 1)
                        if (cur_e1rm and base_e1rm)
                        else None
                    )
                    ex["delta_best"] = delta_b if delta_b else None

                    # Volume delta vs the most recent prior session.
                    prev_act_id = order[0]
                    prev_for_ex = by_session[prev_act_id]
                    prev_vol = sum(
                        (ps.reps or 0) * (ps.weight_kg or 0) for ps in prev_for_ex
                    )
                    cur_vol = ex["volume_kg"]
                    if prev_vol > 0 and cur_vol > 0:
                        ex["delta_vol"] = round(cur_vol - prev_vol)
                    else:
                        ex["delta_vol"] = None
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
        "device_last_upload": _device_last_upload(),
    })


@app.post("/sync/reset")
def sync_reset():
    """Escape hatch: force-clear a stuck 'syncing' state."""
    sync_runner.reset()
    return RedirectResponse("/", status_code=303)


# --- App login (cookie session) -------------------------------------------
_APP_LOGIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Login — GarminCoach</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #0d1117; color: #e6edf3;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh;
    }}
    .login-card {{
      background: #161b22; border: 1px solid #30363d; border-radius: 12px;
      padding: 2.5rem; width: 100%; max-width: 380px;
      box-shadow: 0 8px 32px rgba(0,0,0,.4);
    }}
    .login-card h1 {{ font-size: 1.4rem; text-align: center; margin-bottom: .3rem; }}
    .login-card .sub {{ text-align: center; color: #8b949e; font-size: .85rem; margin-bottom: 1.5rem; }}
    label {{ display: block; font-size: .85rem; color: #8b949e; margin-bottom: .3rem; margin-top: 1rem; }}
    input[type=text], input[type=password] {{
      width: 100%; padding: .65rem .8rem; border-radius: 6px;
      border: 1px solid #30363d; background: #0d1117; color: #e6edf3;
      font-size: .95rem; outline: none; transition: border-color .2s;
    }}
    input:focus {{ border-color: #58a6ff; }}
    button {{
      width: 100%; margin-top: 1.5rem; padding: .7rem;
      border: none; border-radius: 6px; cursor: pointer;
      font-size: .95rem; font-weight: 600;
      background: #238636; color: #fff; transition: background .2s;
    }}
    button:hover {{ background: #2ea043; }}
    .error {{
      background: #3d1f1f; border: 1px solid #6e3630; border-radius: 6px;
      padding: .6rem .8rem; margin-bottom: 1rem; font-size: .85rem; color: #f85149;
    }}
  </style>
</head>
<body>
  <div class="login-card">
    <h1>🏃 GarminCoach</h1>
    <p class="sub">Sign in to continue</p>
    {error_html}
    <form method="post">
      <input type="hidden" name="next" value="{next_url}">
      <label for="username">Username</label>
      <input type="text" id="username" name="username" autocomplete="username" required autofocus>
      <label for="password">Password</label>
      <input type="password" id="password" name="password" autocomplete="current-password" required>
      <button type="submit">Sign in</button>
    </form>
  </div>
</body>
</html>"""


@app.get("/app-login", response_class=HTMLResponse)
def app_login_form(request: Request, next: str = "/"):
    html = _APP_LOGIN_HTML.format(error_html="", next_url=next)
    return HTMLResponse(html)


@app.post("/app-login", response_class=HTMLResponse)
def app_login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    env_user = (config.APP_USERNAME or "").strip()
    env_pass = (config.APP_PASSWORD or "").strip()
    if (
        env_user
        and secrets.compare_digest(username.strip(), env_user)
        and secrets.compare_digest(password, env_pass)
    ):
        # Success → set signed session cookie and redirect.
        token = _sign_session(username.strip())
        response = RedirectResponse(next or "/", status_code=303)
        response.set_cookie(
            _COOKIE_NAME,
            token,
            max_age=_MAX_AGE_S,
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    # Failed login.
    error_html = '<div class="error">Invalid username or password.</div>'
    html = _APP_LOGIN_HTML.format(error_html=error_html, next_url=next)
    return HTMLResponse(html, status_code=401)


@app.get("/app-logout")
def app_logout():
    response = RedirectResponse("/app-login", status_code=303)
    response.delete_cookie(_COOKIE_NAME, path="/")
    return response


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


@app.get("/goal", response_class=HTMLResponse)
def get_goal_page(request: Request):
    """View and edit the user goal."""
    with get_session() as session:
        goal_row = session.get(Goal, 1) or Goal(id=1, goal="", custom_input="")
        return templates.TemplateResponse(request, "goal.html", {"goal": goal_row})

@app.post("/goal", response_class=RedirectResponse)
def post_goal_page(request: Request, goal: str = Form(""), custom_input: str = Form("")):
    """Save the user goal."""
    with get_session() as session:
        goal_row = session.get(Goal, 1)
        if not goal_row:
            goal_row = Goal(id=1)
            session.add(goal_row)
        goal_row.goal = goal
        goal_row.custom_input = custom_input
        goal_row.updated_at = datetime.now()
        session.commit()
    return RedirectResponse(url="/", status_code=303)


@app.get("/chat", response_class=HTMLResponse)
def get_chat_page(request: Request):
    """AI Coach chat interface."""
    with get_session() as session:
        msgs = session.query(CoachMessage).filter(
            CoachMessage.role.in_(["user", "assistant"])
        ).order_by(CoachMessage.created_at.asc()).all()
        response = templates.TemplateResponse(request, "chat.html", {"messages": msgs})
        response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

@app.post("/chat", response_class=RedirectResponse)
def post_chat_page(request: Request, message: str = Form(...)):
    """Handle a new chat message."""
    with get_session() as session:
        handle_chat(session, message)
    return RedirectResponse(url="/chat", status_code=303)


@app.post("/chat/{msg_id}/approve", response_class=RedirectResponse)
def approve_action(request: Request, msg_id: int):
    """Approve a staged action."""
    with get_session() as session:
        msg = session.get(CoachMessage, msg_id)
        if msg and msg.pending_action_json:
            import json
            from coach.garmin_compiler import compile_and_schedule
            payload = json.loads(msg.pending_action_json)
            
            if payload.get("action") == "schedule_workout":
                success = compile_and_schedule(session, payload)
                msg.pending_action_json = None  # Clear pending status
                if success:
                    msg.content += "\n\n✅ *Workout successfully approved, uploaded, and scheduled on your Garmin Calendar!*"
                else:
                    msg.content += "\n\n❌ *I tried to schedule this workout, but an error occurred while talking to Garmin.*"
                session.commit()
    return RedirectResponse("/chat", status_code=303)

@app.post("/chat/{msg_id}/reject", response_class=RedirectResponse)
def reject_action(request: Request, msg_id: int):
    """Reject a staged action."""
    with get_session() as session:
        msg = session.get(CoachMessage, msg_id)
        if msg and msg.pending_action_json:
            msg.pending_action_json = None
            msg.content += "\n\n❌ *Workout cancelled by user.*"
            session.commit()
    return RedirectResponse("/chat", status_code=303)


@app.get("/calendar", response_class=HTMLResponse)
def get_calendar_page(request: Request, year: int = None, month: int = None):
    """Monthly calendar view with workouts and readiness."""
    import calendar
    
    today = date.today()
    y = year or today.year
    m = month or today.month
    
    # Calculate prev/next month links
    prev_y, prev_m = (y, m - 1) if m > 1 else (y - 1, 12)
    next_y, next_m = (y, m + 1) if m < 12 else (y + 1, 1)
    
    cal = calendar.Calendar(firstweekday=0) # Monday first
    month_days = cal.monthdatescalendar(y, m)
    
    with get_session() as session:
        # Get all activities for the displayed dates
        start_date = month_days[0][0]
        end_date = month_days[-1][-1]
        
        start_dt = datetime.combine(start_date, datetime.min.time())
        end_dt = datetime.combine(end_date, datetime.max.time())
        
        activities = session.query(Activity).filter(
            Activity.start_time >= start_dt,
            Activity.start_time <= end_dt
        ).all()
        
        metrics = session.query(DailyMetrics).filter(
            DailyMetrics.day >= start_date,
            DailyMetrics.day <= end_date
        ).all()
        
        act_map = {}
        for a in activities:
            d = a.start_time.date()
            if d not in act_map: act_map[d] = []
            act_map[d].append(a)
            
        metric_map = {m.day: m for m in metrics}
        
        weeks = []
        for week in month_days:
            week_data = []
            for d in week:
                # Determine readiness color
                r_val = metric_map.get(d).readiness if metric_map.get(d) else None
                color = None
                if r_val is not None:
                    color = "green" if r_val >= 70 else "yellow" if r_val >= 40 else "red"
                
                week_data.append({
                    "date": d,
                    "is_current_month": d.month == m,
                    "is_today": d == today,
                    "activities": act_map.get(d, []),
                    "readiness_color": color,
                    "readiness_score": int(r_val) if r_val is not None else None
                })
                
            # ISO year and week for the Monday of this week
            iso_year, iso_week, _ = week[0].isocalendar()
            year_week = f"{iso_year}-W{iso_week:02d}"
            
            weeks.append({
                "days": week_data,
                "year_week": year_week,
                "is_current_week": today in week
            })
            
    month_name = calendar.month_name[m]
    
    return templates.TemplateResponse(request, "calendar.html", {
        "weeks": weeks,
        "month_name": month_name,
        "year": y,
        "prev_y": prev_y, "prev_m": prev_m,
        "next_y": next_y, "next_m": next_m
    })




if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.HOST, port=config.PORT, reload=False)
