"""Atomically configure a production instance for invitation-only Google auth."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile

from cryptography.fernet import Fernet


def configure(env_path: Path, oauth_path: Path, owner_email: str, public_origin: str) -> None:
    document = json.loads(oauth_path.read_text(encoding="utf-8"))
    client = document.get("web")
    if not isinstance(client, dict):
        raise ValueError("OAuth credentials must be a Web application client")
    callback = f"{public_origin.rstrip('/')}/auth/google/callback"
    if callback not in client.get("redirect_uris", []):
        raise ValueError("OAuth client is missing the exact production callback URI")
    if public_origin not in client.get("javascript_origins", []):
        raise ValueError("OAuth client is missing the exact production origin")
    if not client.get("client_id") or not client.get("client_secret"):
        raise ValueError("OAuth client credentials are incomplete")

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    existing: dict[str, str] = {}
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            existing[key] = value
    values = {
        "MULTI_USER_ENABLED": "true",
        "MULTI_USER_DATA_ROOT": "/home/ubuntu/garmincoach/data/users",
        "CONTROL_DB_PATH": "/home/ubuntu/garmincoach/data/control.db",
        "DATA_ENCRYPTION_KEY": existing.get("DATA_ENCRYPTION_KEY") or Fernet.generate_key().decode("ascii"),
        "MAX_INVITED_USERS": "5",
        "OWNER_GOOGLE_EMAIL": owner_email.strip().casefold(),
        "GOOGLE_CLIENT_ID": client["client_id"],
        "GOOGLE_CLIENT_SECRET": client["client_secret"],
        "GOOGLE_REDIRECT_URI": callback,
        "APP_USERNAME": "",
        "APP_PASSWORD": "",
        "LLM_ENABLED": "false",
    }
    output: list[str] = []
    replaced: set[str] = set()
    for line in lines:
        key = line.split("=", 1)[0] if "=" in line and not line.lstrip().startswith("#") else None
        if key in values:
            output.append(f"{key}={values[key]}")
            replaced.add(key)
        else:
            output.append(line)
    for key, value in values.items():
        if key not in replaced:
            output.append(f"{key}={value}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=".env-", dir=env_path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, env_path)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--oauth-json", type=Path, required=True)
    parser.add_argument("--owner-email", required=True)
    parser.add_argument("--origin", required=True)
    args = parser.parse_args()
    configure(args.env, args.oauth_json, args.owner_email, args.origin)
    print("Multi-user environment configured; secret values were not printed.")


if __name__ == "__main__":
    main()
