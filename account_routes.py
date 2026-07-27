"""Authenticated multi-user settings, invitations, and destructive deletion."""
from __future__ import annotations

import hashlib
import secrets
import shutil
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import config
from auth_routes import SESSION_COOKIE
from auth_service import issue_invitation
from coach.calendar import calendar_feed_record, test_calendar_url
from control_db import (
    AuditEvent,
    IntegrationRoute,
    Invitation,
    User,
    calendar_feed_token_hash,
    generate_calendar_feed_token,
    get_control_session,
    utcnow,
)
from secret_vault import UserSecretVault
from sync.garmin_registry import get_garmin_registry
from sync.scheduler import refresh_user_jobs
from telegram_link import issue_link_code, unlink_user
from tenant_store import dispose_user_engine, user_root


import time
router = APIRouter(prefix="/account")
templates = Jinja2Templates(directory=str(config.PROJECT_ROOT / "templates"))
templates.env.globals["asset_version"] = lambda: int(time.time())
templates.env.globals["multi_user_enabled"] = True


def _disabled() -> Response | None:
    return Response(status_code=404) if not config.MULTI_USER_ENABLED else None


def _current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        if not config.MULTI_USER_ENABLED:
            return User(id=1, email=config.APP_USERNAME or "athlete@garmincoach.local", role="owner")
        raise HTTPException(status_code=401)
    return user


def _settings_context(
    user: User,
    *,
    invitation_link: str | None = None,
    telegram_command: str | None = None,
    error: str | None = None,
    success: str | None = None,
    outbound_calendar_url: str | None = None,
):
    with get_control_session() as session:
        users = session.query(User).order_by(User.created_at).all() if user.role == "owner" and config.MULTI_USER_ENABLED else []
        invitations = (
            session.query(Invitation).order_by(Invitation.created_at.desc()).all()
            if user.role == "owner" and config.MULTI_USER_ENABLED else []
        )
        route = session.get(IntegrationRoute, user.id) if config.MULTI_USER_ENABLED else None
    try:
        vault_values = UserSecretVault().read(user.id)
    except Exception:
        vault_values = {}
    stored_feeds = vault_values.get("calendar_feeds")
    if not isinstance(stored_feeds, list):
        legacy = vault_values.get("calendar_ics_url")
        stored_feeds = [calendar_feed_record(legacy)] if legacy else []
        if stored_feeds:
            try:
                UserSecretVault().update(
                    user.id, calendar_feeds=stored_feeds, calendar_ics_url=None
                )
            except Exception:
                pass
    calendar_feeds = [
        {"id": item.get("id"), "provider": item.get("provider", "Calendar")}
        for item in stored_feeds
        if isinstance(item, dict) and item.get("id") and item.get("url")
    ]
    return {
        "user": user,
        "users": users,
        "invitations": invitations,
        "calendar_feeds": calendar_feeds,
        "invitation_link": invitation_link,
        "telegram_command": telegram_command,
        "error": error,
        "success": success,
        "max_invited_users": config.MAX_INVITED_USERS,
        "telegram_bot_username": config.TELEGRAM_BOT_USERNAME,
        "has_outbound_calendar_subscription": bool(
            route and route.calendar_feed_token_hash
        ),
        "outbound_calendar_url": outbound_calendar_url,
    }


@router.get("", response_class=HTMLResponse)
def account_settings(request: Request):
    status = request.query_params.get("calendar_status", "")
    success = "Calendar removed successfully." if status == "removed" else None
    if status == "subscription_revoked":
        success = "Your private calendar subscription URL was revoked."
    if status == "added":
        try:
            event_count = max(0, min(999, int(request.query_params.get("events", "0"))))
        except ValueError:
            event_count = 0
        success = (
            f"Calendar connected successfully. Found {event_count} upcoming "
            f"event{'s' if event_count != 1 else ''} in the next 30 days."
        )
    return templates.TemplateResponse(
        request, "account.html", _settings_context(
            _current_user(request),
            success=success,
        ),
        headers={"Cache-Control": "no-store"},
    )


