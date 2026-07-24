import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

from sqlalchemy.orm import Session

from coach.onboarding import active_program, program_sessions_for
from coach.program_state import program_state_facts
from db import AthleteProfile, Goal


_DAY_NAMES = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}
_CLOCK_PATTERN = r"\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?)?"

# Default workout availability (Sunday-Thursday: 18:00-20:00, Friday: 11:00-15:00, Saturday: 11:00-20:00)
DEFAULT_WEEKLY_AVAILABILITY = {
    6: {"off": False, "start": "18:00", "end": "20:00"},  # Sunday
    0: {"off": False, "start": "18:00", "end": "20:00"},  # Monday
    1: {"off": False, "start": "18:00", "end": "20:00"},  # Tuesday
    2: {"off": False, "start": "18:00", "end": "20:00"},  # Wednesday
    3: {"off": False, "start": "18:00", "end": "20:00"},  # Thursday
    4: {"off": False, "start": "11:00", "end": "15:00"},  # Friday
    5: {"off": False, "start": "11:00", "end": "20:00"},  # Saturday
}


def parse_weekly_availability(raw: str | None) -> dict[int, dict]:
    """Parse JSON weekly availability into a dict keyed by int weekday (0=Mon..6=Sun)."""
    if not raw or not raw.strip():
        return {k: dict(v) for k, v in DEFAULT_WEEKLY_AVAILABILITY.items()}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            parsed = {}
            for day_idx in range(7):
                str_key = str(day_idx)
                if str_key in data and isinstance(data[str_key], dict):
                    day_cfg = data[str_key]
                    parsed[day_idx] = {
                        "off": bool(day_cfg.get("off", False)),
                        "start": str(day_cfg.get("start", DEFAULT_WEEKLY_AVAILABILITY[day_idx]["start"])),
                        "end": str(day_cfg.get("end", DEFAULT_WEEKLY_AVAILABILITY[day_idx]["end"])),
                    }
                elif day_idx in data and isinstance(data[day_idx], dict):
                    day_cfg = data[day_idx]
                    parsed[day_idx] = {
                        "off": bool(day_cfg.get("off", False)),
                        "start": str(day_cfg.get("start", DEFAULT_WEEKLY_AVAILABILITY[day_idx]["start"])),
                        "end": str(day_cfg.get("end", DEFAULT_WEEKLY_AVAILABILITY[day_idx]["end"])),
                    }
                else:
                    parsed[day_idx] = dict(DEFAULT_WEEKLY_AVAILABILITY[day_idx])
            return parsed
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    # Legacy regex fallback for unmigrated plain text constraints
    lower_by_day, global_lower = _bound_rules(raw, "before")
    upper_by_day, global_upper = _bound_rules(raw, "after")
    result = {}
    for day_idx in range(7):
        default_cfg = DEFAULT_WEEKLY_AVAILABILITY[day_idx]
        lower_t = lower_by_day.get(day_idx, global_lower)
        upper_t = upper_by_day.get(day_idx, global_upper)
        start_str = lower_t.strftime("%H:%M") if lower_t else default_cfg["start"]
        end_str = upper_t.strftime("%H:%M") if upper_t else default_cfg["end"]
        result[day_idx] = {"off": False, "start": start_str, "end": end_str}
    return result


@dataclass(frozen=True)
class TimeSuggestion:
    day: date
    start: time
    duration_min: int
    session_name: str
    program_session_id: int

    def render(self) -> str:
        return f"{self.session_name} — {self.day:%A} at {self.start:%H:%M}."


def is_timing_question(user_text: str) -> bool:
    text = " ".join(user_text.lower().split())
    if any(phrase in text for phrase in (
        "when should i do it", "when should i work out", "when should i workout",
        "when can i work out", "when can i workout", "what time should i",
        "best time for", "available time", "time window",
    )):
        return True
    day_reference = bool(re.search(
        r"\b(today|tomorrow|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
        text,
    ))
    workout_reference = any(phrase in text for phrase in (
        "do it", "work out", "workout", "train", "session",
    ))
    question_reference = any(phrase in text for phrase in (
        "can i", "could i", "should i", "will", "would", "what about", "how about", "is there",
    ))
    return day_reference and workout_reference and question_reference


def is_schedule_request(user_text: str) -> bool:
    """Recognize requests that intend to create a scheduled workout."""
    text = " ".join(user_text.lower().split())
    schedule_verb = bool(re.search(r"\b(schedule|book)\b", text))
    workout_reference = any(phrase in text for phrase in (
        "do it", "work out", "workout", "session",
    ))
    return schedule_verb and workout_reference


def requested_day(user_text: str, today: date) -> date | None:
    """Resolve an explicitly requested relative day or weekday."""
    text = " ".join(user_text.lower().split())
    if re.search(r"\b(today|tonight|this evening)\b", text):
        return today
    if re.search(r"\btomorrow\b", text):
        return today + timedelta(days=1)
    iso_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    if iso_match:
        try:
            return date.fromisoformat(iso_match.group(1))
        except ValueError:
            return None
    for name, weekday in _DAY_NAMES.items():
        if len(name) <= 3:
            continue
        if re.search(rf"\b{name}s?\b", text):
            days_ahead = (weekday - today.weekday()) % 7
            return today + timedelta(days=days_ahead)
    return None


