"""Invitation-only Google authentication routes for multi-user mode."""
from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import config
from auth_service import (
    AuthenticationError,
    begin_google_oidc,
    bind_google_identity,
    consume_oauth_attempt,
    create_web_session,
    exchange_invitation_for_enrollment,
    revoke_web_session,
)
from control_db import get_control_session
from google_oidc import exchange_code
from tenant_store import provision_user_store


router = APIRouter()
SESSION_COOKIE = "__Host-gc_session"
ENROLLMENT_COOKIE = "__Host-gc_enroll"


def _disabled() -> Response | None:
    if not config.MULTI_USER_ENABLED:
        return Response(status_code=404)
    return None


def _no_store(response: Response) -> Response:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/auth/login", response_class=HTMLResponse)
def multi_user_login() -> Response:
    if disabled := _disabled():
        return disabled
    return _no_store(HTMLResponse("""
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in - GarminCoach</title></head><body>
<main><h1>GarminCoach</h1><p>Sign in with your invited Google account.</p>
<a href="/auth/google/start">Continue with Google</a></main></body></html>
"""))


@router.get("/invite", response_class=HTMLResponse)
def invitation_page() -> Response:
    if disabled := _disabled():
        return disabled
    return _no_store(HTMLResponse("""
<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>Accept invitation</title></head>
<body><main><h1>Accept your GarminCoach invitation</h1>
<p id="status">Preparing your secure sign-in…</p></main>
<script src="/static/invitation.js" defer></script></body></html>
"""))


@router.post("/auth/enrollment")
def create_enrollment(invitation_token: str = Form("")) -> Response:
    if disabled := _disabled():
        return disabled
    try:
        with get_control_session() as session:
            ticket = exchange_invitation_for_enrollment(session, invitation_token)
    except AuthenticationError:
        return _no_store(Response(status_code=401))
    response = _no_store(Response(status_code=204))
    response.set_cookie(
        ENROLLMENT_COOKIE,
        ticket,
        max_age=600,
        secure=True,
        httponly=True,
        samesite="strict",
        path="/",
    )
    return response


@router.get("/auth/google/start")
def google_start(request: Request) -> Response:
    if disabled := _disabled():
        return disabled
    enrollment = request.cookies.get(ENROLLMENT_COOKIE)
    try:
        with get_control_session() as session:
            start = begin_google_oidc(
                session,
                redirect_uri=config.GOOGLE_REDIRECT_URI,
                enrollment_ticket=enrollment,
            )
    except AuthenticationError:
        return _no_store(HTMLResponse(
            "Invitation is invalid or expired.", status_code=401
        ))
    response = RedirectResponse(start.authorization_url, status_code=303)
    response.delete_cookie(ENROLLMENT_COOKIE, path="/", secure=True, httponly=True)
    return _no_store(response)


@router.get("/auth/google/callback")
def google_callback(state: str = "", code: str = "", error: str = "") -> Response:
    if disabled := _disabled():
        return disabled
    if error or not state or not code:
        return _no_store(HTMLResponse("Google sign-in was cancelled.", status_code=401))
    try:
        # Burn state before the external exchange so a failed or intercepted
        # callback can never be replayed.
        with get_control_session() as session:
            attempt = consume_oauth_attempt(session, state)
            session.commit()
            claims = exchange_code(code, attempt, redirect_uri=config.GOOGLE_REDIRECT_URI)
            user = bind_google_identity(
                session,
                attempt=attempt,
                claims=claims,
                owner_email=config.OWNER_GOOGLE_EMAIL,
            )
            provision_user_store(user.id)
            raw_session = create_web_session(session, user)
    except AuthenticationError:
        return _no_store(HTMLResponse(
            "This Google account is not authorized for GarminCoach.", status_code=401
        ))
    response = RedirectResponse("/onboarding", status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        raw_session,
        max_age=config.SESSION_MAX_AGE_DAYS * 86400,
        secure=True,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return _no_store(response)


@router.post("/auth/logout")
def multi_user_logout(request: Request) -> Response:
    if disabled := _disabled():
        return disabled
    raw_session = request.cookies.get(SESSION_COOKIE, "")
    with get_control_session() as session:
        revoke_web_session(session, raw_session)
    response = RedirectResponse("/auth/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/", secure=True, httponly=True)
    return _no_store(response)
