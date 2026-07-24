"""Garmin Connect auth + data fetchers.

Auth strategy (critical for avoiding rate-limit lockouts):
  - Log in ONCE, persist the OAuth token to GARMIN_TOKEN_STORE.
  - On subsequent runs, resume from the cached token (no re-login).
  - MFA is handled interactively via a prompt callback on first login only.

All fetchers return plain dicts/lists straight from the library; parsing into
the DB schema happens in sync/sync_service.py.
"""
from __future__ import annotations

from datetime import date
from typing import Callable, Optional

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

import config


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
        # The token store is a directory; ensure it exists so the library can
        # persist tokens into it after a fresh login.
        token_dir = self.token_store
        token_dir.mkdir(parents=True, exist_ok=True)
        token_store = str(token_dir)

        # 1) Try resuming from cached token — the happy path, no creds needed.
        #    Garmin.login(path) loads tokens from `path` if present; raises if
        #    no usable token exists there. Only accept it if a real API call
        #    works (a loaded-but-expired token must NOT count as success).
        try:
            api = Garmin()
            api.login(token_store)
            api.get_full_name()  # cheap authenticated call — proves the session
            _ensure_display_name(api)
            self._api = api
            return
        except Exception:
            self._api = None  # fall through to credential login

        # 2) Fresh login with credentials (+ MFA via prompt_mfa callback).
        if not self.email or not password:
            raise GarminConnectAuthenticationError(
                "No valid cached token and no email/password provided. "
                "Run the first-login flow with your Garmin password."
            )

        # With prompt_mfa set and return_on_mfa=False, login() performs the full
        # flow (calling prompt_mfa when Garmin challenges) and AUTOMATICALLY dumps
        # tokens to the tokenstore path — no separate save call needed.
        api = Garmin(
            email=self.email,
            password=password,
            prompt_mfa=mfa_prompt or (lambda: ""),
            return_on_mfa=False,
        )
        api.login(token_store)
        # Verify the session is genuinely authenticated before accepting it.
        # (A rate-limited / partial login can otherwise return without raising.)
        api.get_full_name()
        _ensure_display_name(api)
        self._api = api

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
        except Exception:
            pass
        _ensure_display_name(api)
        self._api = api

    def serialized_tokens(self) -> str:
        return self.api.client.dumps()

    @property
    def api(self) -> Garmin:
        if self._api is None:
            raise RuntimeError("GarminClient.login() must be called first.")
        return self._api

    def is_authenticated(self) -> bool:
        return self._api is not None
    # Thin wrappers; names mirror the plan's verified method list.

    def activities_by_date(self, start: date, end: date) -> list[dict]:
        return self.api.get_activities_by_date(start.isoformat(), end.isoformat())

    def recent_activities(self, limit: int = 1) -> list[dict]:
        return self.api.get_activities(0, limit) or []

    def activity_count(self) -> int:
        return self.api.count_activities()

    def exercise_sets(self, activity_id: int) -> dict:
        """Per-set strength detail: exercise name/category, reps, weight, rest."""
        return self.api.get_activity_exercise_sets(activity_id)

    def hr_zones(self, activity_id: int) -> list:
        """Time-in-HR-zone for an activity. Cached in-memory: a past activity's
        zones never change, so we fetch each at most once per process."""
        if activity_id not in self._hr_zone_cache:
            self._hr_zone_cache[activity_id] = (
                self.api.get_activity_hr_in_timezones(activity_id) or []
            )
        return self._hr_zone_cache[activity_id]

    def sleep(self, day: date) -> dict:
        return self.api.get_sleep_data(day.isoformat())

    def hrv(self, day: date) -> dict:
        return self.api.get_hrv_data(day.isoformat())

    def body_battery(self, start: date, end: date) -> list[dict]:
        return self.api.get_body_battery(start.isoformat(), end.isoformat())

    def stress(self, day: date) -> dict:
        return self.api.get_all_day_stress(day.isoformat())

    def resting_hr(self, day: date) -> dict:
        return self.api.get_rhr_day(day.isoformat())

    def daily_steps(self, start: date, end: date) -> list[dict]:
        return self.api.get_daily_steps(start.isoformat(), end.isoformat())

    def user_summary(self, day: date) -> dict:
        return self.api.get_stats(day.isoformat())

    def device_last_used(self) -> dict:
        return self.api.get_device_last_used()

    def training_readiness(self, day: date) -> dict:
        return self.api.get_training_readiness(day.isoformat())

    def training_status(self, day: date) -> dict:
        return self.api.get_training_status(day.isoformat())


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
