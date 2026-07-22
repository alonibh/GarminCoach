"""Rotate Telegram's webhook secret and register the production endpoint."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import tempfile
import urllib.request


def secure_webhook(env_path: Path, domain: str) -> None:
    lines = env_path.read_text(encoding="utf-8").splitlines()
    values = {}
    for line in lines:
        if line and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    bot_token = values.get("TELEGRAM_BOT_TOKEN", "")
    if not bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is not configured")
    webhook_secret = secrets.token_urlsafe(32)
    replaced = False
    output = []
    for line in lines:
        if line.startswith("TELEGRAM_WEBHOOK_SECRET="):
            output.append(f"TELEGRAM_WEBHOOK_SECRET={webhook_secret}")
            replaced = True
        else:
            output.append(line)
    if not replaced:
        output.append(f"TELEGRAM_WEBHOOK_SECRET={webhook_secret}")

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

    url = f"https://api.telegram.org/bot{bot_token}/setWebhook"
    payload = json.dumps({
        "url": f"https://{domain}/telegram/webhook",
        "secret_token": webhook_secret,
        "drop_pending_updates": False,
    }).encode("utf-8")
    request = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.loads(response.read().decode("utf-8"))
    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the webhook registration")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env", type=Path, required=True)
    parser.add_argument("--domain", required=True)
    args = parser.parse_args()
    secure_webhook(args.env, args.domain)
    print("Telegram webhook secret rotated and registered; secrets were not printed.")


if __name__ == "__main__":
    main()
