"""GarminCoach FastAPI app — dashboard + sync + workout detail."""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from db import (
    Activity,
    AthleteProfile,
    DailyHealth,
    DailyMetrics,
    ExerciseSet,
    MetricSnapshot,
    PlannedSession,
    ProgramSession,
    Sleep,
    SyncState,
    TrainingProgram,
    Workout,
    Goal,
    CoachMessage,
    get_session,
    init_db,
)
from coach.onboarding import analyze_user_history, active_program, activity_family, program_sessions_for
from metrics.engine import acwr_label
from sync.garmin_client import client
from sync.scheduler import start_scheduler

app = FastAPI(title="GarminCoach")
app.mount("/static", StaticFiles(directory=str(config.PROJECT_ROOT / "static")), name="static")
templates = Jinja2Templates(directory=str(config.PROJECT_ROOT / "templates"))

import re

def clean_workout_name(name: str) -> str:
    if not name:
        return ""
    # Remove common emojis at start of name (e.g. "🏋️ ")
    name = re.sub(r'^[^\w\s]+\s*', '', name)
    # Remove time suffix (e.g. " @ 18:00")
    name = re.sub(r'\s*@\s*\d{1,2}:\d{2}\s*$', '', name)
    return name.strip()

templates.env.filters["clean_name"] = clean_workout_name
templates.env.filters["activity_family"] = activity_family


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

# Paths that don't require auth. NOTE: /sysinfo is intentionally NOT here — it
# runs journalctl and returns logs (emails, stack traces), so it must require a
# session cookie. /calendar/coach.ics stays public because external calendar
# apps fetch it without cookies (it carries no secrets).
_PUBLIC_PREFIXES = ("/static", "/app-login", "/favicon", "/calendar/coach.ics", "/telegram/webhook")


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


def _localize_message_created_at(created_at: datetime | None) -> datetime | None:
    """Return a message timestamp in the configured local timezone."""
    if created_at is None:
        return None

    from time_utils import get_local_tz

    local_tz = get_local_tz()
    if created_at.tzinfo is None:
        return local_tz.localize(created_at)
    return created_at.astimezone(local_tz)


def _ensure_schedule_target_date(payload: dict, msg: CoachMessage) -> dict:
    """Make date-less schedule actions stable across delayed approval clicks."""
    if payload.get("target_date"):
        return payload

    from time_utils import get_local_now

    created_at = _localize_message_created_at(msg.created_at) or get_local_now()
    target_date = created_at.date()
    if created_at.hour >= 17:
        target_date = target_date + timedelta(days=1)

    today = get_local_now().date()
    if target_date < today:
        target_date = today

    payload = dict(payload)
    payload["target_date"] = target_date.isoformat()
    return payload


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
        fa_tile["hint"] = "Garmin's estimate of how old your body performs. Lower is better: a 30-year-old with a fitness age of 22 has above-average cardiovascular fitness."
        
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
        # Latest row (today or most recent day with data) for load/ACWR.
        latest_metrics = (
            s.query(DailyMetrics)
            .filter(DailyMetrics.day <= today)
            .order_by(DailyMetrics.day.desc())
            .first()
        )
        # Readiness depends on finalized overnight data. If today's sleep/HRV
        # is not ready yet, use the most recent completed readiness row instead
        # of showing a misleading poor score.
        latest_readiness = (
            s.query(DailyMetrics)
            .filter(DailyMetrics.day <= today)
            .filter(DailyMetrics.readiness.isnot(None))
            .order_by(DailyMetrics.day.desc())
            .first()
        )
        # Previous day for trend arrows.
        prev = None
        if latest_readiness:
            prev = (
                s.query(DailyMetrics)
                .filter(DailyMetrics.day < latest_readiness.day)
                .filter(DailyMetrics.readiness.isnot(None))
                .order_by(DailyMetrics.day.desc())
                .first()
            )

        # Readiness tile.
        r_val = latest_readiness.readiness if latest_readiness else None
        
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
            "age": _age_label(latest_readiness.day.isoformat()) if latest_readiness else None,
            "trend": None,
            "desc": r_desc,
            "color": ("green" if r_val and r_val >= 70
                      else "yellow" if r_val and r_val >= 40
                      else "red" if r_val is not None
                      else None),
            "bar_pct": int(r_val) if r_val is not None else None,
            "hint": "Daily recovery score (0-100) based on your overnight HRV, resting heart rate, sleep duration, and Body Battery, all compared to your own 60-day personal baselines. Green (70+) = ready to push, yellow (40-69) = moderate, red (<40) = prioritize recovery.",
        }

        # ACWR tile.
        a_val = latest_metrics.acwr if latest_metrics else None
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
            "hint": "Acute:Chronic Workload Ratio: your last 7 days of training load divided by your last 28 days. Balanced (0.8-1.3) = steady progression. Ramping (1.3-1.5) = building up. Spike (>1.5) = sharp increase, higher injury risk. Detraining (<0.8) = doing less than usual.",
        }

        return [readiness_tile, acwr_tile]


