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


class GarminClient:
    def __init__(self) -> None:
        self._api: Optional[Garmin] = None
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
        token_dir = config.GARMIN_TOKEN_STORE
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
            self._api = api
            return
        except Exception:
            self._api = None  # fall through to credential login

        # 2) Fresh login with credentials (+ MFA via prompt_mfa callback).
        if not config.GARMIN_EMAIL or not password:
            raise GarminConnectAuthenticationError(
                "No valid cached token and no email/password provided. "
                "Run the first-login flow with your Garmin password."
            )

        # With prompt_mfa set and return_on_mfa=False, login() performs the full
        # flow (calling prompt_mfa when Garmin challenges) and AUTOMATICALLY dumps
        # tokens to the tokenstore path — no separate save call needed.
        api = Garmin(
            email=config.GARMIN_EMAIL,
            password=password,
            prompt_mfa=mfa_prompt or (lambda: ""),
            return_on_mfa=False,
        )
        api.login(token_store)
        # Verify the session is genuinely authenticated before accepting it.
        # (A rate-limited / partial login can otherwise return without raising.)
        api.get_full_name()
        self._api = api

    @property
    def api(self) -> Garmin:
        if self._api is None:
            raise RuntimeError("GarminClient.login() must be called first.")
        return self._api

    def is_authenticated(self) -> bool:
        return self._api is not None

    # --- Fetchers ---------------------------------------------------------
    # Thin wrappers; names mirror the plan's verified method list.

    def activities_by_date(self, start: date, end: date) -> list[dict]:
        return self.api.get_activities_by_date(start.isoformat(), end.isoformat())

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


# Module-level singleton so the scheduler and web routes share one session.
client = GarminClient()
