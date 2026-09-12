from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timezone
import json
from types import SimpleNamespace

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytest
from pydantic import ValidationError

import config
from coach.daily_insight import (
    generate_daily_coach_insight_for_date,
    reserve_daily_insight_claim,
    run_daily_coach_insight_job,
)
from coach.daily_insight_llm import (
    DailyInsightResponse,
    generate_daily_coach_insight,
    render_daily_insight_telegram_text,
)
import coach.daily_insight_llm as daily_llm
from coach.telegram_menu import (
    DAILY_INSIGHT_DISCLOSURE,
    daily_insight_markup,
    settings_markup,
)
from control_db import (
    DailyInsightConsent,
    User,
    get_ask_coach_consent,
    get_control_session,
    get_daily_insight_consent,
    init_control_db,
    is_daily_insight_authorized,
    record_ask_coach_consent,
    record_daily_insight_consent,
    revoke_ask_coach_consent,
    revoke_daily_insight_consent,
)
from db import NotificationOutbox
from notify.outbox import deliver_notification, process_due_notifications
from sync.scheduler import refresh_user_jobs, start_scheduler
from tenant_context import TenantIdentity, tenant_scope


class FakeInteractions:
    def __init__(self, outputs: list[str]):
        self.outputs = list(outputs)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.outputs:
            raise RuntimeError("No more fake responses")
        output = self.outputs.pop(0)
        return SimpleNamespace(output_text=output, status="completed")


class FakeClient:
    def __init__(self, outputs: list[str]):
        self.aio = SimpleNamespace(interactions=FakeInteractions(outputs))


def _sample_snapshot(local_day: str = "2026-07-05"):
    # 2026-07-05 is a Sunday
    return {
        "snapshot_version": "ask-coach-v3",
        "privacy_contract_version": "ask-coach-aggregate-context-v1",
        "generated_at": f"{local_day}T21:00:00Z",
        "timezone": "UTC",
        "date_context": {"local_day": local_day},
        "official_recommendation": {"status": "available", "action": "keep_workout"},
        "data_freshness": {},
        "profile": {"name": "Athlete"},
        "current_recovery": {},
        "training_aggregates": {"strength_highlights_14_days": {"items": []}},
        "recovery_trends_28_days": {"items": []},
        "slow_fitness_summary": {"items": []},
        "recent_activity_facts_7_days": {"items": []},
        "active_program": None,
        "planned_sessions_next_7_days": {"items": []},
    }


def _create_user_and_consents(user_id: str, *, timezone_str: str = "UTC") -> None:
    init_control_db()
    with get_control_session() as session:
        user = session.get(User, user_id)
        if user is None:
            user = User(
                id=user_id,
                email=f"{user_id}@example.com",
                status="active",
                role="athlete",
                timezone=timezone_str,
                telegram_linked=True,
            )
            session.add(user)
            session.flush()
    record_ask_coach_consent(
        user_id,
        config.ASK_COACH_CONSENT_VERSION,
        config.ASK_COACH_PROVIDER,
        config.ASK_COACH_DATA_CATEGORIES_VERSION,
        config.CURRENT_ASK_COACH_DATA_CATEGORIES,
    )
    record_daily_insight_consent(
        user_id,
        config.DAILY_INSIGHT_CONSENT_VERSION,
    )


# --- 1. Pydantic Schema & Mode Rendering Tests ---


def test_daily_insight_pydantic_schema_modes():
    modes = ["daily_insight", "tomorrow_preview", "progress_spotlight", "steady_state"]
    for mode in modes:
        resp = DailyInsightResponse(
            mode=mode,
            body="Factual observation about recovery and training.",
            action="Consider mobility work before bed." if mode != "steady_state" else None,
            basis_sections=["current_recovery", "training_aggregates"],
        )
        assert resp.mode == mode
        text = render_daily_insight_telegram_text(resp)
        assert "Factual observation" in text
        assert "*" not in text
        if resp.action:
            assert resp.action in text


