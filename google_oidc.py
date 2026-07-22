"""Google authorization-code exchange and ID-token verification."""
from __future__ import annotations

import requests
from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2 import id_token

import config
from auth_service import AuthenticationError
from control_db import OAuthAttempt


GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"


def exchange_code(code: str, attempt: OAuthAttempt, *, redirect_uri: str) -> dict:
    if not code:
        raise AuthenticationError("Google authorization code is missing")
    try:
        response = requests.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "client_id": config.GOOGLE_CLIENT_ID,
                "client_secret": config.GOOGLE_CLIENT_SECRET,
                "redirect_uri": redirect_uri,
                "code_verifier": attempt.code_verifier,
            },
            timeout=15,
        )
        response.raise_for_status()
        token = response.json().get("id_token")
        if not token:
            raise AuthenticationError("Google identity token is missing")
        claims = id_token.verify_oauth2_token(
            token, GoogleRequest(), config.GOOGLE_CLIENT_ID
        )
        return claims
    except AuthenticationError:
        raise
    except Exception as exc:
        # Do not include Google responses, codes, or tokens in the public error.
        raise AuthenticationError("Google sign-in could not be completed") from exc
