"""GarminCoach FastAPI app — dashboard + sync + workout detail."""
from __future__ import annotations

import os
import threading
from datetime import date, datetime, timedelta

import pytz
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import config
from db import (
    Activity,
    ActivityProgramMatch,
    AthleteProfile,
    DailyHealth,
    DailyMetrics,
    ExerciseSet,
    MetricSnapshot,
    PlannedSession,
    ProgramSession,
    SessionExercise,
    Sleep,
    SyncState,
    TrainingProgram,
    Workout,
    Goal,
    CoachMessage,
    get_session,
    init_db,
)
from coach.onboarding import (
    analyze_user_history,
    active_program,
    activity_family,
    clean_session_name,
    latest_draft_program,
    program_sessions_for,
)
from coach.programs import PLAN_CHOICES, PROGRAMS, recommend_program, warmup_defaults
from coach.exercises import catalog_for_ui, exercise_key, exercise_metadata, muscle_group_for
from metrics.engine import acwr_label
from sync.garmin_client import client
from sync.scheduler import start_multi_user_scheduler, start_scheduler
from time_utils import get_local_date
from auth_routes import SESSION_COOKIE as MULTI_USER_SESSION_COOKIE
from auth_routes import router as auth_router
from account_routes import router as account_router
from setup_routes import CONSENT_VERSION, router as setup_router
from auth_service import resolve_web_session
from control_db import get_control_session, init_control_db
from tenant_context import TenantIdentity, bind_tenant, reset_tenant, tenant_scope

app = FastAPI(title="GarminCoach")
app.mount("/static", StaticFiles(directory=str(config.PROJECT_ROOT / "static")), name="static")
app.include_router(auth_router)
app.include_router(account_router)
app.include_router(setup_router)
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


def _apply_recent_strength_weights(session, proposal: dict) -> None:
    """Fill source exercises from recent matching Garmin sets; never guess."""
    cutoff = datetime.now() - timedelta(days=90)
    rows = (
        session.query(ExerciseSet)
        .join(Activity, ExerciseSet.activity_id == Activity.id)
        .filter(Activity.start_time >= cutoff)
        .filter(ExerciseSet.weight_kg > 0)
        .filter(ExerciseSet.set_type != "REST")
        .order_by(Activity.start_time.desc(), ExerciseSet.set_index.desc())
        .all()
    )
    by_key: dict[str, list[ExerciseSet]] = {}
    for row in rows:
        key = exercise_key(row.exercise_name or row.exercise_category or "")
        if exercise_metadata(key):
            by_key.setdefault(key, []).append(row)

    for routine in proposal["sessions"]:
        for exercise in routine["exercises"]:
            matches = by_key.get(exercise["exercise_key"], [])
            target_reps = exercise.get("reps")
            if target_reps:
                compatible = [row for row in matches if row.reps and row.reps >= target_reps]
                matches = compatible or matches
            if not matches:
                continue
            weight = round(float(matches[0].weight_kg) * 2) / 2
            exercise["weight_kg"] = weight
            if exercise.get("warmup_enabled"):
                exercise["warmup_weight_kg"] = round(weight * 0.5, 1)


def _replace_program_sessions(session, program: TrainingProgram, routines: list[dict]) -> None:
    """Replace editable program sessions with the source-template proposal."""
    for existing in session.query(ProgramSession).filter_by(program_id=program.id).all():
        session.delete(existing)
    session.flush()
    for order, routine in enumerate(routines, start=1):
        planned_session = ProgramSession(
            program_id=program.id,
            name=routine["name"],
            sport_type=routine["sport_type"],
            sequence_order=order,
            focus_tags=json.dumps(routine["focus_tags"]),
            duration_min=routine["duration_min"],
            notes=routine.get("notes", ""),
            session_role=routine.get("session_role", "coach_strength"),
            target_frequency=routine.get("target_frequency", 1),
        )
        session.add(planned_session)
        session.flush()
        _replace_session_exercises(session, planned_session, routine["exercises"])


def _replace_session_exercises(session, program_session: ProgramSession, exercises: list[dict]) -> None:
    """Restore one session's editable exercises without replacing the session itself."""
    session.query(SessionExercise).filter_by(program_session_id=program_session.id).delete()
    for exercise_order, exercise in enumerate(exercises):
        session.add(
            SessionExercise(
                program_session_id=program_session.id,
                exercise_name=exercise["exercise_name"],
                exercise_key=exercise["exercise_key"],
                garmin_category=exercise["garmin_category"],
                garmin_name=exercise["garmin_name"],
                movement_pattern=exercise["movement_pattern"],
                is_generic=exercise["is_generic"],
                sets=exercise["sets"],
                reps=exercise["reps"],
                duration_seconds=exercise["duration_seconds"],
                rest_seconds=exercise["rest_seconds"],
                warmup_enabled=exercise["warmup_enabled"],
                warmup_reps=exercise["warmup_reps"],
                warmup_duration_seconds=exercise["warmup_duration_seconds"],
                warmup_weight_kg=exercise["warmup_weight_kg"],
                weight_kg=exercise.get("weight_kg"),
                order_index=exercise_order,
                notes=exercise.get("notes", ""),
            )
        )


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
        if config.MULTI_USER_ENABLED:
            path = request.url.path
            public = ("/static", "/auth", "/invite", "/favicon", "/telegram/webhook")
            if path.startswith(public):
                return await call_next(request)
            raw_session = request.cookies.get(MULTI_USER_SESSION_COOKIE, "")
            with get_control_session() as control_session:
                user = resolve_web_session(control_session, raw_session)
                if user is None:
                    return RedirectResponse("/auth/login", status_code=303)
                request.state.user = user
                tenant = TenantIdentity(user.id, role=user.role, timezone=user.timezone)
                with tenant_scope(tenant):
                    onboarding_allowed = (
                        (path == "/onboarding" and request.method == "GET")
                        or path.startswith("/setup/")
                    )
                    if user.status != "active" and not onboarding_allowed:
                        return RedirectResponse("/onboarding", status_code=303)
                    response = await call_next(request)
                    response.headers.setdefault("Cache-Control", "no-store")
                    return response
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
    """Cache-buster for local static assets used by rendered pages."""
    try:
        static_dir = config.PROJECT_ROOT / "static"
        return int(max(
            os.path.getmtime(static_dir / "style.css"),
            os.path.getmtime(static_dir / "ui.css"),
            os.path.getmtime(static_dir / "ui.js"),
            os.path.getmtime(static_dir / "onboarding.js"),
        ))
    except OSError:
        return 0