def _parse_clock(value: str) -> time | None:
    match = re.fullmatch(
        r"\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*",
        value,
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    hour, minute, meridiem = int(match.group(1)), int(match.group(2) or 0), match.group(3)
    if minute > 59:
        return None
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        hour %= 12
        if meridiem.lower().startswith("p"):
            hour += 12
    elif hour > 23:
        return None
    return time(hour, minute)


def _day_indices(value: str) -> set[int]:
    names = re.findall(r"[A-Za-z]+", value.lower())
    indices = [_DAY_NAMES[name.rstrip("s")] for name in names if name.rstrip("s") in _DAY_NAMES]
    if not indices:
        return set()
    if "-" in value or "–" in value or "—" in value:
        start, end = indices[0], indices[-1]
        result = {start}
        while start != end:
            start = (start + 1) % 7
            result.add(start)
        return result
    return set(indices)


def _bound_rules(constraints: str, direction: str) -> tuple[dict[int, time], time | None]:
    """Extract day-specific and global before/after rules from plain-language constraints."""
    day_rules: dict[int, time] = {}
    global_rule: time | None = None
    pattern = re.compile(
        rf"(?:(?:on)\s+)?(?P<days>(?:(?:mon|tues|wednes|thurs|fri|satur|sun)\w*\s*(?:[-–—,]\s*)?)+)"
        rf"no\s+workouts?\s+{direction}\s+(?P<clock>{_CLOCK_PATTERN})",
        flags=re.IGNORECASE,
    )
    matched_spans: list[tuple[int, int]] = []
    for match in pattern.finditer(constraints):
        parsed = _parse_clock(match.group("clock"))
        if not parsed:
            continue
        for day_index in _day_indices(match.group("days")):
            day_rules[day_index] = parsed
        matched_spans.append(match.span())

    generic = re.compile(rf"no\s+workouts?\s+{direction}\s+(?P<clock>{_CLOCK_PATTERN})", re.IGNORECASE)
    for match in generic.finditer(constraints):
        if any(start <= match.start() < end for start, end in matched_spans):
            continue
        parsed = _parse_clock(match.group("clock"))
        if parsed:
            global_rule = parsed
    return day_rules, global_rule


def _round_up_to_quarter(value: datetime) -> datetime:
    remainder = value.minute % 15
    if remainder or value.second or value.microsecond:
        value += timedelta(minutes=15 - remainder)
    return value.replace(second=0, microsecond=0)


def _session_details(session: Session, today: date) -> tuple[int, str, int, date] | None:
    program = active_program(session)
    if not program:
        return None
    state = program_state_facts(session, program, on_date=today)
    sessions = program_sessions_for(session, program.id)
    by_id = {item.id: item for item in sessions}
    next_session = by_id.get(state["next_session_id"]) if state else (sessions[0] if sessions else None)
    if not next_session:
        return None
    earliest = today
    if state and state.get("earliest_recommended_date"):
        earliest = date.fromisoformat(state["earliest_recommended_date"])
    return next_session.id, next_session.name, next_session.duration_min or 60, earliest


def _event_bounds(item: dict) -> tuple[datetime, datetime] | None:
    try:
        start = datetime.strptime(item["start"], "%Y-%m-%d %H:%M")
        end_time = datetime.strptime(item["end"], "%H:%M").time()
    except (KeyError, TypeError, ValueError):
        return None
    end = datetime.combine(start.date(), end_time)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def available_start_times(
    session: Session, *, now: datetime, schedule: list[dict], target_day: date,
    duration_min: int, limit: int = 3,
) -> list[time]:
    """Return valid quarter-hour starts for one day under hard constraints.

    This is also the typed date/time dialogue source: the chat layer may only
    accept a replacement time that appears in this deterministic result.
    """
    goal = session.get(Goal, 1)
    profile = session.get(AthleteProfile, 1) if session else None
    raw_constraints = (goal.custom_input if goal and goal.custom_input else (profile.availability if profile else "")) or ""

    weekly = parse_weekly_availability(raw_constraints)
    day_cfg = weekly.get(target_day.weekday(), DEFAULT_WEEKLY_AVAILABILITY[target_day.weekday()])

    if day_cfg.get("off"):
        return []

    opening_time = _parse_clock(day_cfg.get("start", "18:00")) or time(18, 0)
    closing_time = _parse_clock(day_cfg.get("end", "20:00")) or time(20, 0)

    candidate = datetime.combine(target_day, opening_time)
    if target_day == now.date():
        candidate = max(candidate, _round_up_to_quarter(now))
    end_limit = datetime.combine(target_day, closing_time)
    events = [bounds for item in schedule if (bounds := _event_bounds(item))]
    starts: list[time] = []
    while candidate + timedelta(minutes=duration_min) <= end_limit and len(starts) < limit:
        blocked = any(
            candidate < event_end and event_start < candidate + timedelta(minutes=duration_min)
            for event_start, event_end in events
        )
        if not blocked:
            starts.append(candidate.time())
        candidate += timedelta(minutes=15)
    return starts


def next_available_time(
    session: Session, *, now: datetime, schedule: list[dict], max_days: int = 7,
    start_day: date | None = None, preferred_time: time | None = None,
) -> TimeSuggestion | None:
    """Return the first valid full session slot; never delegate time arithmetic to the LLM."""
    details = _session_details(session, now.date())
    if not details:
        return None
    program_session_id, session_name, duration_min, earliest_day = details
    search_start = start_day or now.date()
    for offset in range(max_days):
        candidate_day = search_start + timedelta(days=offset)
        if candidate_day < earliest_day:
            continue
        starts = available_start_times(
            session, now=now, schedule=schedule, target_day=candidate_day,
            duration_min=duration_min, limit=96,
        )
        chosen = preferred_time if preferred_time in starts else (starts[0] if starts and not preferred_time else None)
        if chosen is not None:
            return TimeSuggestion(
                candidate_day, chosen, duration_min, session_name, program_session_id
            )
    return None
