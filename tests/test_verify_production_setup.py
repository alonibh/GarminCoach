import pytest
from pathlib import Path
from scripts.verify_production_setup import check_and_fix_env, verify_scheduler_and_outbox


def test_check_and_fix_env_fixes_stale_model(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GARMIN_EMAIL=test@example.com\n"
        "GEMINI_API_KEY=secret-dummy-key\n"
        "GEMINI_MODEL=gemini-2.5-flash\n"
        "GEMINI_TEMPERATURE=0.3\n"
        "ASK_COACH_MODEL=gemini-3.7-flash\n",
        encoding="utf-8",
    )

    info = check_and_fix_env(env_file)
    assert info["env_found"] is True
    assert info["gemini_api_key_configured"] is True
    assert len(info["stale_overrides_found"]) >= 2
    assert info["modifications_made"] is True
    assert info["backup_created"] is not None

    # Check updated content
    updated = env_file.read_text(encoding="utf-8")
    assert "GEMINI_MODEL=gemini-3.5-flash-lite" in updated
    assert "GEMINI_THINKING_LEVEL=minimal" in updated
    assert "ASK_COACH_MODEL=gemini-3.5-flash-lite" in updated
    assert "DAILY_INSIGHT_MODEL=gemini-3.5-flash-lite" in updated
    assert "GEMINI_TEMPERATURE" not in updated
    assert "gemini-2.5" not in updated
    assert "gemini-3.7" not in updated
    assert "GARMIN_EMAIL=test@example.com" in updated
    assert "GEMINI_API_KEY=secret-dummy-key" in updated


def test_check_and_fix_env_noop_when_clean(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "GARMIN_EMAIL=test@example.com\n"
        "GEMINI_API_KEY=secret-dummy-key\n"
        "LLM_PROVIDER=gemini\n"
        "GEMINI_MODEL=gemini-3.5-flash-lite\n"
        "GEMINI_THINKING_LEVEL=minimal\n"
        "ASK_COACH_MODEL=gemini-3.5-flash-lite\n"
        "ASK_COACH_THINKING_LEVEL=low\n"
        "DAILY_INSIGHT_MODEL=gemini-3.5-flash-lite\n"
        "DAILY_INSIGHT_THINKING_LEVEL=minimal\n",
        encoding="utf-8",
    )

    info = check_and_fix_env(env_file)
    assert info["env_found"] is True
    assert info["modifications_made"] is False
    assert info["backup_created"] is None
    assert len(info["stale_overrides_found"]) == 0


def test_verify_scheduler_and_outbox():
    sched = verify_scheduler_and_outbox()
    assert sched["cron_trigger_day_of_week"] == "mon-fri,sun"
    assert sched["cron_trigger_hour"] == 21
    assert sched["saturday_skipped"] is True
    assert sched["weekly_summary_day"] == "sat"
    assert sched["weekly_summary_hour"] == 20
    assert sched["stale_cutoff_hour"] == 22