templates.env.globals["asset_version"] = _asset_version
templates.env.globals["multi_user_enabled"] = config.MULTI_USER_ENABLED


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
    if config.MULTI_USER_ENABLED:
        init_control_db()
        start_multi_user_scheduler()
        return
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
    """Capability-aware recovery facts plus descriptive, UI-only ACWR."""
    with get_session() as s:
        # The server and CI runners use UTC, while the athlete's day is defined
        # by USER_TIMEZONE.  Using the host date here hid freshly synced
        # overnight data between local midnight and UTC midnight.
        today = get_local_date()
        # Latest row (today or most recent day with data) for load/ACWR.
        latest_metrics = (
            s.query(DailyMetrics)
            .filter(DailyMetrics.day <= today)
            .order_by(DailyMetrics.day.desc())
            .first()
        )
        from coach.decision_engine import sleep_score_category, training_readiness_category
        from metrics.freshness import (
            FRESH,
            HRV,
            RESTING_HR,
            SLEEP,
            SLEEP_SCORE,
            STRESS,
            TRAINING_READINESS,
            capability_state,
            synced_raw_metrics_ready,
        )
        from db import ObservationFreshness

        health = s.get(DailyHealth, today)
        sleep = s.get(Sleep, today)
        freshness_row = s.get(ObservationFreshness, (TRAINING_READINESS, today))
        capability = capability_state(s)
        r_val = (
            health.training_readiness
            if health and health.training_readiness is not None
            and freshness_row and freshness_row.state == FRESH
            else None
        )
        category = training_readiness_category(int(r_val)) if r_val is not None else None
        if capability == "unsupported":
            raw_facts_synced = synced_raw_metrics_ready(s, today)

            def is_fresh(signal: str) -> bool:
                row = s.get(ObservationFreshness, (signal, today))
                if row:
                    return row.state == FRESH
                return raw_facts_synced

            signal_rows = []
            if sleep and sleep.total_s and is_fresh(SLEEP):
                total_minutes = int(round(sleep.total_s / 60.0))
                hours, minutes = divmod(total_minutes, 60)
                sleep_value = f"{hours}h {minutes:02d}m"
                if sleep.score is not None and is_fresh(SLEEP_SCORE):
                    sleep_category = sleep_score_category(sleep.score)
                    sleep_value += f" · score {int(round(sleep.score))}"
                    if sleep_category:
                        sleep_value += f" ({sleep_category})"
                    sleep_indicator = sleep_category
                    sleep_tone = {
                        "Excellent": "positive",
                        "Good": "positive",
                        "Fair": "caution",
                        "Poor": "alert",
                    }.get(sleep_category, "neutral")
                else:
                    sleep_indicator = "Measured"
                    sleep_tone = "neutral"
            else:
                sleep_value = "Not available today"
                sleep_indicator = "No data"
                sleep_tone = "neutral"
            signal_rows.append({
                "label": "Sleep",
                "value": sleep_value,
                "indicator": sleep_indicator,
                "tone": sleep_tone,
            })

            if health and health.hrv_overnight is not None and is_fresh(HRV):
                hrv = int(round(health.hrv_overnight))
                if health.hrv_baseline_low is not None and health.hrv_baseline_high is not None:
                    low = int(round(health.hrv_baseline_low))
                    high = int(round(health.hrv_baseline_high))
                    if hrv < low:
                        hrv_state = "below"
                    elif hrv > high:
                        hrv_state = "above"
                    else:
                        hrv_state = "within"
                    hrv_value = f"{hrv} ms · {hrv_state} {low}–{high} baseline"
                    hrv_indicator = f"{hrv_state.title()} baseline"
                    hrv_tone = "positive" if hrv_state == "within" else "caution"
                else:
                    hrv_value = f"{hrv} ms · baseline unavailable"
                    hrv_indicator = "Measured"
                    hrv_tone = "neutral"
            else:
                hrv_value = "Not available today"
                hrv_indicator = "No data"
                hrv_tone = "neutral"
            signal_rows.append({
                "label": "HRV",
                "value": hrv_value,
                "indicator": hrv_indicator,
                "tone": hrv_tone,
            })

            if health and health.resting_hr is not None and is_fresh(RESTING_HR):
                from statistics import median

                rhr = int(round(health.resting_hr))
                recent_rhr = [
                    float(value)
                    for (value,) in (
                        s.query(DailyHealth.resting_hr)
                        .filter(
                            DailyHealth.day < today,
                            DailyHealth.day >= today - timedelta(days=28),
                            DailyHealth.resting_hr.isnot(None),
                        )
                        .all()
                    )
                ]
                if len(recent_rhr) >= 7:
                    recent_median = int(round(median(recent_rhr)))
                    delta = rhr - recent_median
                    if delta == 0:
                        comparison = "matches 28-day median"
                        rhr_indicator = "At median"
                    else:
                        direction = "above" if delta > 0 else "below"
                        comparison = f"{abs(delta)} bpm {direction} 28-day median"
                        rhr_indicator = f"{direction.title()} median"
                    rhr_value = f"{rhr} bpm · {comparison}"
                    rhr_tone = "comparison"
                else:
                    rhr_value = f"{rhr} bpm · recent baseline unavailable"
                    rhr_indicator = "Measured"
                    rhr_tone = "neutral"
            else:
                rhr_value = "Not available today"
                rhr_indicator = "No data"
                rhr_tone = "neutral"
            signal_rows.append({
                "label": "Resting HR",
                "value": rhr_value,
                "indicator": rhr_indicator,
                "tone": rhr_tone,
            })

            if sleep and sleep.sleep_stress_avg is not None and is_fresh(SLEEP):
                stress_label = "Sleep stress"
                stress = int(round(sleep.sleep_stress_avg))
            elif health and health.stress_avg is not None and is_fresh(STRESS):
                stress_label = "Stress today"
                stress = int(round(health.stress_avg))
            else:
                stress_label = "Stress"
                stress = None
            if stress is not None and 0 <= stress <= 100:
                if stress <= 25:
                    stress_category = "resting range"
                elif stress <= 50:
                    stress_category = "low"
                elif stress <= 75:
                    stress_category = "medium"
                else:
                    stress_category = "high"
                stress_value = f"{stress} · Garmin {stress_category}"
                stress_indicator = stress_category.title()
                stress_tone = {
                    "resting range": "positive",
                    "low": "positive",
                    "medium": "caution",
                    "high": "alert",
                }[stress_category]
            else:
                stress_value = "Not available today"
                stress_indicator = "No data"
                stress_tone = "neutral"
            signal_rows.append({
                "label": stress_label,
                "value": stress_value,
                "indicator": stress_indicator,
                "tone": stress_tone,
            })
            readiness_tile = {
                "key": "recovery_signals",
                "label": "Recovery signals",
                "signal_rows": signal_rows,
                "hint": "Your watch does not provide Garmin Training Readiness. These synced signals are shown separately without applying unvalidated composite weights.",
            }
        elif r_val is None:
            readiness_tile = {
                "key": "readiness", "label": "Garmin Readiness",
                "value": None, "unit": "", "empty_value": "-", "empty_label": "No data yet",
                "prev": None, "age": None, "trend": None,
                "desc": "Waiting for today's Garmin Training Readiness.",
                "color": None, "bar_pct": None,
                "hint": "Garmin's 1-100 Training Readiness score and official category. It supports a decision but does not predict performance.",
            }
        else:
            readiness_tile = {
                "key": "readiness", "label": "Garmin Readiness",
                "value": int(r_val), "unit": "", "empty_value": None, "empty_label": None,
                "prev": None, "age": category, "trend": None, "desc": category or "",
                "color": ("green" if category in {"Prime", "High", "Moderate"}
                          else "yellow" if category == "Low"
                          else "red" if category == "Poor"
                          else None),
                "bar_pct": int(r_val),
                "hint": "Garmin's 1-100 Training Readiness score and official category. It supports a decision but does not predict performance.",
            }

        # ACWR tile.
        a_val = latest_metrics.acwr if latest_metrics else None
        # Bar position: map ACWR 0–2.0 to 0–100%, capped.
        a_bar_pct = min(100, int(a_val / 2.0 * 100)) if a_val is not None else None
        acwr_tile = {
            "key": "acwr", "label": "ACWR",
            "value": a_val,
            "unit": "",
            "is_gauge": True,
            "age": None,
            "desc": None,
            "color": None,
            "bar_pct": a_bar_pct,
            "hint": "Acute:Chronic Workload Ratio. Display only; it has no authority in workout or injury-risk recommendations.",
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
        start_t = sl.sleep_start_time.strftime("%H:%M") if sl.sleep_start_time else None
        end_t = sl.sleep_end_time.strftime("%H:%M") if sl.sleep_end_time else None
        out.append({
            "day": sl.day.isoformat(),
            "hours": hours,
            "score": sl.score,
            "start_time": start_t,
            "end_time": end_t,
        })
    return out


def _dashboard_chart_data(session) -> dict:
    """Return the dashboard series used by the in-page chart refresh."""
    since = date.today() - timedelta(days=90)
    health = (
        session.query(DailyHealth)
        .filter(DailyHealth.day >= since)
        .order_by(DailyHealth.day.asc())
        .all()
    )
    sleep = (
        session.query(Sleep)
        .filter(Sleep.day >= since)
        .order_by(Sleep.day.asc())
        .all()
    )
    overnight_ready = _overnight_metrics_ready(session)
    return {
        "health_series": _dashboard_health_series(health, overnight_ready),
        "sleep_series": _dashboard_sleep_series(sleep, overnight_ready),
    }


def _dashboard_hero(readiness_tiles: list[dict], sleep_series: list[dict]) -> dict:
    """Build display-only headline metrics from existing Garmin observations."""
    readiness = next(
        (tile for tile in readiness_tiles if tile.get("key") in {"readiness", "recovery_signals"}),
        {},
    )
    latest_sleep = next(
        (row for row in reversed(sleep_series) if row.get("hours") is not None or row.get("score") is not None),
        {},
    )
    sleep_score = latest_sleep.get("score")
    sleep_hours = latest_sleep.get("hours")
    start_t = latest_sleep.get("start_time")
    end_t = latest_sleep.get("end_time")
    with get_session() as session:
        metrics = (
            session.query(DailyMetrics)
            .filter(DailyMetrics.day <= get_local_date())
            .order_by(DailyMetrics.day.desc())
            .first()
        )
        load_value = metrics.acwr if metrics else None
        acute_load = metrics.acute_load if metrics else None
        chronic_load = metrics.chronic_load if metrics else None

        latest_sleep_db = (
            session.query(Sleep)
            .filter(Sleep.total_s > 0)
            .order_by(Sleep.day.desc())
            .first()
        )
        if latest_sleep_db and latest_sleep_db.sleep_start_time and latest_sleep_db.sleep_end_time:
            st = latest_sleep_db.sleep_start_time.strftime("%H:%M")
            et = latest_sleep_db.sleep_end_time.strftime("%H:%M")
            sleep_time_range = f"{st} - {et}"
        else:
            sleep_time_range = f"{start_t} - {end_t}" if start_t and end_t else None

    readiness_value = readiness.get("value")
    readiness_color = readiness.get("color")
    readiness_tone = (
        "good" if readiness_color == "green"
        else "caution" if readiness_color == "yellow"
        else "poor" if readiness_color == "red"
        else "neutral"
    )
    return {
        "readiness": {
            "value": readiness_value,
            "progress": max(0, min(100, float(readiness_value or 0))),
            "detail": readiness.get("age") or readiness.get("desc") or "Waiting for today's data",
            "tone": readiness_tone,
            "signals": readiness.get("signal_rows"),
            "hint": readiness.get("hint"),
        },
        "sleep": {
            "value": int(round(sleep_score)) if sleep_score is not None else sleep_hours,
            "unit": "score" if sleep_score is not None else ("hours" if sleep_hours is not None else "no data"),
            "progress": max(0, min(100, float(sleep_score or 0))),
            "detail": f"{sleep_hours:g} hours last night" if sleep_hours is not None else "Waiting for last night's sleep",
            "time_range": sleep_time_range,
        },
        "load": {
            "value": round(load_value, 2) if load_value is not None else None,
            "progress": max(0, min(100, float(load_value or 0) / 2.0 * 100)),
            "detail": (
                f"Acute load: {acute_load:.0f} · Chronic avg: {chronic_load:.0f}"
                if acute_load is not None and chronic_load is not None
                else "Not enough load history"
            ),
        },
    }
# --- routes ---------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    needs_login = not client.is_authenticated()
    if needs_login:
        return RedirectResponse("/login", status_code=303)
    since = date.today() - timedelta(days=90)
    with get_session() as s:
        goal_row = s.get(Goal, 1)
        active_goal = goal_row.goal if goal_row and goal_row.goal else None
        profile = s.get(AthleteProfile, 1) or AthleteProfile(id=1)
        current_program = active_program(s)
        
        # All workouts in the past month (no row cap).
        activities = (
            s.query(Activity)
            .filter(Activity.start_time >= datetime.combine(since, datetime.min.time()))
            .order_by(Activity.start_time.desc())
            .all()
        )
        chart_data = _dashboard_chart_data(s)
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

    fitness_tiles = _fitness_tiles()
    readiness_tiles = _readiness_tiles()
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "needs_login": needs_login,
            "activities": activities,
            **chart_data,
            "fitness_tiles": fitness_tiles,
            "readiness_tiles": readiness_tiles,
            "hero_metrics": _dashboard_hero(readiness_tiles, chart_data["sleep_series"]),
            "today_label": get_local_date().strftime("%A, %b %d"),
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
def sync_now(request: Request, full: bool = Form(False)):
    wants_json = "application/json" in request.headers.get("accept", "")
    # Can't sync without an authenticated Garmin session — send to login.
    if not client.is_authenticated():
        if wants_json:
            return JSONResponse({"error": "Garmin authentication required"}, status_code=401)
        return RedirectResponse("/login", status_code=303)
    started = sync_runner.try_start_sync(full, force=not full)
    if wants_json:
        return JSONResponse({"started": started, "running": sync_runner.is_running()})
    return RedirectResponse("/", status_code=303)


@app.get("/sync/status")
def sync_status():
    """JSON endpoint polled by the dashboard while a sync is in progress."""
    running = sync_runner.is_running()
    payload = {
        "running": running,
        "summary": sync_runner.status["summary"],
        "last_sync_at": _last_sync_at(),
        "device_last_upload": _device_last_upload(),
    }
    if not running:
        with get_session() as session:
            payload.update(_dashboard_chart_data(session))
    return JSONResponse(payload)


@app.post("/sync/reset")
def sync_reset(request: Request):
    """Escape hatch: force-clear a stuck 'syncing' state."""
    sync_runner.reset()
    if "application/json" in request.headers.get("accept", ""):
        return JSONResponse({"running": False})
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


@app.get("/app-login")
def app_login_form(request: Request, next: str = "/"):
    return RedirectResponse("/auth/login", status_code=303)


@app.post("/app-login", response_class=HTMLResponse)
def app_login_submit(
    request: Request,
    username: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),
):
    if config.MULTI_USER_ENABLED:
        raise HTTPException(status_code=404)
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
    return templates.TemplateResponse(
        request,
        "app_login.html",
        {"error": "Invalid username or password.", "next_url": _safe_next(next)},
        status_code=401,
    )