def test_daily_insight_pydantic_schema_validation_failures():
    with pytest.raises(ValidationError):
        DailyInsightResponse(
            mode="unsupported_mode",
            body="Some text",
            basis_sections=["current_recovery"],
        )
    with pytest.raises(ValidationError):
        DailyInsightResponse(
            mode="daily_insight",
            body="   ",  # empty
            basis_sections=["current_recovery"],
        )


# --- 2. Scheduler Registration Tests ---


def test_scheduler_registers_daily_coach_insight_sun_fri(monkeypatch):
    sched = BackgroundScheduler(timezone="UTC")
    monkeypatch.setattr("sync.scheduler._scheduler", sched)
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)

    init_control_db()
    with get_control_session() as csession:
        user = User(
            id="user-sched-1",
            email="athlete@example.com",
            status="active",
            role="athlete",
            timezone="UTC",
            telegram_linked=True,
        )
        csession.add(user)

    refresh_user_jobs("user-sched-1")

    job = sched.get_job("user_user-sched-1_daily_coach_insight")
    assert job is not None
    trigger = job.trigger
    assert isinstance(trigger, CronTrigger)
    assert str(trigger.fields[4]) == "mon-fri,sun"  # day_of_week
    assert str(trigger.fields[5]) == "21"           # hour
    assert str(trigger.fields[6]) == "0"            # minute


def test_single_user_scheduler_registers_daily_coach_insight(monkeypatch):
    sched = BackgroundScheduler(timezone="UTC")
    monkeypatch.setattr("sync.scheduler._scheduler", None)
    monkeypatch.setattr("sync.scheduler.BackgroundScheduler", lambda **kw: sched)
    monkeypatch.setattr("time_utils.get_local_tz", lambda: timezone.utc)

    started = start_scheduler()
    job = started.get_job("daily_coach_insight")
    assert job is not None
    assert isinstance(job.trigger, CronTrigger)
    assert str(job.trigger.fields[4]) == "mon-fri,sun"
    assert str(job.trigger.fields[5]) == "21"
    assert str(job.trigger.fields[6]) == "0"
    started.shutdown(wait=False)


# --- 3. Consent Matrix & Authorization Tests ---


def test_daily_insight_authorization_matrix():
    init_control_db()
    user_id = "user-auth-1"

    # Create user in users table
    with get_control_session() as session:
        user = User(
            id=user_id,
            email=f"{user_id}@example.com",
            status="active",
            role="athlete",
            timezone="UTC",
            telegram_linked=True,
        )
        session.add(user)

    # 1. No consent at all
    assert not is_daily_insight_authorized(user_id)

    # 2. Only Ask Coach consent
    record_ask_coach_consent(
        user_id,
        config.ASK_COACH_CONSENT_VERSION,
        config.ASK_COACH_PROVIDER,
        config.ASK_COACH_DATA_CATEGORIES_VERSION,
        config.CURRENT_ASK_COACH_DATA_CATEGORIES,
    )
    assert not is_daily_insight_authorized(user_id)

    # 3. Both consents granted
    record_daily_insight_consent(user_id, config.DAILY_INSIGHT_CONSENT_VERSION)
    assert is_daily_insight_authorized(user_id)

    # 4. Daily insight consent revoked
    revoke_daily_insight_consent(user_id)
    assert not is_daily_insight_authorized(user_id)

    # 5. Re-granted
    record_daily_insight_consent(user_id, config.DAILY_INSIGHT_CONSENT_VERSION)
    assert is_daily_insight_authorized(user_id)

    # 6. Ask Coach consent revoked -> invalidates Daily Insight authorization
    revoke_ask_coach_consent(user_id)
    assert not is_daily_insight_authorized(user_id)


# --- 4. Saturday Skip Test ---


