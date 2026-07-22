"""Authenticated multi-user settings, invitations, and destructive deletion."""
from __future__ import annotations

import hashlib
import shutil
from urllib.parse import urlsplit

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

import config
from auth_routes import SESSION_COOKIE
from auth_service import issue_invitation
from coach.calendar import validate_ics_url
from control_db import AuditEvent, Invitation, User, get_control_session, utcnow
from secret_vault import UserSecretVault
from sync.garmin_registry import get_garmin_registry
from sync.scheduler import refresh_user_jobs
from telegram_link import issue_link_code, unlink_user
from tenant_store import dispose_user_engine, user_root


router = APIRouter(prefix="/account")
templates = Jinja2Templates(directory=str(config.PROJECT_ROOT / "templates"))
templates.env.globals["asset_version"] = lambda: 0
templates.env.globals["multi_user_enabled"] = True


def _disabled() -> Response | None:
    return Response(status_code=404) if not config.MULTI_USER_ENABLED else None


def _current_user(request: Request) -> User:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(status_code=401)
    return user


def _settings_context(
    user: User,
    *,
    invitation_link: str | None = None,
    telegram_command: str | None = None,
    error: str | None = None,
):
    with get_control_session() as session:
        users = session.query(User).order_by(User.created_at).all() if user.role == "owner" else []
        invitations = (
            session.query(Invitation).order_by(Invitation.created_at.desc()).all()
            if user.role == "owner" else []
        )
    calendar_url = UserSecretVault().read(user.id).get("calendar_ics_url", "")
    return {
        "user": user,
        "users": users,
        "invitations": invitations,
        "calendar_url": calendar_url,
        "invitation_link": invitation_link,
        "telegram_command": telegram_command,
        "error": error,
        "max_invited_users": config.MAX_INVITED_USERS,
        "telegram_bot_username": config.TELEGRAM_BOT_USERNAME,
    }


@router.get("", response_class=HTMLResponse)
def account_settings(request: Request):
    if disabled := _disabled():
        return disabled
    return templates.TemplateResponse(
        request, "account.html", _settings_context(_current_user(request)),
        headers={"Cache-Control": "no-store"},
    )


@router.post("/calendar")
def save_calendar(request: Request, calendar_url: str = Form("")):
    if disabled := _disabled():
        return disabled
    user = _current_user(request)
    value = calendar_url.strip()
    if value:
        try:
            validate_ics_url(value)
        except ValueError as exc:
            return templates.TemplateResponse(
                request, "account.html", _settings_context(user, error=str(exc)),
                status_code=400, headers={"Cache-Control": "no-store"},
            )
    UserSecretVault().update(user.id, calendar_ics_url=value or None)
    with get_control_session() as session:
        stored = session.get(User, user.id)
        stored.inbound_calendar_linked = bool(value)
        stored.updated_at = utcnow()
    return RedirectResponse("/account", status_code=303)


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
