"""Mandatory multi-user consent, timezone, and Garmin connection flow."""
from __future__ import annotations

from datetime import datetime

import pytz
from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

import config
from control_db import User, get_control_session, utcnow
from sync import sync_runner
from sync.garmin_registry import get_garmin_registry


router = APIRouter(prefix="/setup")
CONSENT_VERSION = "privacy-2026-07-22-v1"


def _redirect(error: str | None = None) -> RedirectResponse:
    target = "/onboarding" + (f"?error={error}" if error else "")
    response = RedirectResponse(target, status_code=303)
    response.headers["Cache-Control"] = "no-store"
    return response


def _user_for_update(session, request: Request) -> User:
    request_user = getattr(request.state, "user", None)
    user = session.get(User, request_user.id) if request_user else None
    if user is None or user.status == "deleting":
        raise PermissionError("Account is unavailable")
    return user


def _activate_connected_user(session, user: User, now: datetime | None = None) -> None:
    if not user.consented_at or not user.timezone:
        raise ValueError("Consent and timezone are required")
    user.garmin_connected = True
    user.onboarding_step = "complete"
    user.status = "active"
    user.updated_at = now or utcnow()


@router.post("/consent")
def accept_privacy_notice(request: Request, accepted: str = Form("")) -> Response:
    if not config.MULTI_USER_ENABLED:
        return Response(status_code=404)
    if accepted != "yes":
        return _redirect("consent_required")
    with get_control_session() as session:
        user = _user_for_update(session, request)
        if user.status == "active":
            return RedirectResponse("/", status_code=303)
        now = utcnow()
        user.consent_version = CONSENT_VERSION
        user.consented_at = now
        user.onboarding_step = "timezone"
        user.updated_at = now
    return _redirect()


@router.post("/timezone")
def choose_timezone(request: Request, timezone_name: str = Form("")) -> Response:
    if not config.MULTI_USER_ENABLED:
        return Response(status_code=404)
    try:
        pytz.timezone(timezone_name)
    except pytz.UnknownTimeZoneError:
        return _redirect("invalid_timezone")
    with get_control_session() as session:
        user = _user_for_update(session, request)
        if not user.consented_at:
            return _redirect("consent_required")
        user.timezone = timezone_name
        user.onboarding_step = "garmin"
        user.updated_at = utcnow()
    return _redirect()


@router.post("/garmin")
def connect_garmin(
    request: Request,
    garmin_email: str = Form(""),
    garmin_password: str = Form(""),
) -> Response:
    if not config.MULTI_USER_ENABLED:
        return Response(status_code=404)
    with get_control_session() as session:
        user = _user_for_update(session, request)
        if not user.consented_at or not user.timezone:
            return _redirect("configuration_required")
        user_id = user.id
    try:
        result = get_garmin_registry().begin_login(
            user_id, garmin_email.strip(), garmin_password
        )
    except GarminConnectTooManyRequestsError:
        return _redirect("garmin_rate_limited")
    except (GarminConnectAuthenticationError, GarminConnectConnectionError, ValueError):
        return _redirect("garmin_auth_failed")
    with get_control_session() as session:
        user = session.get(User, user_id)
        if result == "mfa_required":
            user.onboarding_step = "garmin_mfa"
            user.updated_at = utcnow()
        else:
            _activate_connected_user(session, user)
    if result == "connected":
        sync_runner.try_start_sync(full=True)
        return RedirectResponse("/", status_code=303)
    return _redirect()


@router.post("/garmin/mfa")
def complete_garmin_mfa(request: Request, mfa_code: str = Form("")) -> Response:
    if not config.MULTI_USER_ENABLED:
        return Response(status_code=404)
    with get_control_session() as session:
        user = _user_for_update(session, request)
        if user.onboarding_step != "garmin_mfa":
            return _redirect("garmin_session_expired")
        user_id = user.id
    try:
        get_garmin_registry().complete_mfa(user_id, mfa_code)
    except GarminConnectTooManyRequestsError:
        return _redirect("garmin_rate_limited")
    except (GarminConnectAuthenticationError, GarminConnectConnectionError, ValueError):
        return _redirect("garmin_mfa_failed")
    with get_control_session() as session:
        user = session.get(User, user_id)
        _activate_connected_user(session, user)
    sync_runner.try_start_sync(full=True)
    return RedirectResponse("/", status_code=303)