def test_daily_insight_skips_saturday(session, monkeypatch):
    user_id = "user-sat-1"
    _create_user_and_consents(user_id)

    gemini_called = []
    monkeypatch.setattr(
        "coach.daily_insight.generate_daily_coach_insight",
        lambda **kw: gemini_called.append(kw),
    )

    # 2026-07-11 is a Saturday
    saturday_snapshot = _sample_snapshot("2026-07-11")
    monkeypatch.setattr(
        "coach.daily_insight.build_advisory_snapshot",
        lambda *args, **kw: saturday_snapshot,
    )

    now_sat = datetime(2026, 7, 11, 21, 0, tzinfo=timezone.utc)
    res = generate_daily_coach_insight_for_date(session, user_id=user_id, now_utc=now_sat)

    assert res is False
    assert len(gemini_called) == 0
    rows = session.query(NotificationOutbox).all()
    assert len(rows) == 0


# --- 5. Atomic Reservation, Gemini Call & Idempotency ---


def test_atomic_reservation_and_idempotent_single_gemini_call(session, monkeypatch):
    user_id = "user-claim-1"
    _create_user_and_consents(user_id)

    sunday_snapshot = _sample_snapshot("2026-07-05")
    monkeypatch.setattr(
        "coach.daily_insight.build_advisory_snapshot",
        lambda *args, **kw: sunday_snapshot,
    )

    fake_response = DailyInsightResponse(
        mode="daily_insight",
        body="Solid recovery day with resting HR down 2 bpm.",
        action="Prep gear for tomorrow morning.",
        basis_sections=["current_recovery"],
    )
    gemini_calls = []

    async def fake_gemini(*args, **kw):
        gemini_calls.append(kw)
        return fake_response

    monkeypatch.setattr("coach.daily_insight.generate_daily_coach_insight", fake_gemini)

    now_sun = datetime(2026, 7, 5, 21, 0, tzinfo=timezone.utc)

    # 1. First run: successfully claims, calls Gemini once, transitions to pending
    success = generate_daily_coach_insight_for_date(session, user_id=user_id, now_utc=now_sun)
    assert success is True
    assert len(gemini_calls) == 1

    outbox_rows = session.query(NotificationOutbox).filter_by(event_type="daily_ai_insight").all()
    assert len(outbox_rows) == 1
    row = outbox_rows[0]
    assert row.status == "pending"
    assert row.idempotency_key == "daily-insight:2026-07-05"
    payload = json.loads(row.payload_json)
    assert payload["local_date"] == "2026-07-05"
    assert "Solid recovery day" in payload["text"]
    assert "untrusted_advisory_snapshot" not in payload
    assert "basis_sections" not in payload

    # 2. Second run on same day: idempotency skips Gemini
    second_run = generate_daily_coach_insight_for_date(session, user_id=user_id, now_utc=now_sun)
    assert second_run is False
    assert len(gemini_calls) == 1

    assert session.query(NotificationOutbox).filter_by(event_type="daily_ai_insight").count() == 1


def test_concurrent_claim_race_prevents_duplicate_spend(session, monkeypatch):
    user_id = "user-race-1"
    _create_user_and_consents(user_id)

    now_naive = datetime(2026, 7, 5, 21, 0)
    claim = NotificationOutbox(
        event_type="daily_ai_insight",
        due_at=now_naive,
        quiet_hour_policy="cancel",
        payload_json=json.dumps({"local_date": "2026-07-05", "text": ""}),
        status="generating",
        attempts=0,
        idempotency_key="daily-insight:2026-07-05",
        created_at=now_naive,
    )
    session.add(claim)
    session.commit()

    gemini_calls = []
    monkeypatch.setattr(
        "coach.daily_insight.generate_daily_coach_insight",
        lambda *args, **kw: gemini_calls.append(kw),
    )
    sunday_snapshot = _sample_snapshot("2026-07-05")
    monkeypatch.setattr(
        "coach.daily_insight.build_advisory_snapshot",
        lambda *args, **kw: sunday_snapshot,
    )

    now_sun = datetime(2026, 7, 5, 21, 1, tzinfo=timezone.utc)
    res = generate_daily_coach_insight_for_date(session, user_id=user_id, now_utc=now_sun)

    assert res is False
    assert len(gemini_calls) == 0