@app.get("/app-logout")
def app_logout():
    if config.MULTI_USER_ENABLED:
        return RedirectResponse("/auth/login", status_code=303)
    response = RedirectResponse("/app-login", status_code=303)
    response.delete_cookie(_COOKIE_NAME, path="/")
    return response


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if config.MULTI_USER_ENABLED:
        return RedirectResponse("/auth/login", status_code=303)
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
    return RedirectResponse("/onboarding", status_code=303)


def _split_csv(value: str) -> list[str]:
    return [v.strip() for v in (value or "").split(",") if v.strip()]


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except Exception:
        return []
    return [str(v) for v in parsed] if isinstance(parsed, list) else []


def _clean_list(values: list[str] | None) -> list[str]:
    return [v.strip() for v in (values or []) if v and v.strip()]


def _constraint_session_duration(constraints: str, default: int = 60) -> int:
    """Use a clear duration mentioned in free-text constraints, otherwise 60 minutes."""
    match = re.search(r"\b(\d{2,3})\s*(?:min(?:ute)?s?)\b", constraints, flags=re.IGNORECASE)
    return min(180, max(20, int(match.group(1)))) if match else default


def _onboarding_form_defaults(
    profile: AthleteProfile,
    goal: Goal | None,
    analysis: dict,
    current_program: TrainingProgram | None,
    current_sessions: list[ProgramSession],
) -> dict:
    defaults = dict(analysis.get("defaults", {}))
    if profile and profile.onboarding_complete:
        defaults["training_type"] = profile.training_type or defaults.get("training_type", "")

    if current_program:
        defaults["program_name"] = current_program.name
        defaults["plan_mode"] = current_program.mode
        tags = _json_list(current_program.goal_tags)
        plan_keys = {choice["key"] for choice in PLAN_CHOICES}
        selected_key = next((tag for tag in tags if tag in plan_keys), None)
        if selected_key:
            defaults["plan_key"] = selected_key
        selected = [ps.base_workout_id for ps in current_sessions if ps.base_workout_id]
        if selected:
            defaults["selected_templates"] = selected
    return defaults