def _overnight_metrics_ready(session) -> bool:
    try:
        from metrics.freshness import proactive_metrics_ready

        return proactive_metrics_ready(session)
    except Exception:
        return False


def _dashboard_health_series(health: list[DailyHealth], overnight_ready: bool) -> list[dict]:
    today = date.today()
    out = []
    for h in health:
        today_unready = h.day == today and not overnight_ready
        out.append(
            {
                "day": h.day.isoformat(),
                "rhr": None if today_unready else h.resting_hr,
                "hrv": None if today_unready else h.hrv_overnight,
                "hrv_baseline_low": None if today_unready else h.hrv_baseline_low,
                "hrv_baseline_high": None if today_unready else h.hrv_baseline_high,
                "bb_low": h.body_battery_low,
                "steps": h.steps,
                "step_goal": h.step_goal,
                "total_kcal": h.total_kcal,
                "active_kcal": h.active_kcal,
                "bmr_kcal": h.bmr_kcal,
            }
        )
    return out


def _dashboard_sleep_series(sleep: list[Sleep], overnight_ready: bool) -> list[dict]:
    today = date.today()
    out = []
    for sl in sleep:
        hours = None
        if sl.total_s and sl.total_s > 0 and (sl.day != today or overnight_ready):
            hours = round(sl.total_s / 3600, 1)
        out.append({"day": sl.day.isoformat(), "hours": hours, "score": sl.score})
    return out


# --- routes ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    needs_login = not client.is_authenticated()
    since = date.today() - timedelta(days=90)
    with get_session() as s:
        goal_row = s.get(Goal, 1)
        active_goal = goal_row.goal if goal_row and goal_row.goal else None
        profile = s.get(AthleteProfile, 1)
        current_program = active_program(s)
        
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
        overnight_ready = _overnight_metrics_ready(s)
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
        health_series = _dashboard_health_series(health, overnight_ready)
        sleep_series = _dashboard_sleep_series(sleep, overnight_ready)

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
            "profile": profile,
            "active_program": current_program,
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


