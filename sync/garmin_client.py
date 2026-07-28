"""Garmin Connect auth + data fetchers.

Auth strategy (critical for avoiding rate-limit lockouts):
  - Log in ONCE, persist the OAuth token to GARMIN_TOKEN_STORE.
  - On subsequent runs, resume from the cached token (no re-login).
  - MFA is handled interactively via a prompt callback on first login only.

All fetchers return plain dicts/lists straight from the library; parsing into
the DB schema happens in sync/sync_service.py.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

import config
from sync.endpoint_telemetry import instrument_read


def _readiness_score(snapshot: dict[str, Any]) -> int | None:
    value = snapshot.get("trainingReadiness")
    if value is None:
        value = snapshot.get("value")
    if value is None:
        value = snapshot.get("score")
    if isinstance(value, bool):
        return None
    try:
        score = int(value)
    except (TypeError, ValueError):
        return None
    if score < 1 or score > 100:
        return None
    return score


def _readiness_date(snapshot: dict[str, Any]) -> date | None:
    raw_date = snapshot.get("calendarDate")
    if isinstance(raw_date, str):
        try:
            return date.fromisoformat(raw_date)
        except ValueError:
            return None

    raw_timestamp = snapshot.get("timestampLocal")
    if not isinstance(raw_timestamp, str):
        return None
    try:
        return datetime.fromisoformat(raw_timestamp.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _readiness_timestamp(snapshot: dict[str, Any]) -> datetime | None:
    for key in ("timestamp", "timestampLocal"):
        raw = snapshot.get(key)
        if not isinstance(raw, str):
            continue
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is not None:
            parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
        return parsed
    return None


def _normalized_readiness(
    snapshot: dict[str, Any],
    target_date: date,
) -> dict[str, Any] | None:
    score = _readiness_score(snapshot)
    if score is None:
        return None

    snapshot_date = _readiness_date(snapshot)
    if snapshot_date is not None and snapshot_date != target_date:
        return None
    if "calendarDate" in snapshot and snapshot_date is None:
        return None

    normalized = dict(snapshot)
    normalized["calendarDate"] = target_date.isoformat()
    normalized["trainingReadiness"] = score
    return normalized


def normalize_training_readiness(
    response: object,
    target_date: date,
) -> dict[str, Any] | None:
    """Normalize Garmin Training Readiness without interpreting its score.

    Legacy library versions returned one dictionary. GarminConnect 0.3.7
    returns timestamped snapshots. List entries must belong to the target local
    date and carry a usable timestamp; the latest valid snapshot wins.
    """
    if isinstance(response, dict):
        return _normalized_readiness(response, target_date)
    if not isinstance(response, list):
        return None

    candidates: list[tuple[datetime, dict[str, Any]]] = []
    for entry in response:
        if not isinstance(entry, dict):
            continue
        normalized = _normalized_readiness(entry, target_date)
        if normalized is None or _readiness_date(entry) != target_date:
            continue
        timestamp = _readiness_timestamp(entry)
        if timestamp is None:
            continue
        candidates.append((timestamp, normalized))

    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _ensure_display_name(api: Garmin) -> None:
    if not getattr(api, "display_name", None):
        if hasattr(api, "_load_profile_and_settings"):
            try:
                api._load_profile_and_settings()
            except Exception:
                pass


class GarminClient:
    def __init__(
        self,
        *,
        email: str | None = None,
        token_store=None,
    ) -> None:
        self.email = email if email is not None else config.GARMIN_EMAIL
        self.token_store = token_store if token_store is not None else config.GARMIN_TOKEN_STORE
        self._api: Optional[Garmin] = None
        self._pending_api: Optional[Garmin] = None
        self._pending_state: dict | None = None
        self._hr_zone_cache: dict[int, list] = {}
        self._session_expired: bool = False

    # --- Auth -------------------------------------------------------------
    def login(
        self,
        password: Optional[str] = None,
        mfa_prompt: Optional[Callable[[], str]] = None,
    ) -> None:
        """Connect to Garmin.

        First tries the cached token. If that fails (or none exists) and a
        password is supplied, performs a fresh login, prompting for MFA via
        ``mfa_prompt`` when Garmin requires it, then caches the new token.
        """
        token_dir = self.token_store
        token_dir.mkdir(parents=True, exist_ok=True)
        token_store = str(token_dir)

        try:
            api = Garmin()
            api.login(token_store)
            api.get_full_name()  # cheap authenticated call — proves the session
            _ensure_display_name(api)
            self._api = api
            self._session_expired = False
            return
        except Exception:
            self._api = None  # fall through to credential login

        if not self.email or not password:
            self._session_expired = True
            raise GarminConnectAuthenticationError(
                "No valid cached token and no email/password provided. "
                "Run the first-login flow with your Garmin password."
            )

        api = Garmin(
            email=self.email,
            password=password,
            prompt_mfa=mfa_prompt or (lambda: ""),
            return_on_mfa=False,
        )
        api.login(token_store)
        api.get_full_name()
        _ensure_display_name(api)
        self._api = api
        self._session_expired = False

    def ensure_authenticated(
        self,
        password: Optional[str] = None,
        mfa_prompt: Optional[Callable[[], str]] = None,
    ) -> None:
        """Reuse a live session, restoring legacy cached tokens only if needed.

        Multi-user clients are restored from the encrypted per-user vault into
        memory. Calling ``login()`` on those clients would replace that valid
        API object and look in the unrelated legacy filesystem token store.
        """
        if self.is_authenticated():
            return
        self.login(password=password, mfa_prompt=mfa_prompt)

    def mark_session_expired(self) -> None:
        """Prevent further mutations after Garmin rejects the live session."""
        self._session_expired = True

    def begin_login(self, email: str, password: str) -> str:
        """Start a fresh per-user login without persisting the password.

        Returns ``connected`` or ``mfa_required``. When MFA is needed, the
        library's short-lived SSO state remains only in this process.
        """
        if not email or not password:
            raise GarminConnectAuthenticationError("Email and password are required")
        self.email = email.strip()
        api = Garmin(
            email=self.email,
            password=password,
            return_on_mfa=True,
        )
        status, client_state = api.login()
        if status == "needs_mfa":
            self._pending_api = api
            self._pending_state = client_state or {}
            self._api = None
            return "mfa_required"
        api.get_full_name()
        _ensure_display_name(api)
        self._pending_api = None
        self._pending_state = None
        self._api = api
        return "connected"

    def complete_mfa(self, code: str) -> None:
        if self._pending_api is None or self._pending_state is None or not code.strip():
            raise GarminConnectAuthenticationError("MFA session is invalid or expired")
        api = self._pending_api
        api.resume_login(self._pending_state, code.strip())
        api.get_full_name()
        _ensure_display_name(api)
        self._pending_api = None
        self._pending_state = None
        self._api = api

    def restore_tokens(self, token_json: str) -> None:
        """Restore an encrypted-at-rest session without a plaintext token file."""
        if not token_json:
            raise GarminConnectAuthenticationError("Garmin token data is empty")
        api = Garmin()
        api.client.loads(token_json)
        try:
            api.get_full_name()
        except Exception as exc:
            self._api = None
            self._session_expired = True
            raise GarminConnectAuthenticationError("Garmin session token is expired or invalid") from exc
        _ensure_display_name(api)
        self._api = api
        self._session_expired = False

    def serialized_tokens(self) -> str:
        return self.api.client.dumps()

    @property
    def api(self) -> Garmin:
        if self._api is None:
            raise RuntimeError("GarminClient.login() must be called first.")
        return self._api

    def is_authenticated(self) -> bool:
        return self._api is not None and not self._session_expired

    def _read(self, endpoint: str, call: Callable[[], Any]) -> Any:
        return instrument_read(endpoint, call)
    # Thin wrappers; names mirror the plan's verified method list.

    def activities_by_date(self, start: date, end: date) -> list[dict]:
        return self._read("activities_by_date", lambda: self.api.get_activities_by_date(start.isoformat(), end.isoformat()))

    def recent_activities(self, limit: int = 1) -> list[dict]:
        return self._read("recent_activities", lambda: self.api.get_activities(0, limit) or [])

    def activity_count(self) -> int:
        return self._read("activity_count", self.api.count_activities)

    def activity_detail(self, activity_id: int) -> dict:
        return self._read("activity_detail", lambda: self.api.get_activity(activity_id))

    def exercise_sets(self, activity_id: int) -> dict:
        """Per-set strength detail: exercise name/category, reps, weight, rest."""
        return self._read("activity_strength_sets", lambda: self.api.get_activity_exercise_sets(activity_id))

    def hr_zones(self, activity_id: int) -> list:
        """Time-in-HR-zone for an activity. Cached in-memory: a past activity's
        zones never change, so we fetch each at most once per process."""
        if activity_id not in self._hr_zone_cache:
            self._hr_zone_cache[activity_id] = (
                self._read("activity_hr_zones", lambda: self.api.get_activity_hr_in_timezones(activity_id) or [])
            )
        return self._hr_zone_cache[activity_id]

    def sleep(self, day: date) -> dict:
        return self._read("sleep", lambda: self.api.get_sleep_data(day.isoformat()))

    def hrv(self, day: date) -> dict:
        return self._read("hrv", lambda: self.api.get_hrv_data(day.isoformat()))

    def body_battery(self, start: date, end: date) -> list[dict]:
        return self._read("body_battery", lambda: self.api.get_body_battery(start.isoformat(), end.isoformat()))

    def stress(self, day: date) -> dict:
        return self._read("stress", lambda: self.api.get_all_day_stress(day.isoformat()))

    def resting_hr(self, day: date) -> dict:
        return self._read("resting_hr", lambda: self.api.get_rhr_day(day.isoformat()))

    def daily_steps(self, start: date, end: date) -> list[dict]:
        return self._read("daily_steps", lambda: self.api.get_daily_steps(start.isoformat(), end.isoformat()))

    def user_summary(self, day: date) -> dict:
        return self._read("daily_summary", lambda: self.api.get_stats(day.isoformat()))

    def device_last_used(self) -> dict:
        return self._read("device_last_used", self.api.get_device_last_used)

    def training_readiness(self, day: date) -> dict | list[dict]:
        return self._read("training_readiness", lambda: self.api.get_training_readiness(day.isoformat()))

    def training_status(self, day: date) -> dict:
        return self._read("training_status", lambda: self.api.get_training_status(day.isoformat()))

    def fitness_age(self, day: date) -> dict:
        """Current Fitness Age only; callers must not turn this into a scan."""
        return self._read("fitness_age", lambda: self.api.get_fitnessage_data(day.isoformat()))

    def workout_list(self) -> list[dict] | None:
        return self._read("workout_list", self.api.get_workouts)

    def workout_detail(self, workout_id: int) -> dict:
        return self._read("workout_detail", lambda: self.api.get_workout_by_id(workout_id))

    def user_profile(self) -> dict:
        return self._read("user_profile", self.api.get_user_profile)


_legacy_client = GarminClient()


class GarminClientProxy:
    """Preserve existing call sites while selecting the authenticated tenant."""

    def _target(self) -> GarminClient:
        if not config.MULTI_USER_ENABLED:
            return _legacy_client
        from sync.garmin_registry import get_garmin_registry
        from tenant_context import current_tenant

        tenant = current_tenant()
        if tenant is None:
            return _legacy_client
        return get_garmin_registry().get(tenant.user_id)

    @property
    def api(self) -> Garmin:
        return self._target().api

    def __getattr__(self, name):
        return getattr(self._target(), name)

    def __setattr__(self, name, value) -> None:
        setattr(self._target(), name, value)


client = GarminClientProxy()