@app.get("/onboarding", response_class=HTMLResponse)
def get_onboarding(request: Request):
    """Fresh generic setup. Detection is advisory until the user confirms."""
    if config.MULTI_USER_ENABLED:
        user = getattr(request.state, "user", None)
        if user and user.onboarding_step != "complete":
            error_messages = {
                "consent_required": "You must accept the privacy notice to continue.",
                "invalid_timezone": "Choose a valid timezone from the list.",
                "configuration_required": "Complete the privacy and timezone steps first.",
                "garmin_rate_limited": "Garmin is rate limiting logins. Wait before trying again.",
                "garmin_auth_failed": "Garmin could not verify those credentials.",
                "garmin_session_expired": "The Garmin verification session expired. Sign in again.",
                "garmin_mfa_failed": "Garmin could not verify that one-time code.",
            }
            return templates.TemplateResponse(
                request,
                "multi_onboarding.html",
                {
                    "user": user,
                    "timezones": pytz.common_timezones,
                    "error": error_messages.get(request.query_params.get("error", "")),
                    "consent_version": CONSENT_VERSION,
                },
                headers={"Cache-Control": "no-store"},
            )
    with get_session() as session:
        profile = session.get(AthleteProfile, 1) or AthleteProfile(id=1)
        goal = session.get(Goal, 1) or Goal(id=1, goal="", custom_input="")
        analysis = analyze_user_history(session)
        current_program = latest_draft_program(session) or active_program(session)
        current_sessions = program_sessions_for(session, current_program.id) if current_program else []
        form_defaults = _onboarding_form_defaults(profile, goal, analysis, current_program, current_sessions)
        form_defaults.setdefault("plan_key", analysis["plan_recommendation"]["key"])
        return templates.TemplateResponse(
            request,
            "onboarding.html",
            {
                "profile": profile,
                "goal": goal,
                "analysis": analysis,
                "active_program": current_program,
                "form_defaults": form_defaults,
                "plan_choices": sorted(
                    PLAN_CHOICES,
                    key=lambda choice: analysis["plan_matches"][choice["key"]]["rank"],
                ),
                "garmin_connected": client.is_authenticated(),
                "sync_running": sync_runner.is_running(),
                "is_editing": bool(profile.onboarding_complete),
            },
        )


@app.get("/onboarding/status")
def onboarding_status():
    """Small polling payload for the sync-first onboarding screen."""
    with get_session() as session:
        analysis = analyze_user_history(session)
    return JSONResponse({
        "running": sync_runner.is_running(),
        "total_activities": analysis["total_activities"],
        "classification": analysis["classification"],
        "activity_patterns": analysis["activity_patterns"],
        "recent_routine": analysis["recent_routine"],
        "training_background": analysis["training_background"],
        "plan_recommendation": analysis["plan_recommendation"],
        "plan_matches": analysis["plan_matches"],
        "last_sync_at": _last_sync_at(),
    })


@app.post("/onboarding", response_class=RedirectResponse)
def post_onboarding(
    request: Request,
    plan_key: str = Form(""),
    injuries_limitations: str = Form(""),
):
    """Save setup and create a reviewable, undated program proposal."""
    with get_session() as session:
        profile = session.get(AthleteProfile, 1)
        if not profile:
            profile = AthleteProfile(id=1)
            session.add(profile)

        analysis = analyze_user_history(session)
        requested_duration = _constraint_session_duration(injuries_limitations)
        if plan_key not in PROGRAMS:
            raise HTTPException(status_code=422, detail="Choose one of the available gym routines.")
        template = PROGRAMS[plan_key]
        try:
            proposal = recommend_program(
                plan_key=plan_key,
                limitations=injuries_limitations,
                session_duration_min=requested_duration,
                history_summary=analysis["classification"]["reason"],
            )
            _apply_recent_strength_weights(session, proposal)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))

        profile.training_type = "strength_focused"
        profile.experience_level = template["experience"]
        profile.primary_goal = ""
        profile.goal_detail = ""
        profile.equipment_access = json.dumps(["gym"])
        profile.availability = ""
        profile.timing_preferences = ""
        profile.injuries_limitations = injuries_limitations.strip()
        profile.scheduling_preferences = "Scheduling options: manual_approval"
        profile.approval_mode = "manual"
        profile.onboarding_complete = True
        profile.updated_at = datetime.now()

        goal_row = session.get(Goal, 1)
        if not goal_row:
            goal_row = Goal(id=1)
            session.add(goal_row)
        goal_row.goal = ""
        goal_row.custom_input = injuries_limitations.strip()
        goal_row.updated_at = datetime.now()

        for existing in session.query(TrainingProgram).filter(TrainingProgram.status == "draft").all():
            existing.status = "archived"
            existing.updated_at = datetime.now()

        name = proposal["name"]
        program = TrainingProgram(
            name=name,
            mode="curated_strength",
            source_type="curated_archetype",
            source_url=proposal["source_url"],
            attribution=proposal["attribution"],
            goal_tags=json.dumps([proposal["key"]]),
            experience_level=template["experience"],
            days_per_week=proposal["days_per_week"],
            equipment=json.dumps(["gym"]),
            active=False,
            status="draft",
            rationale=proposal["rationale"],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )
        session.add(program)
        session.flush()

        _replace_program_sessions(session, program, proposal["sessions"])
        return RedirectResponse(url=f"/program?proposal={program.id}", status_code=303)


@app.get("/program", response_class=HTMLResponse)
def get_program_page(
    request: Request,
    proposal: int | None = None,
    view: str = "",
    approved: int = 0,
):
    with get_session() as session:
        profile = session.get(AthleteProfile, 1)
        active = active_program(session)
        draft = None
        if view in ("draft", "proposal", "review") or proposal:
            draft = session.get(TrainingProgram, proposal) if proposal else latest_draft_program(session)
            if not draft and active:
                draft = None
        elif not active:
            draft = latest_draft_program(session)
        if draft and draft.status != "draft":
            draft = None
        current_program = draft or active
        sessions = program_sessions_for(session, current_program.id) if current_program else []
        strength_sessions = [item for item in sessions if item.session_role == "coach_strength"]
        # Additional sessions created before ``is_custom`` was introduced were
        # appended after the curated template sessions. Mark them on first view
        # so they gain the same removable-session controls as new additions.
        plan_keys = _json_list(current_program.goal_tags) if current_program else []
        template = PROGRAMS.get(plan_keys[0]) if plan_keys else None
        template_session_count = len(template["sessions"]) if template else 0
        if template_session_count:
            for ps in strength_sessions:
                if ps.sequence_order > template_session_count and not ps.is_custom:
                    ps.is_custom = True

        # Load exercises for each session, keyed by session id
        exercises_by_session: dict[int, list] = {}
        session_ready: dict[int, bool] = {}
        for ps in sessions:
            exs = (
                session.query(SessionExercise)
                .filter_by(program_session_id=ps.id)
                .order_by(SessionExercise.order_index)
                .all()
            )
            exercises_by_session[ps.id] = [
                {
                    "id": ex.id,
                    "exercise_name": ex.exercise_name,
                    "exercise_key": ex.exercise_key,
                    "garmin_category": ex.garmin_category,
                    "garmin_name": ex.garmin_name,
                    "movement_pattern": ex.movement_pattern,
                    "muscle_group": muscle_group_for(ex.exercise_key or ex.exercise_name, ex.movement_pattern),
                    "is_generic": ex.is_generic,
                    "sets": ex.sets,
                    "reps": ex.reps,
                    "duration_seconds": ex.duration_seconds,
                    "weight_kg": ex.weight_kg,
                    "rest_seconds": ex.rest_seconds,
                    "warmup_enabled": ex.warmup_enabled,
                    "warmup_reps": ex.warmup_reps,
                    "warmup_duration_seconds": ex.warmup_duration_seconds,
                    "warmup_weight_kg": ex.warmup_weight_kg,
                    "order_index": ex.order_index,
                    "notes": ex.notes,
                }
                for ex in exs
            ]
            if exs:
                from coach.garmin_compiler import build_program_workout
                try:
                    build_program_workout(session, ps.id, require_active=False)
                    session_ready[ps.id] = True
                except ValueError:
                    session_ready[ps.id] = False
            else:
                session_ready[ps.id] = False

        # User physical profile (from Garmin sync)
        gender_row = session.get(SyncState, "user_gender")
        weight_row = session.get(SyncState, "user_weight")
        birth_row = session.get(SyncState, "user_birth_date")
        age = None
        if birth_row and birth_row.value:
            try:
                bd = date.fromisoformat(birth_row.value[:10])
                today_date = date.today()
                age = today_date.year - bd.year - ((today_date.month, today_date.day) < (bd.month, bd.day))
            except ValueError:
                pass
        user_physical = {
            "gender": gender_row.value if gender_row and gender_row.value else None,
            "weight_kg": float(weight_row.value) if weight_row and weight_row.value else None,
            "age": age,
        }

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
                "user_physical": user_physical,
                "program": current_program,
                "active_program": active,
                "is_draft": bool(current_program and current_program.status == "draft"),
                "just_approved": bool(approved),
                "calendar_connected": bool(config.ICS_CALENDAR_URL),
                "profile_equipment": _json_list(profile.equipment_access) if profile else [],
                "sessions": sessions,
                "strength_sessions": strength_sessions,
                "exercises_by_session": exercises_by_session,
                "session_ready": session_ready,
                "planned": planned,
                "exercise_catalog": catalog_for_ui(),
            },
        )