# Plain-language explanations for the non-obvious metrics shown in hover tips.
_METRIC_HINTS = {
    "Avg speed": "Average speed over the whole activity, including any time spent stationary.",
    "Max speed": "Fastest instantaneous speed recorded during the activity.",
    "Moving time": "Time spent actually moving; excludes pauses and standing still.",
    "Avg cadence": "Steps per minute, or your running rhythm. This is a running metric and is less meaningful for stop-start sports.",
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
        rows.append(("Intensity min", f"{act.moderate_intensity_min or 0} mod | {act.vigorous_intensity_min or 0} vig"))
    te = _te_label(act.aerobic_te_msg)
    if act.training_effect_label or te:
        label = (act.training_effect_label or "").replace("_", " ").title()
        rows.append(("Training effect", f"{label}{' | ' + te if te else ''}".strip(" |")))
    return [{"label": k, "value": v, "hint": _METRIC_HINTS.get(k)} for k, v in rows]


def _hr_zones(activity_id: int, duration_s: float | None = None) -> list[dict]:
    """Time-in-HR-zone bars for a workout. Live (cached) fetch; returns [] on
    any failure so the page still renders. Each row: zone, low BPM, minutes,
    and pct of the activity's in-zone time (for the bar width)."""
    if not client.is_authenticated():
        return []
    try:
        raw = client.hr_zones(activity_id) or []
    except Exception:
        return []
    total_z = sum((z.get("secsInZone") or 0) for z in raw)
    
    # Use total activity duration if it's larger than the sum of Z1-Z5.
    base_total = duration_s if duration_s and duration_s > total_z else total_z
    if base_total <= 0:
        return []

    out = []
    
    # Add a "Below Z1" pseudo-zone for any remaining time
    if duration_s and duration_s > total_z + 60:
        below_secs = duration_s - total_z
        out.append({
            "zone": 0,
            "low_bpm": None,
            "minutes": round(below_secs / 60),
            "pct": round(below_secs / base_total * 100),
        })

    for z in raw:
        secs = z.get("secsInZone") or 0
        out.append({
            "zone": z.get("zoneNumber"),
            "low_bpm": round(z.get("zoneLowBoundary")) if z.get("zoneLowBoundary") else None,
            "minutes": round(secs / 60),
            "pct": round(secs / base_total * 100),
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
            "rpe": act.rpe,
            "feel": act.feel,
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
                        ex["delta_unit"] = "kg"
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
                        ex["delta_unit"] = "kg"
                    elif prev_vol == 0 and cur_vol == 0:
                        # Bodyweight exercise — compare total reps instead
                        prev_reps = sum(ps.reps or 0 for ps in prev_for_ex)
                        cur_reps = sum(
                            st["reps"] for st in ex["sets"] if st.get("reps")
                        )
                        ex["delta_vol"] = cur_reps - prev_reps  # 0 → "Same as last"
                        ex["delta_unit"] = " reps"
                    else:
                        ex["delta_vol"] = None
                        ex["delta_unit"] = "kg"
        else:
            cardio = _cardio_stats(act)

    return templates.TemplateResponse(
        request,
        "workout.html",
        {
            "activity": activity,
            "exercises": exercises,
            "cardio": cardio,
            "hr_zones": _hr_zones(activity_id, act.duration_s),
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
        try:
            if reps.strip():
                st.reps = int(reps)
            if weight_kg.strip():
                st.weight_kg = float(weight_kg)
        except ValueError:
            return HTMLResponse("Reps must be a whole number and weight a number.", status_code=400)
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
  <title>Login - GarminCoach</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: Inter, -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
      background: #0b1117; color: #edf4fb;
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh; padding: 1.5rem;
    }}
    .login-card {{
      background: #17212b; border: 1px solid #2d3a46; border-radius: 8px;
      padding: 2rem; width: 100%; max-width: 400px;
      box-shadow: 0 18px 45px rgba(0,0,0,.22);
    }}
    .brand-lockup {{ display: flex; align-items: center; justify-content: center; gap: .6rem; margin-bottom: .4rem; }}
    .brand-mark {{ display: inline-grid; place-items: center; width: 30px; height: 30px; border-radius: 7px; background: linear-gradient(135deg, #2f81f7, #3fb950); color: #fff; font-weight: 800; font-size: .8rem; }}
    .login-card h1 {{ font-size: 1.45rem; text-align: center; }}
    .login-card .sub {{ text-align: center; color: #9aacbc; font-size: .9rem; margin-bottom: 1.5rem; }}
    label {{ display: block; font-size: .9rem; color: #edf4fb; margin-bottom: .35rem; margin-top: 1rem; font-weight: 650; }}
    input[type=text], input[type=password] {{
      width: 100%; padding: .68rem .75rem; border-radius: 6px;
      border: 1px solid #2d3a46; background: #0b1117; color: #edf4fb;
      font-size: 1rem; outline: none; transition: border-color .2s, box-shadow .2s;
    }}
    input:focus {{ border-color: #58a6ff; box-shadow: 0 0 0 3px rgba(47,129,247,.18); }}
    button {{
      width: 100%; margin-top: 1.5rem; padding: .72rem;
      border: none; border-radius: 6px; cursor: pointer;
      font-size: .95rem; font-weight: 600;
      background: #2f81f7; color: #fff; transition: filter .2s;
    }}
    button:hover {{ filter: brightness(1.08); }}
    .error {{
      background: #3d1f1f; border: 1px solid #6e3630; border-radius: 6px;
      padding: .6rem .8rem; margin-bottom: 1rem; font-size: .85rem; color: #f85149;
    }}
  </style>
</head>
<body>
  <div class="login-card">
    <div class="brand-lockup"><span class="brand-mark">GC</span><h1>GarminCoach</h1></div>
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


def _safe_next(next: str) -> str:
    """Constrain post-login redirect to a local path, blocking open redirects.

    Accept only paths starting with a single '/'. Reject protocol-relative
    ('//evil.com'), absolute URLs ('https://evil.com'), and backslash tricks.
    """
    if not next or not next.startswith("/") or next.startswith("//") or "\\" in next:
        return "/"
    return next


@app.get("/app-login", response_class=HTMLResponse)
def app_login_form(request: Request, next: str = "/"):
    html = _APP_LOGIN_HTML.format(error_html="", next_url=_safe_next(next))
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
        # Success → set signed session cookie and redirect (local paths only).
        token = _sign_session(username.strip())
        response = RedirectResponse(_safe_next(next), status_code=303)
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
                "Garmin is rate-limiting your IP (HTTP 429): too many login "
                "attempts. Wait 15-60 minutes, then try again. This is a Garmin "
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


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


@app.get("/onboarding", response_class=HTMLResponse)
def get_onboarding(request: Request):
    """Fresh generic setup. Detection is advisory until the user confirms."""
    with get_session() as session:
        profile = session.get(AthleteProfile, 1) or AthleteProfile(id=1)
        analysis = analyze_user_history(session)
        current_program = active_program(session)
        return templates.TemplateResponse(
            request,
            "onboarding.html",
            {
                "profile": profile,
                "analysis": analysis,
                "active_program": current_program,
            },
        )


@app.post("/onboarding", response_class=RedirectResponse)
def post_onboarding(
    request: Request,
    experience_level: str = Form(""),
    primary_goal: str = Form(""),
    preferred_activities: str = Form(""),
    equipment_access: str = Form(""),
    availability: str = Form(""),
    injuries_limitations: str = Form(""),
    sport_commitments: str = Form(""),
    scheduling_preferences: str = Form(""),
    program_name: str = Form(""),
    plan_mode: str = Form("schedule_my_routine"),
    selected_templates: list[int] = Form([]),
    custom_sessions: str = Form(""),
):
    """Save profile and create an active program only from explicit choices."""
    with get_session() as session:
        profile = session.get(AthleteProfile, 1)
        if not profile:
            profile = AthleteProfile(id=1)
            session.add(profile)

        profile.experience_level = experience_level.strip()
        profile.primary_goal = primary_goal.strip()
        profile.preferred_activities = json.dumps(_split_csv(preferred_activities))
        profile.equipment_access = json.dumps(_split_csv(equipment_access))
        profile.availability = availability.strip()
        profile.injuries_limitations = injuries_limitations.strip()
        profile.sport_commitments = sport_commitments.strip()
        profile.scheduling_preferences = scheduling_preferences.strip()
        profile.approval_mode = "manual"
        profile.onboarding_complete = True
        profile.updated_at = datetime.now()

        for existing in session.query(TrainingProgram).filter(TrainingProgram.active.is_(True)).all():
            existing.active = False
            existing.updated_at = datetime.now()

        name = program_name.strip() or "My routine"
        program = TrainingProgram(
            name=name,
            mode=plan_mode.strip() or "schedule_my_routine",
            source_type="external_reference" if plan_mode == "known_plan" else "user_defined",
            goal_tags=json.dumps(_split_csv(primary_goal)),
            experience_level=experience_level.strip(),
            equipment=json.dumps(_split_csv(equipment_access)),
            active=True,
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        session.add(program)
        session.flush()

        order = 1
        if selected_templates:
            templates_by_id = {
                w.workout_id: w
                for w in session.query(Workout).filter(Workout.workout_id.in_(selected_templates)).all()
            }
            for wid in selected_templates:
                w = templates_by_id.get(wid)
                if not w:
                    continue
                session.add(
                    ProgramSession(
                        program_id=program.id,
                        name=w.name,
                        sport_type=w.sport_type,
                        sequence_order=order,
                        base_workout_id=w.workout_id,
                    )
                )
                order += 1

        for line in custom_sessions.splitlines():
            title = line.strip()
            if not title:
                continue
            session.add(
                ProgramSession(
                    program_id=program.id,
                    name=title,
                    sport_type="general",
                    sequence_order=order,
                    duration_min=60,
                )
            )
            order += 1

        session.commit()

    return RedirectResponse(url="/program", status_code=303)


@app.get("/program", response_class=HTMLResponse)
def get_program_page(request: Request):
    with get_session() as session:
        profile = session.get(AthleteProfile, 1)
        current_program = active_program(session)
        sessions = program_sessions_for(session, current_program.id) if current_program else []
        today = date.today()
        through = today + timedelta(days=14)
        planned = (
            session.query(PlannedSession)
            .filter(PlannedSession.target_date >= today)
            .filter(PlannedSession.target_date <= through)
            .order_by(PlannedSession.target_date.asc(), PlannedSession.suggested_time.asc())
            .all()
        )
        return templates.TemplateResponse(
            request,
            "program.html",
            {
                "profile": profile,
                "program": current_program,
                "sessions": sessions,
                "planned": planned,
            },
        )


@app.get("/calendar", response_class=HTMLResponse)
def get_calendar_page(request: Request, year: int = None, month: int = None):
    """Monthly calendar view with workouts and readiness."""
    import calendar
    
    today = date.today()
    y = year or today.year
    m = month or today.month

    # Guard against out-of-range input so monthdatescalendar can't raise a 500.
    if not (1 <= m <= 12) or not (1 <= y <= 9999):
        raise HTTPException(status_code=400, detail="Invalid year or month")

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


@app.get("/sysinfo")
def sysinfo():
    import subprocess
    import re
    from fastapi.responses import PlainTextResponse
    try:
        cmd = ["sudo", "journalctl", "-u", "garmincoach.service", "-n", "100", "--no-pager"]
        logs = subprocess.check_output(cmd).decode("utf-8")
        # Redact common secrets
        logs = re.sub(r"(?i)(api_key|token|password|secret)[\s=:\"']+([a-zA-Z0-9_\-\.]+)", r"\1=***REDACTED***", logs)
        return PlainTextResponse(f"LOGS:\n{logs}")
    except Exception as e:
        return PlainTextResponse(str(e))

@app.get("/calendar/coach.ics")
def coach_calendar_feed():
    """Serve an ICS calendar feed with all scheduled workout events."""
    import pytz
    from fastapi.responses import Response
    from icalendar import Calendar, Event

    tz_name = os.getenv("USER_TIMEZONE", "Asia/Jerusalem")
    local_tz = pytz.timezone(tz_name)

    cal = Calendar()
    cal.add('prodid', '-//GarminCoach//AI Workout//EN')
    cal.add('version', '2.0')
    cal.add('calscale', 'GREGORIAN')
    cal.add('method', 'PUBLISH')
    cal.add('x-wr-calname', 'GarminCoach Workouts')
    cal.add('x-wr-timezone', tz_name)

    with get_session() as session:
        row = session.get(SyncState, "coach_calendar_events")
        if row and row.value:
            try:
                events_list = json.loads(row.value)
            except Exception:
                events_list = []

            for ev_data in events_list:
                try:
                    ev_date = ev_data.get("date", "")
                    ev_time = ev_data.get("start_time", "18:30")
                    ev_title = ev_data.get("title", "Workout")
                    duration_min = ev_data.get("duration_min", 60)

                    dt_start = datetime.strptime(f"{ev_date} {ev_time}", "%Y-%m-%d %H:%M")
                    dt_start = local_tz.localize(dt_start)
                    dt_end = dt_start + timedelta(minutes=duration_min)

                    dt_start_utc = dt_start.astimezone(pytz.utc)
                    dt_end_utc = dt_end.astimezone(pytz.utc)

                    uid = f"garmincoach-{ev_date}-{ev_time.replace(':', '')}@garmincoach"

                    event = Event()
                    event.add('summary', ev_title)
                    event.add('description', 'Scheduled by GarminCoach AI')
                    event.add('dtstart', dt_start_utc)
                    event.add('dtend', dt_end_utc)
                    event.add('dtstamp', datetime.now(pytz.utc))
                    event.add('uid', uid)
                    event.add('status', 'CONFIRMED')
                    
                    cal.add_component(event)
                except Exception:
                    continue

    ics_content = cal.to_ical().decode("utf-8")

    return Response(
        content=ics_content,
        media_type="text/calendar; charset=utf-8",
        headers={
            "Content-Disposition": "inline; filename=coach.ics",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    """Handle incoming messages from Telegram."""
    
    # 0. Size Limit
    content_length = request.headers.get('content-length')
    if content_length and int(content_length) > 2 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Payload too large")

    # 1. Verify Secret Token
    secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if secret != config.TELEGRAM_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    try:
        data = await request.json()
        # If it's a callback query from an inline button
        if "callback_query" in data:
            callback = data["callback_query"]
            callback_id = callback.get("id")
            chat_id = callback.get("message", {}).get("chat", {}).get("id")
            message_id = callback.get("message", {}).get("message_id")
            callback_data = callback.get("data", "")
            
            from notify import telegram
            telegram.answer_callback_query(callback_id)
            
            if str(chat_id) == config.TELEGRAM_CHAT_ID:
                if callback_data.startswith("approve_workout_"):
                    msg_id = int(callback_data.split("_")[-1])
                    with get_session() as db:
                        from db import CoachMessage
                        msg = db.get(CoachMessage, msg_id)
                        if msg and msg.pending_action_json:
                            import json
                            from coach.garmin_compiler import compile_and_schedule
                            payload = json.loads(msg.pending_action_json)
                            payload = _ensure_schedule_target_date(payload, msg)
                            success = compile_and_schedule(db, payload)
                            
                            if success:
                                msg.pending_action_json = None
                                from notify.reminders import schedule_pre_workout_reminder
                                schedule_pre_workout_reminder(payload)
                                msg.content += "\n\n*Workout successfully approved, uploaded, and scheduled on your Garmin Calendar.*"
                                telegram.edit_message_text("*Workout successfully approved and scheduled.*", chat_id=str(chat_id), message_id=message_id)
                            else:
                                msg.content += "\n\n*Failed to schedule workout on Garmin.*"
                                telegram.edit_message_text("*Failed to schedule workout on Garmin.*", chat_id=str(chat_id), message_id=message_id)
                            db.commit()
                
                elif callback_data.startswith("reject_workout_"):
                    msg_id = int(callback_data.split("_")[-1])
                    with get_session() as db:
                        from db import CoachMessage
                        msg = db.get(CoachMessage, msg_id)
                        if msg:
                            msg.pending_action_json = None
                            db.commit()
                    telegram.edit_message_text("*Workout suggestion dismissed.*", chat_id=str(chat_id), message_id=message_id)
                    
                elif callback_data.startswith("reschedule_workout_"):
                    msg_id = int(callback_data.split("_")[-1])
                    with get_session() as db:
                        from db import CoachMessage
                        msg = db.get(CoachMessage, msg_id)
                        if msg:
                            msg.content += "\n\n*User requested to reschedule.*"
                            db.commit()
                    telegram.edit_message_text("*When would you like to reschedule it?* Reply with a time or day, for example: tomorrow at 18:00.", chat_id=str(chat_id), message_id=message_id)

            
            return {"status": "ok"}
            
        # Regular text message
        message = data.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        text = message.get("text")
        
        # We only care about text messages from the authorized chat
        if text and str(chat_id) == config.TELEGRAM_CHAT_ID:
            from coach.coach import handle_chat
            from notify import telegram
            
            telegram.send_chat_action(str(chat_id), "typing")
            
            # Pass to AI Coach
            with get_session() as db:
                response_text, asst_msg = handle_chat(db, text)
                
                reply_markup = None
                if asst_msg.pending_action_json:
                    reply_markup = {
                        "inline_keyboard": [
                            [
                                {"text": "Approve and schedule", "callback_data": f"approve_workout_{asst_msg.id}"},
                                {"text": "Not today", "callback_data": f"reschedule_workout_{asst_msg.id}"}
                            ],
                            [
                                {"text": "Dismiss", "callback_data": f"reject_workout_{asst_msg.id}"}
                            ]
                        ]
                    }
            
            # Send response back to Telegram
            telegram.send_message(response_text, chat_id=str(chat_id), reply_markup=reply_markup)
            
    except Exception as e:
        import logging
        logging.error(f"Telegram webhook failed: {e}")
        try:
            from notify import telegram
            chat_id = (data.get("message") or {}).get("chat", {}).get("id")
            if chat_id and str(chat_id) == config.TELEGRAM_CHAT_ID:
                telegram.send_message(f"*Coach error:*\nAn error occurred while processing your request:\n`{str(e)}`\n\nThis could be a temporary issue with the AI provider, such as a rate limit. Please try again later.", chat_id=str(chat_id))
        except Exception:
            pass
        
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.HOST, port=config.PORT, reload=False)