def test_gemini_failure_marks_claim_failed(session, monkeypatch):
    user_id = "user-fail-1"
    _create_user_and_consents(user_id)

    sunday_snapshot = _sample_snapshot("2026-07-05")
    monkeypatch.setattr(
        "coach.daily_insight.build_advisory_snapshot",
        lambda *args, **kw: sunday_snapshot,
    )

    async def failing_gemini(*args, **kw):
        raise daily_llm.DailyInsightLLMError("gemini_quota_exceeded")

    monkeypatch.setattr("coach.daily_insight.generate_daily_coach_insight", failing_gemini)

    now_sun = datetime(2026, 7, 5, 21, 0, tzinfo=timezone.utc)
    res = generate_daily_coach_insight_for_date(session, user_id=user_id, now_utc=now_sun)

    assert res is False
    row = session.query(NotificationOutbox).filter_by(idempotency_key="daily-insight:2026-07-05").first()
    assert row is not None
    assert row.status == "failed"
    assert row.last_error == "gemini_quota_exceeded"


# --- 6. Delivery & Staleness Cancellation Tests ---


def test_delivery_within_window_succeeds(session, monkeypatch):
    user_id = "user-deliv-1"
    _create_user_and_consents(user_id)

    now_dt = datetime(2026, 7, 5, 21, 5)
    row = NotificationOutbox(
        event_type="daily_ai_insight",
        due_at=datetime(2026, 7, 5, 21, 0),
        quiet_hour_policy="cancel",
        payload_json=json.dumps({
            "local_date": "2026-07-05",
            "text": "*Daily Coach Insight*\n\nRest up tonight.",
            "user_id": user_id,
        }),
        status="pending",
        attempts=0,
        idempotency_key="daily-insight:2026-07-05",
        created_at=datetime(2026, 7, 5, 21, 0),
    )
    session.add(row)
    session.commit()

    sent_messages = []
    monkeypatch.setattr(
        "notify.outbox.send_message",
        lambda text, reply_markup=None, parse_mode="Markdown": sent_messages.append(text) or True,
    )

    outcome = deliver_notification(session, row, now_dt)
    assert outcome == "sent"
    assert row.status == "sent"
    assert len(sent_messages) == 1
    assert "Rest up tonight" in sent_messages[0]


def test_delivery_cancelled_if_stale_after_2200(session, monkeypatch):
    user_id = "user-stale-1"
    _create_user_and_consents(user_id)

    attempt_time = datetime(2026, 7, 5, 22, 15)
    row = NotificationOutbox(
        event_type="daily_ai_insight",
        due_at=datetime(2026, 7, 5, 21, 0),
        quiet_hour_policy="cancel",
        payload_json=json.dumps({
            "local_date": "2026-07-05",
            "text": "*Daily Coach Insight*\n\nRest up tonight.",
            "user_id": user_id,
        }),
        status="pending",
        attempts=0,
        idempotency_key="daily-insight:2026-07-05",
        created_at=datetime(2026, 7, 5, 21, 0),
    )
    session.add(row)
    session.commit()

    sent_messages = []
    monkeypatch.setattr(
        "notify.outbox.send_message",
        lambda text, reply_markup=None, parse_mode="Markdown": sent_messages.append(text) or True,
    )

    outcome = deliver_notification(session, row, attempt_time)
    assert outcome == "cancelled"
    assert row.status == "cancelled"
    assert row.last_error == "stale_daily_insight"
    assert len(sent_messages) == 0


def test_delivery_cancelled_next_morning_never_deferred(session, monkeypatch):
    user_id = "user-morning-1"
    _create_user_and_consents(user_id)

    attempt_time = datetime(2026, 7, 6, 7, 15)
    row = NotificationOutbox(
        event_type="daily_ai_insight",
        due_at=datetime(2026, 7, 5, 21, 0),
        quiet_hour_policy="cancel",
        payload_json=json.dumps({
            "local_date": "2026-07-05",
            "text": "*Daily Coach Insight*\n\nRest up tonight.",
            "user_id": user_id,
        }),
        status="pending",
        attempts=0,
        idempotency_key="daily-insight:2026-07-05",
        created_at=datetime(2026, 7, 5, 21, 0),
    )
    session.add(row)
    session.commit()

    outcome = deliver_notification(session, row, attempt_time)
    assert outcome == "cancelled"
    assert row.status == "cancelled"
    assert row.last_error == "stale_daily_insight"