@app.post("/program/{program_id}/approve")
def approve_program(program_id: int):
    """Activate a reviewed program. Dates are still decided session by session."""
    with get_session() as session:
        program = session.get(TrainingProgram, program_id)
        if not program or program.status != "draft":
            raise HTTPException(status_code=404, detail="Program proposal not found")
        incomplete = [
            planned.name
            for planned in program_sessions_for(session, program_id)
            if planned.session_role == "coach_strength"
            and not session.query(SessionExercise).filter_by(program_session_id=planned.id).first()
        ]
        if incomplete:
            raise HTTPException(
                status_code=422,
                detail=f"Add at least one exercise to: {', '.join(incomplete)}.",
            )
        from coach.garmin_compiler import build_program_workout
        for planned in program_sessions_for(session, program_id):
            if planned.session_role != "coach_strength":
                continue
            try:
                build_program_workout(session, planned.id, require_active=False)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=f"{planned.name}: {exc}") from exc
        for existing in session.query(TrainingProgram).filter(TrainingProgram.active.is_(True)).all():
            existing.active = False
            existing.status = "archived"
            existing.updated_at = datetime.now()
        program.active = True
        program.status = "active"
        program.activated_at = datetime.now()
        program.updated_at = program.activated_at
        from coach.program_state import initialize_program_cursor
        initialize_program_cursor(session, program, activated_at=program.activated_at)
    # If this morning was deferred because no program was active—or an older
    # NO_ACTION brief was already sent—activation immediately produces the
    # authoritative recommendation or a clearly labelled correction.
    with get_session() as session:
        from coach.coach import generate_daily_suggestion
        generate_daily_suggestion(session)
    return RedirectResponse(url="/program?view=active&approved=1", status_code=303)


@app.post("/program/{program_id}/reset")
def reset_program_to_template(program_id: int):
    """Restore a curated program's editable sessions from its source template."""
    with get_session() as session:
        program = session.get(TrainingProgram, program_id)
        plan_keys = _json_list(program.goal_tags) if program else []
        plan_key = plan_keys[0] if plan_keys else ""
        template = PROGRAMS.get(plan_key)
        if not program or not template or program.source_type != "curated_archetype":
            raise HTTPException(status_code=404, detail="A curated template was not found for this program.")

        session_ids = [item.id for item in program_sessions_for(session, program.id)]
        if session_ids and session.query(PlannedSession).filter(PlannedSession.program_session_id.in_(session_ids)).first():
            raise HTTPException(status_code=422, detail="Remove scheduled workouts before resetting this program.")
        if session.query(ActivityProgramMatch).filter_by(program_id=program.id).first():
            raise HTTPException(
                status_code=422,
                detail="A program with matched completed sessions cannot be reset to its template.",
            )

        proposal = recommend_program(
            plan_key=plan_key,
            limitations="",
            session_duration_min=max(item["duration_min"] for item in template["sessions"]),
            history_summary="Program reset to the original curated template.",
        )
        _apply_recent_strength_weights(session, proposal)
        _replace_program_sessions(session, program, proposal["sessions"])
        program.name = proposal["name"]
        program.days_per_week = proposal["days_per_week"]
        program.rationale = proposal["rationale"]
        program.updated_at = datetime.now()
        target = f"/program?proposal={program.id}" if program.status == "draft" else "/program?view=active"
    return RedirectResponse(url=target, status_code=303)


@app.post("/program/{program_id}/sessions/{session_id}/reset")
def reset_program_session_to_template(program_id: int, session_id: int):
    """Restore one curated template day while retaining the rest of the program."""
    with get_session() as session:
        program = session.get(TrainingProgram, program_id)
        program_session = session.get(ProgramSession, session_id)
        plan_keys = _json_list(program.goal_tags) if program else []
        plan_key = plan_keys[0] if plan_keys else ""
        template = PROGRAMS.get(plan_key)
        if (
            not program
            or not template
            or program.source_type != "curated_archetype"
            or not program_session
            or program_session.program_id != program.id
            or program_session.is_custom
            or not 1 <= program_session.sequence_order <= len(template["sessions"])
        ):
            raise HTTPException(status_code=404, detail="A curated template day was not found for this session.")
        if session.query(PlannedSession).filter_by(program_session_id=session_id).first():
            raise HTTPException(status_code=422, detail="Remove this day's scheduled workout before resetting it.")
        if session.query(ActivityProgramMatch).filter_by(program_session_id=session_id).first():
            raise HTTPException(status_code=422, detail="A completed template day cannot be reset.")

        proposal = recommend_program(
            plan_key=plan_key,
            limitations="",
            session_duration_min=max(item["duration_min"] for item in template["sessions"]),
            history_summary="Program day reset to the original curated template.",
        )
        _apply_recent_strength_weights(session, proposal)
        template_day = proposal["sessions"][program_session.sequence_order - 1]
        _replace_session_exercises(session, program_session, template_day["exercises"])
        program.updated_at = datetime.now()
        target = f"/program?proposal={program.id}" if program.status == "draft" else "/program?view=active"
    return RedirectResponse(url=target, status_code=303)