def _outbound_calendar_url(token: str) -> str:
    """Build a subscription URL from trusted server configuration, not Host."""
    parsed = urlsplit(config.GOOGLE_REDIRECT_URI)
    if parsed.scheme != "https" or not parsed.netloc:
        raise RuntimeError("GOOGLE_REDIRECT_URI must use an HTTPS public origin")
    return f"{parsed.scheme}://{parsed.netloc}/calendar/feed/{token}.ics"


@router.post("/calendar-subscription/generate", response_class=HTMLResponse)
def generate_calendar_subscription(request: Request):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    token = generate_calendar_feed_token()
    with get_control_session() as session:
        route = session.get(IntegrationRoute, user.id)
        if route is None:
            route = IntegrationRoute(user_id=user.id)
            session.add(route)
        route.calendar_feed_token_hash = calendar_feed_token_hash(token)
        route.updated_at = utcnow()
    return templates.TemplateResponse(
        request,
        "account.html",
        _settings_context(
            user,
            success=(
                "Your private calendar subscription URL is ready. Store it now; "
                "it cannot be shown again."
            ),
            outbound_calendar_url=_outbound_calendar_url(token),
        ),
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/calendar-subscription/revoke")
def revoke_calendar_subscription(request: Request):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    with get_control_session() as session:
        route = session.get(IntegrationRoute, user.id)
        if route is not None:
            route.calendar_feed_token_hash = None
            route.updated_at = utcnow()
    return RedirectResponse("/account?calendar_status=subscription_revoked", status_code=303)


@router.post("/calendar")
def save_calendar(request: Request, calendar_url: str = Form("")):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    value = calendar_url.strip()
    if not value:
        return templates.TemplateResponse(
            request, "account.html", _settings_context(user, error="Enter a calendar URL."),
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    try:
        normalized, event_count = test_calendar_url(value)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "account.html", _settings_context(user, error=str(exc)),
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    except Exception:
        return templates.TemplateResponse(
            request, "account.html",
            _settings_context(user, error="The calendar could not be downloaded or parsed."),
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    vault = UserSecretVault()
    values = vault.read(user.id)
    feeds = values.get("calendar_feeds")
    if not isinstance(feeds, list):
        legacy = values.get("calendar_ics_url")
        feeds = [calendar_feed_record(legacy)] if legacy else []
    if any(item.get("url") == normalized for item in feeds if isinstance(item, dict)):
        return templates.TemplateResponse(
            request, "account.html",
            _settings_context(user, error="That calendar is already connected."),
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    if len(feeds) >= 5:
        return templates.TemplateResponse(
            request, "account.html",
            _settings_context(user, error="You can connect up to five calendars."),
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    feeds.append(calendar_feed_record(normalized))
    vault.update(user.id, calendar_feeds=feeds, calendar_ics_url=None)
    with get_control_session() as session:
        stored = session.get(User, user.id)
        stored.inbound_calendar_linked = True
        stored.updated_at = utcnow()
    return RedirectResponse(
        f"/account?calendar_status=added&events={min(event_count, 999)}",
        status_code=303,
    )


@router.post("/calendar/{feed_id}/delete")
def delete_calendar(request: Request, feed_id: str):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    if len(feed_id) != 16 or any(character not in "0123456789abcdef" for character in feed_id):
        raise HTTPException(status_code=404)
    vault = UserSecretVault()
    values = vault.read(user.id)
    feeds = values.get("calendar_feeds")
    if not isinstance(feeds, list):
        raise HTTPException(status_code=404)
    remaining = [item for item in feeds if not (
        isinstance(item, dict) and secrets.compare_digest(str(item.get("id", "")), feed_id)
    )]
    if len(remaining) == len(feeds):
        raise HTTPException(status_code=404)
    vault.update(user.id, calendar_feeds=remaining)
    with get_control_session() as session:
        stored = session.get(User, user.id)
        stored.inbound_calendar_linked = bool(remaining)
        stored.updated_at = utcnow()
    return RedirectResponse("/account?calendar_status=removed", status_code=303)


@router.post("/telegram/link", response_class=HTMLResponse)
def create_telegram_link(request: Request):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    code = issue_link_code(user.id)
    return templates.TemplateResponse(
        request,
        "account.html",
        _settings_context(user, telegram_command=f"/link {code}"),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/telegram/open")
def open_telegram_link(request: Request):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    if not config.TELEGRAM_BOT_USERNAME:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")
    code = issue_link_code(user.id)
    return RedirectResponse(
        f"https://t.me/{config.TELEGRAM_BOT_USERNAME}?start=link_{code}",
        status_code=303,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


@router.post("/telegram/unlink")
def unlink_telegram(request: Request):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    unlink_user(user.id)
    refresh_user_jobs(user.id)
    return RedirectResponse("/account", status_code=303)


@router.post("/invitations", response_class=HTMLResponse)
def create_invitation(request: Request, email: str = Form("")):
    if disabled := _disabled():
        return disabled
    actor = _current_user(request)
    if actor.role != "owner":
        raise HTTPException(status_code=403)
    try:
        with get_control_session() as session:
            owner = session.get(User, actor.id)
            _invitation, token = issue_invitation(session, owner=owner, email=email)
    except ValueError as exc:
        return templates.TemplateResponse(
            request, "account.html", _settings_context(actor, error=str(exc)),
            status_code=400, headers={"Cache-Control": "no-store"},
        )
    origin = f"{urlsplit(config.GOOGLE_REDIRECT_URI).scheme}://{urlsplit(config.GOOGLE_REDIRECT_URI).netloc}"
    link = f"{origin}/invite#{token}"
    return templates.TemplateResponse(
        request, "account.html", _settings_context(actor, invitation_link=link),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/invitations/{invitation_id}/revoke")
def revoke_invitation(request: Request, invitation_id: str):
    if disabled := _disabled():
        return disabled
    actor = _current_user(request)
    if actor.role != "owner":
        raise HTTPException(status_code=403)
    with get_control_session() as session:
        invitation = session.get(Invitation, invitation_id)
        if invitation is None:
            raise HTTPException(status_code=404)
        if invitation.consumed_at is not None:
            raise HTTPException(status_code=409, detail="Invitation was already used")
        invitation.revoked_at = utcnow()
    return RedirectResponse("/account", status_code=303)


def _destroy_user(user_id: str, *, actor_user_id: str | None) -> None:
    with get_control_session() as session:
        user = session.get(User, user_id)
        if user is None:
            raise HTTPException(status_code=404)
        user.status = "deleting"
        session.commit()
    refresh_user_jobs(user_id)
    get_garmin_registry().evict(user_id)
    dispose_user_engine(user_id)
    directory = user_root(user_id)
    if directory.exists():
        shutil.rmtree(directory)
    subject = hashlib.sha256(user_id.encode("ascii")).hexdigest()[:32]
    with get_control_session() as session:
        user = session.get(User, user_id)
        if user is not None:
            session.delete(user)
        session.add(AuditEvent(
            actor_user_id=actor_user_id,
            subject_ref=subject,
            event_type="account_deleted",
            outcome="success",
        ))


@router.post("/delete")
def delete_own_account(request: Request, confirmation: str = Form("")):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    if confirmation != "DELETE":
        raise HTTPException(status_code=400, detail="Type DELETE to confirm")
    if user.role == "owner":
        with get_control_session() as session:
            if session.query(User).filter(User.id != user.id).count():
                raise HTTPException(status_code=409, detail="Delete invited users first")
    _destroy_user(user.id, actor_user_id=None)
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
    return response


@router.post("/users/{user_id}/delete")
def owner_delete_user(request: Request, user_id: str, confirmation: str = Form("")):
    if disabled := _disabled():
        return disabled
    actor = _current_user(request)
    if actor.role != "owner" or actor.id == user_id:
        raise HTTPException(status_code=403)
    with get_control_session() as session:
        subject = session.get(User, user_id)
        if subject is None:
            raise HTTPException(status_code=404)
        expected = f"DELETE {subject.email}"
    if confirmation != expected:
        raise HTTPException(status_code=400, detail=f"Type {expected} to confirm")
    _destroy_user(user_id, actor_user_id=actor.id)
    return RedirectResponse("/account", status_code=303)