def test_delivery_cancelled_if_consent_revoked_before_delivery(session, monkeypatch):
    user_id = "user-rev-deliv-1"
    _create_user_and_consents(user_id)

    now_dt = datetime(2026, 7, 5, 21, 5)
    row = NotificationOutbox(
        event_type="daily_ai_insight",
        due_at=datetime(2026, 7, 5, 21, 0),
        quiet_hour_policy="cancel",
        payload_json=json.dumps({
            "local_date": "2026-07-05",
            "text": "*Daily Coach Insight*\n\nRest up tonight.",
            "user_id": user_id,
        }),
        status="pending",
        attempts=0,
        idempotency_key="daily-insight:2026-07-05",
        created_at=datetime(2026, 7, 5, 21, 0),
    )
    session.add(row)
    session.commit()

    # User revokes Ask Coach consent before outbox delivery runs
    revoke_ask_coach_consent(user_id)

    outcome = deliver_notification(session, row, now_dt)
    assert outcome == "cancelled"
    assert row.status == "cancelled"
    assert row.last_error == "revalidation_failed"


# --- 7. Telegram Menu & Webhook Callback Tests ---


def test_telegram_settings_markup_includes_daily_insight():
    markup = settings_markup()
    buttons = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
    assert "menu:daily_insight" in buttons


def test_daily_insight_markup_toggle_states():
    markup_off = daily_insight_markup(is_opted_in=False)
    assert markup_off["inline_keyboard"][0][0]["callback_data"] == "insight:enable"
    assert "Enable" in markup_off["inline_keyboard"][0][0]["text"]

    markup_on = daily_insight_markup(is_opted_in=True)
    assert markup_on["inline_keyboard"][0][0]["callback_data"] == "insight:disable"
    assert "Disable" in markup_on["inline_keyboard"][0][0]["text"]


def test_telegram_webhook_daily_insight_enable_disable_flow(monkeypatch):
    from coach.telegram_webhook import handle_telegram_update
    from control_db import IntegrationRoute, utcnow
    from secret_vault import UserSecretVault
    from telegram_link import _chat_hmac

    init_control_db()
    user_id = "00000000-0000-0000-0000-000000000099"
    chat_id = "998877"

    with get_control_session() as csession:
        user = User(
            id=user_id,
            email="athlete-webhook@example.com",
            status="active",
            role="athlete",
            timezone="UTC",
            telegram_linked=True,
        )
        csession.add(user)
        csession.flush()
        route = IntegrationRoute(user_id=user_id)
        route.telegram_chat_hmac = _chat_hmac(chat_id)
        route.updated_at = utcnow()
        csession.add(route)
    UserSecretVault().update(user_id, telegram_chat_id=chat_id)

    edits = []
    messages = []
    monkeypatch.setattr("notify.telegram.answer_callback_query", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("notify.telegram.edit_message_text", lambda text, *_args, **_kwargs: edits.append(text) or True)
    monkeypatch.setattr("notify.telegram.send_message", lambda text, *_args, **_kwargs: messages.append(text) or True)

    def _cb(data, update_id=1):
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cb-{update_id}",
                "data": data,
                "message": {"message_id": 42, "chat": {"id": int(chat_id), "type": "private"}},
            },
        }

    # 1. Attempt to enable Daily Insight without Ask Coach consent
    asyncio.run(handle_telegram_update(_cb("insight:enable", 100)))
    assert len(edits) == 1
    assert "requires active Ask Coach AI consent" in edits[0]
    assert not is_daily_insight_authorized(user_id)

    # 2. Grant Ask Coach consent
    record_ask_coach_consent(
        user_id,
        config.ASK_COACH_CONSENT_VERSION,
        config.ASK_COACH_PROVIDER,
        config.ASK_COACH_DATA_CATEGORIES_VERSION,
        config.CURRENT_ASK_COACH_DATA_CATEGORIES,
    )

    # 3. Enable Daily Insight with valid Ask Coach consent
    asyncio.run(handle_telegram_update(_cb("insight:enable", 101)))
    assert len(edits) == 2
    assert "Daily Coach Insight has been enabled" in edits[1]
    assert is_daily_insight_authorized(user_id)

    # 4. View menu:daily_insight status
    asyncio.run(handle_telegram_update(_cb("menu:daily_insight", 102)))
    assert len(messages) >= 1
    assert "Daily Coach Insight (AI)" in messages[-1]
    assert "Status: *Enabled*" in messages[-1]

    # 5. Disable Daily Insight
    asyncio.run(handle_telegram_update(_cb("insight:disable", 103)))
    assert len(edits) == 3
    assert "Daily Coach Insight has been disabled" in edits[2]
    assert not is_daily_insight_authorized(user_id)

    # 6. Re-enable, then revoke Ask Coach consent -> Daily Insight also revoked
    asyncio.run(handle_telegram_update(_cb("insight:enable", 104)))
    assert is_daily_insight_authorized(user_id)
    asyncio.run(handle_telegram_update(_cb("ask:consent_revoke", 105)))
    assert not is_daily_insight_authorized(user_id)
    assert get_daily_insight_consent(user_id).revoked_at is not None