@app.post("/api/program/{program_id}/sessions")
async def add_program_session(program_id: int, request: Request):
    """Add an independent strength session to an editable program."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    raw_name = str(body.get("name", "")) if isinstance(body, dict) else ""
    name = " ".join(raw_name.split())
    if not name:
        raise HTTPException(status_code=422, detail="Enter a session name.")
    if len(name) > 80:
        raise HTTPException(status_code=422, detail="Session names must be 80 characters or fewer.")
    with get_session() as db:
        program = db.get(TrainingProgram, program_id)
        if not program or program.status not in {"draft", "active"}:
            raise HTTPException(status_code=404, detail="Program not found")

        sessions = (
            db.query(ProgramSession)
            .filter_by(program_id=program_id, session_role="coach_strength")
            .order_by(ProgramSession.sequence_order.asc())
            .all()
        )
        if len(sessions) >= 12:
            raise HTTPException(status_code=422, detail="A program can have at most 12 sessions.")

        existing_names = {session.name.casefold() for session in sessions}
        if name.casefold() in existing_names:
            raise HTTPException(status_code=422, detail="That session name is already in use.")

        added = ProgramSession(
            program_id=program_id,
            name=name,
            sport_type="strength_training",
            sequence_order=max((session.sequence_order for session in sessions), default=0) + 1,
            focus_tags='["strength"]',
            duration_min=60,
            notes="",
            session_role="coach_strength",
            target_frequency=1,
            is_addon=False,
            is_custom=True,
        )
        db.add(added)
        db.flush()
        program.days_per_week = len(sessions) + 1
        program.updated_at = datetime.now()
        return JSONResponse({"id": added.id, "name": added.name})


@app.delete("/api/program/{program_id}/sessions/{session_id}")
def delete_custom_program_session(program_id: int, session_id: int):
    """Remove an unscheduled athlete-added session from a program."""
    with get_session() as db:
        ps = db.get(ProgramSession, session_id)
        if not ps or ps.program_id != program_id:
            raise HTTPException(status_code=404, detail="Additional session not found")
        plan_keys = _json_list(ps.program.goal_tags) if ps.program else []
        template = PROGRAMS.get(plan_keys[0]) if plan_keys else None
        is_legacy_custom = bool(template and ps.sequence_order > len(template["sessions"]))
        if not ps.is_custom and not is_legacy_custom:
            raise HTTPException(status_code=404, detail="Additional session not found")
        if db.query(PlannedSession).filter_by(program_session_id=session_id, status="approved").first():
            raise HTTPException(status_code=422, detail="This session is already scheduled. Remove its scheduled workout first.")
        program = ps.program
        db.delete(ps)
        db.flush()
        if program:
            program.days_per_week = db.query(ProgramSession).filter_by(
                program_id=program_id, session_role="coach_strength"
            ).count()
            program.updated_at = datetime.now()
    return JSONResponse({"ok": True})


@app.post("/api/session/{session_id}/exercises")
async def save_session_exercises(session_id: int, request: Request):
    """Full-replace the exercise list for a program session."""
    import json as _json
    body = await request.body()
    try:
        rows = _json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    if not isinstance(rows, list) or len(rows) > 50 or not all(isinstance(row, dict) for row in rows):
        raise HTTPException(status_code=422, detail="Exercises must be a list of at most 50 rows.")

    with get_session() as db:
        ps = db.get(ProgramSession, session_id)
        if not ps:
            raise HTTPException(status_code=404, detail="Session not found")
        if ps.session_role != "coach_strength":
            raise HTTPException(status_code=400, detail="Only coach strength sessions have exercise templates")

        existing_generic = {
            ex.exercise_name for ex in db.query(SessionExercise).filter_by(program_session_id=session_id)
            if ex.is_generic
        }
        validated = []
        for row in rows:
            name = str(row.get("exercise_name", "")).strip()
            meta = exercise_metadata(str(row.get("exercise_key") or name))
            is_generic = not meta and name in existing_generic
            if not meta and not is_generic:
                raise HTTPException(status_code=422, detail=f"Choose {name or 'each exercise'} from the Garmin exercise list.")
            pattern = (meta or {}).get("movement_pattern", str(row.get("movement_pattern") or "other"))
            weight = float(row["weight_kg"]) if row.get("weight_kg") not in (None, "") else None
            reps = int(row["reps"]) if row.get("reps") not in (None, "") else None
            duration = int(row["duration_seconds"]) if row.get("duration_seconds") not in (None, "") else None
            sets = int(row["sets"]) if row.get("sets") not in (None, "") else None
            if sets is not None and not 1 <= sets <= 20:
                raise HTTPException(status_code=422, detail="Sets must be between 1 and 20.")
            if reps is not None and not 1 <= reps <= 100:
                raise HTTPException(status_code=422, detail="Reps must be between 1 and 100.")
            if duration is not None and not 1 <= duration <= 3600:
                raise HTTPException(status_code=422, detail="Time must be between 1 and 3600 seconds.")
            if weight is not None and not 0 <= weight <= 500:
                raise HTTPException(status_code=422, detail="Weight must be between 0 and 500 kg.")
            defaults = warmup_defaults(name, meta, reps, duration, weight)
            warmup_enabled = defaults["warmup_enabled"] if "warmup_enabled" not in row else bool(row["warmup_enabled"])
            if warmup_enabled and reps is None and duration is None:
                raise HTTPException(status_code=422, detail="Set a rep or time target before adding a warm-up set.")
            warmup_target_type = str(row.get("warmup_target_type") or ("time" if duration is not None else "reps"))
            if warmup_target_type not in {"reps", "time"}:
                raise HTTPException(status_code=422, detail="Warm-up target must be reps or time.")
            default_warmup_reps = 8 if warmup_target_type == "reps" else None
            default_warmup_duration = max(1, round((duration or 30) * 0.5)) if warmup_target_type == "time" else None
            default_warmup_weight = defaults["warmup_weight_kg"] if defaults["warmup_weight_kg"] is not None else (round(weight * 0.5, 1) if weight else None)
            warmup_reps = int(row["warmup_reps"]) if warmup_enabled and warmup_target_type == "reps" and row.get("warmup_reps") not in (None, "") else default_warmup_reps
            warmup_duration = int(row["warmup_duration_seconds"]) if warmup_enabled and warmup_target_type == "time" and row.get("warmup_duration_seconds") not in (None, "") else default_warmup_duration
            warmup_weight = float(row["warmup_weight_kg"]) if warmup_enabled and warmup_target_type == "reps" and row.get("warmup_weight_kg") not in (None, "") else None
            if warmup_reps is not None and not 1 <= warmup_reps <= 100:
                raise HTTPException(status_code=422, detail="Warm-up reps must be between 1 and 100.")
            if warmup_duration is not None and not 1 <= warmup_duration <= 3600:
                raise HTTPException(status_code=422, detail="Warm-up time must be between 1 and 3600 seconds.")
            if warmup_weight is not None and not 0 <= warmup_weight <= 500:
                raise HTTPException(status_code=422, detail="Warm-up weight must be between 0 and 500 kg.")
            validated.append((row, name, meta, is_generic, pattern, warmup_enabled, warmup_reps, warmup_duration, warmup_weight, weight, reps, duration))

        db.query(SessionExercise).filter_by(program_session_id=session_id).delete()

        for i, (row, name, meta, is_generic, pattern, warmup_enabled, warmup_reps, warmup_duration, warmup_weight, weight, reps, duration) in enumerate(validated):
            ex = SessionExercise(
                program_session_id=session_id,
                exercise_name=(meta or {}).get("label", name),
                exercise_key=(meta or {}).get("key", exercise_key(name)),
                garmin_category=(meta or {}).get("category"),
                garmin_name=(meta or {}).get("garmin_name"),
                movement_pattern=pattern,
                is_generic=is_generic,
                sets=int(row["sets"]) if row.get("sets") not in (None, "") else None,
                reps=reps,
                duration_seconds=duration,
                weight_kg=weight,
                rest_seconds=max(0, min(600, int(row.get("rest_seconds") or 60))),
                warmup_enabled=warmup_enabled,
                warmup_reps=warmup_reps if warmup_enabled else None,
                warmup_duration_seconds=warmup_duration if warmup_enabled else None,
                warmup_weight_kg=warmup_weight if warmup_enabled else None,
                order_index=i,
                notes=str(row.get("notes", "")),
            )
            db.add(ex)

        if not validated and ps.program and ps.program.active:
            raise HTTPException(
                status_code=422,
                detail="An active Garmin session needs at least one exercise.",
            )
        if validated:
            from coach.garmin_compiler import build_program_workout
            db.flush()
            try:
                build_program_workout(db, ps.id, require_active=False)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc

    return JSONResponse({"ok": True})


@app.delete("/api/session/{session_id}/exercises/{exercise_id}")
def delete_session_exercise(session_id: int, exercise_id: int):
    """Delete a single exercise row from a session."""
    with get_session() as db:
        ex = db.query(SessionExercise).filter_by(
            id=exercise_id, program_session_id=session_id
        ).first()
        if not ex:
            raise HTTPException(status_code=404, detail="Exercise not found")
        db.delete(ex)
    return JSONResponse({"ok": True})


@app.get("/api/session/{session_id}/suggest-from-history")
def suggest_from_history(session_id: int):
    """Return exercise suggestions for a session derived from activity history.

    Algorithm:
    1. Match strength activities to the session name (exact after cleaning, then keyword).
    2. Take the last 5 matching activities.
    3. For each exercise_name appearing in ≥ 50% of those activities, aggregate:
       - sets  : modal count of working sets across sessions
       - reps  : median reps from the most recent session's working sets
       - weight: most recent non-zero working-set weight
       - warmup: sets where weight < 75 % of modal working weight (first in order)
    4. Return sorted by appearance frequency (most consistent exercise first).
    """
    import re as _re
    import statistics

    def _clean(name: str | None) -> str:
        if not name:
            return ""
        s = _re.sub(r"^[^\w\s]+\s*", "", name)       # strip leading emoji
        s = _re.sub(r"\s*@\s*\d{1,2}:\d{2}\s*$", "", s)  # strip @ HH:MM
        s = _re.sub(r"^[A-Z]\s+-\s+", "", s)           # strip "A - " letter prefix
        return s.strip()

    def _keywords(name: str) -> set[str]:
        """Split 'Chest & Biceps' → {'chest', 'biceps'} ignoring short words."""
        parts = _re.split(r"[\s&,/]+", name.lower())
        return {p for p in parts if len(p) > 2}

    def _detect_warmup_ids(sets_ordered: list) -> set[int]:
        """Return set of ExerciseSet.id values that are warm-up sets.

        A set is a warm-up when:
        - It has a valid weight AND
        - Its weight < 75 % of the modal (working) weight AND
        - It appears before the first working-weight set (by set_index order).
        """
        weighted = [s for s in sets_ordered if s.weight_kg and s.weight_kg > 0]
        if len(weighted) < 2:
            return set()
        weights = [s.weight_kg for s in weighted]
        # Modal working weight
        weight_counts: dict[float, int] = {}
        for w in weights:
            weight_counts[w] = weight_counts.get(w, 0) + 1
        working_weight = max(weight_counts, key=weight_counts.__getitem__)
        threshold = 0.75 * working_weight

        warmup_ids: set[int] = set()
        found_working = False
        for s in sets_ordered:
            if not s.weight_kg or s.weight_kg <= 0:
                continue
            if not found_working:
                if s.weight_kg >= threshold:
                    found_working = True
                else:
                    warmup_ids.add(s.id)
        return warmup_ids

    with get_session() as db:
        ps = db.get(ProgramSession, session_id)
        if not ps:
            raise HTTPException(status_code=404, detail="Session not found")

        cleaned_session = _clean(ps.name)
        kw = _keywords(cleaned_session)

        # --- Find matching strength activities ---
        candidates = (
            db.query(Activity)
            .filter(Activity.activity_type.ilike("%strength%"))
            .order_by(Activity.start_time.desc())
            .limit(200)
            .all()
        )
        matched: list[Activity] = []
        for act in candidates:
            act_name = _clean(act.name)
            # Exact match first, then any keyword overlap
            if act_name.lower() == cleaned_session.lower():
                matched.append(act)
            elif kw and kw.intersection(_keywords(act_name)):
                matched.append(act)
            if len(matched) >= 5:
                break

        if not matched:
            return JSONResponse([])

        # --- Aggregate exercises across matched sessions ---
        # Structure: {exercise_name: [{sets, reps, weight, is_warmup, activity_date}]}
        from collections import defaultdict
        appearances: dict[str, list[dict]] = defaultdict(list)
        # Track order by first seen (most recent first)
        exercise_order: dict[str, int] = {}
        order_counter = 0

        for act in matched:
            act_sets = (
                db.query(ExerciseSet)
                .filter_by(activity_id=act.id, set_type="ACTIVE")
                .order_by(ExerciseSet.set_index)
                .all()
            )
            # Group by exercise_name (fall back to exercise_category)
            by_ex: dict[str, list] = defaultdict(list)
            for s in act_sets:
                key = s.exercise_name or s.exercise_category
                if not key or key == "UNKNOWN":
                    continue
                by_ex[key].append(s)

            for ex_name, ex_sets in by_ex.items():
                warmup_ids = _detect_warmup_ids(ex_sets)
                working = [s for s in ex_sets if s.id not in warmup_ids]
                warmup  = [s for s in ex_sets if s.id in warmup_ids]

                if ex_name not in exercise_order:
                    exercise_order[ex_name] = order_counter
                    order_counter += 1

                appearances[ex_name].append({
                    "working_sets": len(working),
                    "working_reps": [s.reps for s in working if s.reps],
                    "working_weights": [s.weight_kg for s in working if s.weight_kg],
                    "warmup_sets": len(warmup),
                    "warmup_reps": [s.reps for s in warmup if s.reps],
                    "warmup_weights": [s.weight_kg for s in warmup if s.weight_kg],
                    "date": act.start_time,
                })

        n_sessions = len(matched)
        min_appearances = max(1, (n_sessions + 1) // 2)  # ≥ 50 %

        suggestions = []
        for ex_name, records in appearances.items():
            if len(records) < min_appearances:
                continue

            # Most recent record first (matched list is newest-first)
            recent = records[0]

            # Modal working set count
            set_counts = [r["working_sets"] for r in records if r["working_sets"] > 0]
            modal_sets = max(set(set_counts), key=set_counts.count) if set_counts else 1

            # Median reps from most recent session
            reps_recent = recent["working_reps"]
            median_reps = int(statistics.median(reps_recent)) if reps_recent else None

            # Most recent working weight
            wts = recent["working_weights"]
            recent_weight = wts[-1] if wts else None  # last (heaviest typical) set

            # Warm-up from most recent session
            wu_reps_list = recent["warmup_reps"]
            wu_wts_list  = recent["warmup_weights"]
            warmup_reps   = int(statistics.median(wu_reps_list)) if wu_reps_list else None
            warmup_weight = wu_wts_list[0] if wu_wts_list else None  # first (lightest) wu set

            suggestions.append({
                "exercise_name": ex_name.replace("_", " ").title(),
                "sets": modal_sets,
                "reps": median_reps,
                "weight_kg": recent_weight,
                "warmup_reps": warmup_reps,
                "warmup_weight_kg": warmup_weight,
                "frequency": len(records),
            })

        # Sort: most consistent exercise first, then by first-seen order
        suggestions.sort(key=lambda x: (-x["frequency"], exercise_order.get(
            x["exercise_name"].upper().replace(" ", "_"), 999
        )))

        return JSONResponse(suggestions)


@app.patch("/api/session/{session_id}/addon")
async def toggle_addon(session_id: int, request: Request):
    """Toggle the is_addon flag for a program session."""
    import json as _json
    body = await request.body()
    try:
        payload = _json.loads(body)
        is_addon = bool(payload.get("is_addon", False))
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    with get_session() as db:
        ps = db.get(ProgramSession, session_id)
        if not ps:
            raise HTTPException(status_code=404, detail="Session not found")
        ps.is_addon = is_addon

    return JSONResponse({"ok": True, "is_addon": is_addon})


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
        
        metrics = session.query(DailyHealth).filter(
            DailyHealth.day >= start_date,
            DailyHealth.day <= end_date
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
                r_val = metric_map.get(d).training_readiness if metric_map.get(d) else None
                color = None
                if r_val is not None:
                    color = "green" if r_val >= 50 else "yellow" if r_val >= 25 else "red"
                
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

    from time_utils import get_local_tz
    local_tz = get_local_tz()
    tz_name = getattr(local_tz, "zone", str(local_tz))

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
        
    data = {}
    tenant_token = None
    authorized_chat_id = config.TELEGRAM_CHAT_ID
    try:
        data = await request.json()
        callback = data.get("callback_query") or {}
        message = data.get("message") or callback.get("message") or {}
        chat = message.get("chat") or {}
        incoming_chat_id = chat.get("id")
        incoming_chat_type = chat.get("type")
        incoming_text = message.get("text") if not callback else None

        if config.MULTI_USER_ENABLED:
            from notify.telegram import send_link_message
            from sync.scheduler import refresh_user_jobs
            from telegram_link import consume_link_code, resolve_chat_tenant, unlink_user

            if incoming_chat_type != "private" or incoming_chat_id is None:
                return {"status": "ok"}
            parts = (incoming_text or "").strip().split(maxsplit=1)
            command = parts[0].split("@", 1)[0].casefold() if parts else ""
            link_code = None
            if command == "/link" and len(parts) == 2:
                link_code = parts[1]
            elif command == "/start" and len(parts) == 2 and parts[1].startswith("link_"):
                link_code = parts[1].removeprefix("link_")
            if command in {"/link", "/start"} and link_code is None:
                send_link_message("Generate a link command from GarminCoach Account settings.", str(incoming_chat_id))
                return {"status": "ok"}
            if link_code is not None:
                try:
                    identity = consume_link_code(link_code, str(incoming_chat_id))
                except ValueError as exc:
                    send_link_message(str(exc), str(incoming_chat_id))
                    return {"status": "ok"}
                refresh_user_jobs(identity.user_id)
                send_link_message("Telegram is linked to your GarminCoach account.", str(incoming_chat_id))
                return {"status": "ok"}

            identity = resolve_chat_tenant(str(incoming_chat_id))
            if identity is None:
                if incoming_text:
                    send_link_message("This chat is not linked. Generate a link command in GarminCoach Account settings.", str(incoming_chat_id))
                return {"status": "ok"}
            authorized_chat_id = str(incoming_chat_id)
            tenant_token = bind_tenant(identity)
            if command == "/unlink":
                unlink_user(identity.user_id)
                refresh_user_jobs(identity.user_id)
                reset_tenant(tenant_token)
                tenant_token = None
                send_link_message("Telegram was unlinked from GarminCoach.", str(incoming_chat_id))
                return {"status": "ok"}

        # If it's a callback query from an inline button
        if "callback_query" in data:
            callback = data["callback_query"]
            callback_id = callback.get("id")
            chat_id = callback.get("message", {}).get("chat", {}).get("id")
            chat_type = callback.get("message", {}).get("chat", {}).get("type")
            message_id = callback.get("message", {}).get("message_id")
            callback_data = callback.get("data", "")
            
            from notify import telegram
            telegram.answer_callback_query(callback_id)
            
            if str(chat_id) == authorized_chat_id and chat_type == "private":
                if callback_data.startswith("flow:"):
                    from coach.intent_router import handle_flow_callback
                    with get_session() as db:
                        turn = handle_flow_callback(db, callback_data)
                        text = turn.text
                        markup = turn.reply_markup
                        if turn.interactions:
                            from coach.interactions import reply_markup
                            markup = reply_markup(turn.interactions)
                    telegram.edit_message_text(
                        text, chat_id=str(chat_id), message_id=message_id, reply_markup=markup,
                    )

                elif callback_data in {"date_choice_today", "date_choice_tomorrow"}:
                    telegram.edit_message_text(
                        "This old date choice expired. Start the scheduling flow again.",
                        chat_id=str(chat_id), message_id=message_id,
                    )

                elif callback_data.startswith("catalog_details_metric_"):
                    from coach.intent_router import metric_detail_response
                    topic = callback_data.removeprefix("catalog_details_metric_")
                    with get_session() as db:
                        text = metric_detail_response(db, topic)
                    telegram.edit_message_text(text, chat_id=str(chat_id), message_id=message_id)

                elif callback_data.startswith("decision_different_time_"):
                    from coach.interactions import request_different_time
                    interaction_id = callback_data.removeprefix("decision_different_time_")
                    with get_session() as db:
                        text = request_different_time(db, interaction_id)
                        from coach.intent_router import dialogue_reply_markup
                        markup = dialogue_reply_markup(db)
                    telegram.edit_message_text(text, chat_id=str(chat_id), message_id=message_id, reply_markup=markup)

                elif callback_data.startswith("decision_action_"):
                    from coach.interactions import apply_interaction
                    interaction_id = callback_data.removeprefix("decision_action_")
                    with get_session() as db:
                        status, text = apply_interaction(db, interaction_id)
                        markup = None
                        if status == "awaiting_input":
                            from coach.intent_router import dialogue_reply_markup
                            markup = dialogue_reply_markup(db)
                    telegram.edit_message_text(text, chat_id=str(chat_id), message_id=message_id, reply_markup=markup)

                elif callback_data.startswith("decision_cancel_"):
                    from coach.interactions import reject_interaction
                    interaction_id = callback_data.removeprefix("decision_cancel_")
                    with get_session() as db:
                        text = reject_interaction(db, interaction_id)
                    telegram.edit_message_text(text, chat_id=str(chat_id), message_id=message_id)

                elif callback_data.startswith("morning_synced_"):
                    from notify.morning import start_priority_fetch
                    started = start_priority_fetch()
                    text = "Fetching the new Garmin data. The briefing will follow automatically." if started else "A fetch is already running or today's briefing is complete."
                    telegram.edit_message_text(text, chat_id=str(chat_id), message_id=message_id)

                elif callback_data.startswith("morning_anyway_"):
                    from notify.morning import answer_anyway
                    day_key = callback_data.rsplit("_", 1)[-1]
                    accepted = answer_anyway(day_key)
                    text = "Preparing the briefing without the missing data." if accepted else "This choice is no longer current."
                    telegram.edit_message_text(text, chat_id=str(chat_id), message_id=message_id)

                elif callback_data.startswith(("approve_workout_", "reject_workout_", "reschedule_workout_")):
                    telegram.edit_message_text(
                        "This legacy action expired. Ask for a current proposal.",
                        chat_id=str(chat_id), message_id=message_id,
                    )

                elif callback_data.startswith("approve_workout_"):
                    msg_id = int(callback_data.split("_")[-1])
                    with get_session() as db:
                        from db import CoachMessage
                        msg = db.get(CoachMessage, msg_id)
                        if msg and msg.pending_action_json:
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
        chat_type = message.get("chat", {}).get("type")
        text = message.get("text")
        
        # We only care about text messages from the authorized chat
        if text and str(chat_id) == authorized_chat_id and chat_type == "private":
            from coach.coach import handle_chat
            from notify import telegram
            
            telegram.send_chat_action(str(chat_id), "typing")
            
            # Pass to AI Coach
            with get_session() as db:
                response_text, asst_msg = handle_chat(db, text)
                reply_markup = None
                interaction_ids = []
                if asst_msg.pending_action_json:
                    pending = json.loads(asst_msg.pending_action_json)
                    interaction_ids = pending.get("interaction_ids", [])
                    reply_markup = pending.get("reply_markup")
                    if interaction_ids:
                        from coach.interactions import reply_markup_for_ids
                        reply_markup = reply_markup_for_ids(db, interaction_ids)
                    if interaction_ids and not reply_markup:
                        from coach.interactions import mark_delivery_failed
                        mark_delivery_failed(db, interaction_ids, "markup_unavailable")
                        asst_msg.pending_action_json = None
                        asst_msg.content = "I couldn't create safe confirmation controls. No action was taken."
                        response_text = asst_msg.content
                else:
                    # Refresh Telegram's persistent reply keyboard so menu
                    # changes replace older client-cached button layouts.
                    from coach.intent_router import _menu_markup
                    reply_markup = _menu_markup()
            
            # Send response back to Telegram
            delivered = telegram.send_message(response_text, chat_id=str(chat_id), reply_markup=reply_markup)
            if interaction_ids and not delivered:
                with get_session() as db:
                    from coach.interactions import mark_delivery_failed
                    mark_delivery_failed(db, interaction_ids, "telegram_send_failed")
            
    except Exception as e:
        import logging
        logging.error(f"Telegram webhook failed: {e}")
        try:
            from notify import telegram
            chat_id = (data.get("message") or {}).get("chat", {}).get("id")
            if chat_id and str(chat_id) == authorized_chat_id:
                detail = "An error occurred while processing your request. No operation was applied. Please try again later."
                if not config.MULTI_USER_ENABLED:
                    detail = f"An error occurred while processing your request:\n`{str(e)}`\n\nNo operation was applied. Please try again later."
                telegram.send_message(f"*Coach error:*\n{detail}", chat_id=str(chat_id))
        except Exception:
            pass
    finally:
        if tenant_token is not None:
            reset_tenant(tenant_token)
        
    return {"status": "ok"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host=config.HOST, port=config.PORT, reload=False)
