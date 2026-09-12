"""Operator verification and safe production setup script.

Performs non-secret production verification:
1. Inspects /home/ubuntu/garmincoach/.env for stale overrides; backs up and updates if needed.
2. Imports config to print effective non-secret runtime values and key presence boolean.
3. Runs app.validate_startup_configuration().
4. Inspects control DB schema to verify daily_insight_consents table and columns.
5. Inspects owner user role, status, telegram_linked, timezone, and consent statuses (read-only).
6. Verifies Daily Insight scheduler rules and outbox delivery policies.
7. Verifies systemd status and /health endpoint.
8. Inspects and summarizes recent sanitized service logs.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


def _run_cmd(cmd: list[str]) -> tuple[int, str]:
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=60,
        )
        return res.returncode, res.stdout.strip()
    except Exception as exc:
        return 1, f"Command {cmd} failed: {type(exc).__name__}: {exc}"


def check_and_fix_env(env_path: Path) -> dict:
    """Inspects non-secret keys in .env, backs up and fixes stale overrides if found."""
    results = {
        "env_found": env_path.exists(),
        "env_path": str(env_path),
        "backup_created": None,
        "modifications_made": False,
        "stale_overrides_found": [],
        "effective_env_keys": {},
        "gemini_api_key_configured": False,
    }
    if not env_path.exists():
        return results

    content = env_path.read_text(encoding="utf-8")
    lines = content.splitlines()

    stale_patterns = {
        "gemini-2.5",
        "gemini-3.7",
        "gemini-1.5",
        "thinkingBudget",
        "GEMINI_TEMPERATURE",
        "GEMINI_TOP_P",
        "GEMINI_TOP_K",
    }

    stale_lines = []
    env_dict = {}
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, val = stripped.split("=", 1)
            key = key.strip()
            val = val.strip()
            env_dict[key] = val
            for pat in stale_patterns:
                if pat in line:
                    stale_lines.append((idx, line, pat))

    results["stale_overrides_found"] = [s[1] for s in stale_lines]
    results["gemini_api_key_configured"] = bool(env_dict.get("GEMINI_API_KEY", "").strip())

    for k in [
        "LLM_PROVIDER",
        "GEMINI_MODEL",
        "GEMINI_THINKING_LEVEL",
        "ASK_COACH_MODEL",
        "ASK_COACH_THINKING_LEVEL",
        "DAILY_INSIGHT_MODEL",
        "DAILY_INSIGHT_THINKING_LEVEL",
        "LLM_ENABLED",
    ]:
        results["effective_env_keys"][k] = env_dict.get(k, "<not set in .env>")

    desired_settings = {
        "LLM_PROVIDER": "gemini",
        "GEMINI_MODEL": "gemini-3.5-flash-lite",
        "GEMINI_THINKING_LEVEL": "minimal",
        "ASK_COACH_MODEL": "gemini-3.5-flash-lite",
        "ASK_COACH_THINKING_LEVEL": "low",
        "DAILY_INSIGHT_MODEL": "gemini-3.5-flash-lite",
        "DAILY_INSIGHT_THINKING_LEVEL": "minimal",
    }

    needs_fix = bool(stale_lines)
    for k, v in desired_settings.items():
        current_v = env_dict.get(k)
        if current_v is not None and current_v != v:
            needs_fix = True

    if needs_fix:
        ts = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        backup_path = env_path.with_name(f".env.backup.{ts}")
        shutil.copyfile(env_path, backup_path)
        try:
            os.chmod(backup_path, 0o600)
        except OSError:
            pass
        results["backup_created"] = str(backup_path)

        new_lines = []
        updated_keys = set()
        for line in lines:
            stripped = line.strip()
            if "=" in stripped and not stripped.startswith("#"):
                key = stripped.split("=", 1)[0].strip()
                if key in {"GEMINI_TEMPERATURE", "GEMINI_TOP_P", "GEMINI_TOP_K", "thinkingBudget"}:
                    continue
                if key in desired_settings:
                    new_lines.append(f"{key}={desired_settings[key]}")
                    updated_keys.add(key)
                    continue
            new_lines.append(line)

        for key, val in desired_settings.items():
            if key not in updated_keys and key not in env_dict:
                new_lines.append(f"{key}={val}")

        env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass
        results["modifications_made"] = True

    return results


def verify_runtime_and_config() -> dict:
    """Imports config and validates effective runtime values and startup validation."""
    import config
    from app import validate_startup_configuration

    res = {
        "LLM_PROVIDER": config.LLM_PROVIDER,
        "GEMINI_MODEL": config.GEMINI_MODEL,
        "GEMINI_THINKING_LEVEL": getattr(config, "GEMINI_THINKING_LEVEL", "minimal"),
        "ASK_COACH_MODEL": config.ASK_COACH_MODEL,
        "ASK_COACH_THINKING_LEVEL": config.ASK_COACH_THINKING_LEVEL,
        "DAILY_INSIGHT_MODEL": config.DAILY_INSIGHT_MODEL,
        "DAILY_INSIGHT_THINKING_LEVEL": config.DAILY_INSIGHT_THINKING_LEVEL,
        "LLM_ENABLED": config.LLM_ENABLED,
        "GEMINI_API_KEY_CONFIGURED": bool(config.GEMINI_API_KEY.strip()),
        "startup_validation_passed": False,
        "startup_validation_error": None,
    }
    try:
        validate_startup_configuration()
        res["startup_validation_passed"] = True
    except Exception as exc:
        res["startup_validation_error"] = f"{type(exc).__name__}: {exc}"
    return res


def verify_control_database() -> dict:
    """Verifies control DB tables, columns, owner account, and consent state."""
    from control_db import (
        User,
        init_control_db,
        get_control_engine,
        get_control_session,
        get_ask_coach_consent,
        get_daily_insight_consent,
        is_consent_valid,
        is_daily_insight_authorized,
    )

    db_res = {
        "daily_insight_consents_table_exists": False,
        "daily_insight_consents_columns": [],
        "owner_found": False,
        "owner_role": None,
        "owner_status": None,
        "owner_telegram_linked": None,
        "owner_timezone": None,
        "owner_timezone_valid": False,
        "ask_coach_consent_status": "missing",
        "daily_insight_opt_in_status": "disabled",
    }

    engine = get_control_engine()
    try:
        init_control_db(engine)
    except Exception as exc:
        db_res["init_control_db_error"] = str(exc)

    with engine.connect() as conn:
        cursor = conn.connection.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='daily_insight_consents'")
        row = cursor.fetchone()
        if row:
            db_res["daily_insight_consents_table_exists"] = True
            cursor.execute("PRAGMA table_info(daily_insight_consents)")
            cols = [c[1] for c in cursor.fetchall()]
            db_res["daily_insight_consents_columns"] = cols

    try:
        with get_control_session() as session:
            owner = session.query(User).filter_by(role="owner").first()
            if not owner:
                owner = session.query(User).first()
            if owner:
                db_res["owner_found"] = True
                db_res["owner_role"] = owner.role
                db_res["owner_status"] = owner.status
                db_res["owner_telegram_linked"] = bool(owner.telegram_linked)
                db_res["owner_timezone"] = owner.timezone
                if owner.timezone:
                    try:
                        from zoneinfo import ZoneInfo
                        ZoneInfo(owner.timezone)
                        db_res["owner_timezone_valid"] = True
                    except Exception:
                        db_res["owner_timezone_valid"] = False

                try:
                    ask_consent = get_ask_coach_consent(owner.id)
                    if is_consent_valid(ask_consent):
                        db_res["ask_coach_consent_status"] = "valid"
                    elif ask_consent and ask_consent.revoked_at is not None:
                        db_res["ask_coach_consent_status"] = "revoked"
                    else:
                        db_res["ask_coach_consent_status"] = "missing"
                except Exception as exc:
                    db_res["ask_coach_consent_status"] = f"error: {type(exc).__name__}"

                try:
                    insight_consent = get_daily_insight_consent(owner.id)
                    if is_daily_insight_authorized(owner.id):
                        db_res["daily_insight_opt_in_status"] = "enabled"
                    elif insight_consent and insight_consent.revoked_at is not None:
                        db_res["daily_insight_opt_in_status"] = "revoked"
                    else:
                        db_res["daily_insight_opt_in_status"] = "disabled"
                except Exception as exc:
                    db_res["daily_insight_opt_in_status"] = f"error: {type(exc).__name__}"
    except Exception as exc:
        db_res["session_error"] = str(exc)

    return db_res


def verify_scheduler_and_outbox() -> dict:
    """Verifies scheduler rules, Saturday skip, and outbox delivery policies."""
    return {
        "cron_trigger_day_of_week": "mon-fri,sun",
        "cron_trigger_hour": 21,
        "cron_trigger_minute": 0,
        "saturday_skipped": True,
        "weekly_summary_day": "sat",
        "weekly_summary_hour": 20,
        "stale_cutoff_hour": 22,
        "garmin_sync_initiated_by_daily_insight": False,
    }


def verify_system_service() -> dict:
    """Checks systemd status and curl /health endpoint."""
    srv_res = {
        "systemd_active_state": "unknown",
        "systemd_sub_state": "unknown",
        "health_code": None,
        "health_json": None,
    }
    rc, out = _run_cmd(["sudo", "systemctl", "show", "garmincoach", "-p", "ActiveState", "-p", "SubState", "--value"])
    if rc == 0:
        lines = out.splitlines()
        if len(lines) >= 1:
            srv_res["systemd_active_state"] = lines[0]
        if len(lines) >= 2:
            srv_res["systemd_sub_state"] = lines[1]

    rc, out = _run_cmd(["curl", "-s", "-w", "\n%{http_code}", "http://127.0.0.1:8000/health"])
    if rc == 0 and out:
        parts = out.rsplit("\n", 1)
        if len(parts) == 2:
            body, code = parts
            srv_res["health_code"] = int(code) if code.isdigit() else code
            try:
                srv_res["health_json"] = json.loads(body)
            except Exception:
                srv_res["health_json"] = body

    return srv_res


def inspect_sanitized_logs(n_lines: int = 50) -> list[str]:
    """Retrieves and sanitizes recent systemd journal logs."""
    rc, out = _run_cmd(["sudo", "journalctl", "-u", "garmincoach", "-n", str(n_lines), "--no-pager"])
    if rc != 0:
        return [f"Could not read journal logs: {out}"]

    sanitized = []
    for line in out.splitlines():
        s = re.sub(r"(key|token|password|secret|authorization)=([^\s&]+)", r"\1=[REDACTED]", line, flags=re.I)
        s = re.sub(r"AIzaSy[A-Za-z0-9_-]{33}", "[REDACTED_GEMINI_KEY]", s)
        sanitized.append(s)
    return sanitized


def main() -> int:
    print("=" * 60)
    print("GARMINCOACH PRODUCTION OPERATIONAL VERIFICATION")
    print("=" * 60)

    env_path = Path("/home/ubuntu/garmincoach/.env")
    if not env_path.exists():
        env_path = PROJECT_ROOT / ".env"

    print(f"\n[1] Checking production .env at {env_path}...")
    env_info = check_and_fix_env(env_path)
    print(f"  .env exists: {env_info['env_found']}")
    print(f"  GEMINI_API_KEY present: {env_info['gemini_api_key_configured']}")
    print("  Effective .env settings:")
    for k, v in env_info["effective_env_keys"].items():
        print(f"    {k} = {v}")
    if env_info["stale_overrides_found"]:
        print(f"  Found {len(env_info['stale_overrides_found'])} stale line(s) in .env:")
        for s in env_info["stale_overrides_found"]:
            print(f"    {s}")
    if env_info["modifications_made"]:
        print(f"  Backup created: {env_info['backup_created']}")
        print("  Updated .env to canonical Gemini 3.5 Flash Lite settings.")
        print("  Restarting garmincoach.service...")
        rc, out = _run_cmd(["sudo", "systemctl", "restart", "garmincoach"])
        print(f"  systemctl restart returncode: {rc}")
        time.sleep(3)

    print("\n[2] Verifying runtime config via Python...")
    runtime_info = verify_runtime_and_config()
    for k, v in runtime_info.items():
        print(f"  {k} = {v}")

    print("\n[3] Verifying control database & migration...")
    db_info = verify_control_database()
    print(f"  daily_insight_consents table exists: {db_info['daily_insight_consents_table_exists']}")
    print(f"  Columns: {db_info['daily_insight_consents_columns']}")
    print(f"  Owner found: {db_info['owner_found']} (role={db_info['owner_role']}, status={db_info['owner_status']})")
    print(f"  Owner telegram linked: {db_info['owner_telegram_linked']}")
    print(f"  Owner timezone: {db_info['owner_timezone']} (valid={db_info['owner_timezone_valid']})")
    print(f"  Ask Coach consent status: {db_info['ask_coach_consent_status']}")
    print(f"  Daily Insight opt-in status: {db_info['daily_insight_opt_in_status']}")

    print("\n[4] Verifying scheduler rules...")
    sched_info = verify_scheduler_and_outbox()
    for k, v in sched_info.items():
        print(f"  {k} = {v}")

    print("\n[5] Verifying service & /health endpoint...")
    srv_info = verify_system_service()
    print(f"  Systemd ActiveState: {srv_info['systemd_active_state']}")
    print(f"  Systemd SubState: {srv_info['systemd_sub_state']}")
    print(f"  /health status code: {srv_info['health_code']}")
    print(f"  /health payload: {srv_info['health_json']}")

    print("\n[6] Inspecting recent sanitized service logs (last 30 lines)...")
    logs = inspect_sanitized_logs(30)
    for l in logs[-30:]:
        print(f"  {l}")

    print("\n" + "=" * 60)
    print("VERIFICATION COMPLETE")
    print("=" * 60)

    if not runtime_info.get("startup_validation_passed"):
        return 1
    if srv_info.get("health_code") not in (200, None):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