# --- 8. Multi-User Scheduled Job Authorization Gates ---


def test_multi_user_daily_insight_authorization_gates_generation(session, monkeypatch):
    monkeypatch.setattr(config, "MULTI_USER_ENABLED", True)
    init_control_db()

    user_id = "00000000-0000-0000-0000-000000000002"
    with get_control_session() as csession:
        user = User(
            id=user_id,
            email="athlete-gate@example.com",
            status="active",
            role="athlete",
            timezone="UTC",
            telegram_linked=True,
        )
        csession.add(user)

    snapshot_calls = []
    gemini_calls = []

    def fake_snapshot(*args, **kwargs):
        snapshot_calls.append(kwargs)
        return _sample_snapshot("2026-07-05")

    async def fake_gemini(*args, **kwargs):
        gemini_calls.append(kwargs)
        return DailyInsightResponse(
            mode="daily_insight",
            body="Good recovery score.",
            action="Maintain current hydration.",
            basis_sections=["current_recovery"],
        )

    monkeypatch.setattr("coach.daily_insight.build_advisory_snapshot", fake_snapshot)
    monkeypatch.setattr("coach.daily_insight.generate_daily_coach_insight", fake_gemini)

    now_sun = datetime(2026, 7, 5, 21, 0, tzinfo=timezone.utc)

    # 1. Active + linked + no consent at all -> False, 0 snapshot calls, 0 gemini calls, 0 outbox rows
    res = run_daily_coach_insight_job(user_id=user_id, now_utc=now_sun, session=session)
    assert res is False
    assert len(snapshot_calls) == 0
    assert len(gemini_calls) == 0
    assert session.query(NotificationOutbox).count() == 0

    # 2. Active + linked + Ask Coach consent only -> False, 0 snapshot calls, 0 gemini calls, 0 outbox rows
    record_ask_coach_consent(
        user_id,
        config.ASK_COACH_CONSENT_VERSION,
        config.ASK_COACH_PROVIDER,
        config.ASK_COACH_DATA_CATEGORIES_VERSION,
        config.CURRENT_ASK_COACH_DATA_CATEGORIES,
    )
    res = run_daily_coach_insight_job(user_id=user_id, now_utc=now_sun, session=session)
    assert res is False
    assert len(snapshot_calls) == 0
    assert len(gemini_calls) == 0
    assert session.query(NotificationOutbox).count() == 0

    # 3. Active + linked + both consents -> True, 1 snapshot call, 1 gemini call, 1 pending row
    record_daily_insight_consent(user_id, config.DAILY_INSIGHT_CONSENT_VERSION)
    res = run_daily_coach_insight_job(user_id=user_id, now_utc=now_sun, session=session)
    assert res is True
    assert len(snapshot_calls) == 1
    assert len(gemini_calls) == 1
    rows = session.query(NotificationOutbox).filter_by(event_type="daily_ai_insight").all()
    assert len(rows) == 1
    assert rows[0].status == "pending"

    # 4. Revoked Ask Coach consent on next day -> False, 0 additional snapshot/gemini calls
    revoke_ask_coach_consent(user_id)
    now_mon = datetime(2026, 7, 6, 21, 0, tzinfo=timezone.utc)
    def fake_snapshot_mon(*args, **kwargs):
        snapshot_calls.append(kwargs)
        return _sample_snapshot("2026-07-06")
    monkeypatch.setattr("coach.daily_insight.build_advisory_snapshot", fake_snapshot_mon)
    res = run_daily_coach_insight_job(user_id=user_id, now_utc=now_mon, session=session)
    assert res is False
    assert len(snapshot_calls) == 1
    assert len(gemini_calls) == 1

    # 5. Inactive user (even if consents re-granted) -> False
    record_ask_coach_consent(
        user_id,
        config.ASK_COACH_CONSENT_VERSION,
        config.ASK_COACH_PROVIDER,
        config.ASK_COACH_DATA_CATEGORIES_VERSION,
        config.CURRENT_ASK_COACH_DATA_CATEGORIES,
    )
    with get_control_session() as csession:
        u = csession.get(User, user_id)
        u.status = "suspended"
        csession.commit()
    now_tue = datetime(2026, 7, 7, 21, 0, tzinfo=timezone.utc)
    res = run_daily_coach_insight_job(user_id=user_id, now_utc=now_tue, session=session)
    assert res is False
    assert len(snapshot_calls) == 1
    assert len(gemini_calls) == 1

    # 6. Unlinked Telegram -> False
    with get_control_session() as csession:
        u = csession.get(User, user_id)
        u.status = "active"
        u.telegram_linked = False
        csession.commit()
    res = run_daily_coach_insight_job(user_id=user_id, now_utc=now_tue, session=session)
    assert res is False
    assert len(snapshot_calls) == 1
    assert len(gemini_calls) == 1


# --- 9. Plain-Text Delivery and Disclosure Tests ---


def test_plain_text_delivery_handles_markdown_characters(session, monkeypatch):
    user_id = "user-md-chars-1"
    _create_user_and_consents(user_id)

    tricky_text = "🧠 Daily Coach Insight\n\nIntervals [4x400m] @ 5:00_pace (target: HR > 160 & *intensity*)."
    row = NotificationOutbox(
        event_type="daily_ai_insight",
        due_at=datetime(2026, 7, 5, 21, 0),
        quiet_hour_policy="cancel",
        payload_json=json.dumps({
            "local_date": "2026-07-05",
            "text": tricky_text,
            "user_id": user_id,
        }),
        status="pending",
        attempts=0,
        idempotency_key="daily-insight:2026-07-05",
        created_at=datetime(2026, 7, 5, 21, 0),
    )
    session.add(row)
    session.commit()

    captured_calls = []

    def fake_send(text, reply_markup=None, parse_mode="Markdown"):
        captured_calls.append({"text": text, "reply_markup": reply_markup, "parse_mode": parse_mode})
        return True

    monkeypatch.setattr("notify.outbox.send_message", fake_send)

    now_dt = datetime(2026, 7, 5, 21, 5)
    outcome = deliver_notification(session, row, now_dt)
    assert outcome == "sent"
    assert len(captured_calls) == 1
    assert captured_calls[0]["parse_mode"] is None
    assert captured_calls[0]["text"] == tricky_text


def test_daily_insight_disclosure_content():
    assert "store=False" in DAILY_INSIGHT_DISCLOSURE
    assert "Interactions API does not store" in DAILY_INSIGHT_DISCLOSURE
    assert "applicable terms" in DAILY_INSIGHT_DISCLOSURE
    assert "Google Gemini" in DAILY_INSIGHT_DISCLOSURE

