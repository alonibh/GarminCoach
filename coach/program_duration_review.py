"""Neutral, durable source-duration review prompts.

This module is deliberately local-only: its eligibility facts do not write or
call external services, and its durable transitions do not alter training
programs, cursors, planned sessions, Garmin state, or progression state.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from typing import Iterable

from sqlalchemy import and_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from coach.program_policy import PROGRAM_POLICIES, ProgramPolicy
from db import (
    Activity,
    ActivityProgramMatch,
    NotificationOutbox,
    ProgramCursor,
    ProgramDurationReview,
    ProgramSession,
    TrainingProgram,
)
from time_utils import get_local_tz


ACTIVE_STATUSES = ("scheduled", "pending", "snoozed")
REVIEW_STATUSES = frozenset((*ACTIVE_STATUSES, "resolved", "superseded"))
REVIEW_DECISIONS = frozenset(("continue_unchanged", "deload_planned"))
_MAX_DURATION_WEEKS = 52


@dataclass(frozen=True)
class ProgramDurationReviewFacts:
    program_id: int
    program_key: str
    program_name: str
    policy_version: str
    source_duration_weeks: int
    activation_utc: datetime
    activated_local_date: date
    due_on: date
    ordered_source_session_ids: tuple[int, ...]
    ordered_source_session_names: tuple[str, ...]
    fingerprint: str
    source_url: str


@dataclass(frozen=True)
class ProgramDurationReviewContext:
    matched_source_sessions: int
    source_session_count: int
    completed_source_cycles: int
    remaining_matches_in_current_cycle: int
    next_session_name: str | None


def _program_key(program: TrainingProgram) -> str | None:
    try:
        values = json.loads(program.goal_tags or "[]")
    except (TypeError, ValueError):
        return None
    if not isinstance(values, list) or len(values) != 1 or not isinstance(values[0], str):
        return None
    key = values[0]
    return key if key in PROGRAM_POLICIES else None


def _valid_duration(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= _MAX_DURATION_WEEKS:
        return None
    return value


def _activation_utc(value: datetime | None) -> datetime | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        # SQLite returns naïve datetimes; GarminCoach stores these as UTC.
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _activation_anchor(session: Session, program: TrainingProgram) -> datetime | None:
    anchor = _activation_utc(program.activated_at)
    if anchor is not None:
        return anchor
    key = _program_key(program)
    policy = PROGRAM_POLICIES.get(key or "")
    cursor = session.get(ProgramCursor, program.id)
    if cursor is None or policy is None or cursor.policy_version != policy.version:
        return None
    # A cursor belongs to this exact program through its primary key. It is only
    # a fallback for old activations that predate TrainingProgram.activated_at.
    return _activation_utc(cursor.created_at)


def _source_sessions(session: Session, program: TrainingProgram, policy: ProgramPolicy) -> list[ProgramSession] | None:
    rows = (
        session.query(ProgramSession)
        .filter(ProgramSession.program_id == program.id)
        .order_by(ProgramSession.sequence_order, ProgramSession.id)
        .all()
    )
    source = [row for row in rows if not row.is_custom]
    if len(source) != len(policy.session_names):
        return None
    if tuple(row.name for row in source) != policy.session_names:
        return None
    if any(row.session_role != "coach_strength" or row.is_addon for row in source):
        return None
    return source


def _fingerprint(*, program: TrainingProgram, key: str, policy: ProgramPolicy,
                 activation_utc: datetime, source: Iterable[ProgramSession]) -> str:
    payload = {
        "program_id": program.id,
        "program_key": key,
        "policy_version": policy.version,
        "source_duration_weeks": policy.source_duration_weeks,
        "activation_utc": activation_utc.isoformat(timespec="microseconds") + "Z",
        "source_sessions": [
            {"id": row.id, "name": row.name, "role": row.session_role,
             "is_custom": bool(row.is_custom), "is_addon": bool(row.is_addon)}
            for row in source
        ],
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def build_program_duration_review_facts(session: Session, program: TrainingProgram | None) -> ProgramDurationReviewFacts | None:
    """Build deterministic eligibility facts without writes or external work."""
    if program is None or not program.active or program.status != "active" or program.source_type != "curated_archetype":
        return None
    key = _program_key(program)
    policy = PROGRAM_POLICIES.get(key or "")
    duration = _valid_duration(policy.source_duration_weeks if policy else None)
    if key is None or policy is None or duration is None or program.source_url != policy.source_url:
        return None
    source = _source_sessions(session, program, policy)
    activation_utc = _activation_anchor(session, program)
    if source is None or activation_utc is None:
        return None
    activated_local_date = activation_utc.replace(tzinfo=timezone.utc).astimezone(get_local_tz()).date()
    fingerprint = _fingerprint(program=program, key=key, policy=policy,
                               activation_utc=activation_utc, source=source)
    return ProgramDurationReviewFacts(
        program_id=program.id,
        program_key=key,
        program_name=" ".join((program.name or "").split())[:255] or key,
        policy_version=policy.version,
        source_duration_weeks=duration,
        activation_utc=activation_utc,
        activated_local_date=activated_local_date,
        due_on=activated_local_date + timedelta(weeks=duration),
        ordered_source_session_ids=tuple(row.id for row in source),
        ordered_source_session_names=tuple(row.name for row in source),
        fingerprint=fingerprint,
        source_url=policy.source_url,
    )


def _active_program(session: Session) -> TrainingProgram | None:
    rows = session.query(TrainingProgram).filter(
        TrainingProgram.active.is_(True), TrainingProgram.status == "active"
    ).order_by(TrainingProgram.id).all()
    return rows[0] if len(rows) == 1 else None


def _supersede_rows(session: Session, rows: Iterable[ProgramDurationReview], now_utc: datetime) -> None:
    for row in rows:
        if row.status in ACTIVE_STATUSES:
            row.status = "superseded"
            row.updated_at = now_utc
            row.superseded_at = now_utc


def reconcile_program_duration_review(session: Session, *, local_today: date, now_utc: datetime) -> ProgramDurationReview | None:
    """Reconcile one active curated activation, preserving all prior review history."""
    now_utc = _activation_utc(now_utc) or datetime.utcnow()
    program = _active_program(session)
    facts = build_program_duration_review_facts(session, program)
    active_rows = session.query(ProgramDurationReview).filter(
        ProgramDurationReview.status.in_(ACTIVE_STATUSES)
    ).all()
    if facts is None:
        _supersede_rows(session, active_rows, now_utc)
        return None
    _supersede_rows(session, (row for row in active_rows if row.review_fingerprint != facts.fingerprint), now_utc)
    row = session.query(ProgramDurationReview).filter_by(review_fingerprint=facts.fingerprint).one_or_none()
    if row is None:
        is_due = local_today >= facts.due_on
        values = dict(
            program_id=facts.program_id, program_id_snapshot=facts.program_id,
            program_name_snapshot=facts.program_name, program_key=facts.program_key,
            policy_version=facts.policy_version, source_duration_weeks=facts.source_duration_weeks,
            activated_at_snapshot=facts.activation_utc, activated_local_date=facts.activated_local_date,
            due_on=facts.due_on, source_session_count=len(facts.ordered_source_session_ids),
            review_fingerprint=facts.fingerprint, idempotency_key=f"program-duration-review:{facts.fingerprint}",
            status="pending" if is_due else "scheduled", reminder_sequence=0,
            created_at=now_utc, updated_at=now_utc, first_due_at=now_utc if is_due else None,
        )
        try:
            with session.begin_nested():
                row = ProgramDurationReview(**values)
                session.add(row)
                session.flush()
        except IntegrityError:
            # The unique key is the concurrency boundary for repeated jobs.
            row = session.query(ProgramDurationReview).filter_by(review_fingerprint=facts.fingerprint).one_or_none()
            if row is None:
                raise
    if row.status == "scheduled" and local_today >= row.due_on:
        row.status = "pending"
        row.first_due_at = row.first_due_at or now_utc
        row.updated_at = now_utc
    elif row.status == "snoozed" and row.snooze_until is not None and local_today >= row.snooze_until:
        row.status = "pending"
        row.updated_at = now_utc
    return row


def enqueue_due_program_duration_review_notifications(session: Session, *, local_today: date, now_utc: datetime) -> int:
    """Bridge currently pending reviews into the existing outbox, once per sequence."""
    from notify.outbox import enqueue_notification

    review = reconcile_program_duration_review(session, local_today=local_today, now_utc=now_utc)
    if review is None or review.status != "pending":
        return 0
    key = f"program-duration-review:{review.id}:sequence:{review.reminder_sequence}"
    existing = session.query(NotificationOutbox).filter_by(idempotency_key=key).first()
    if existing is not None:
        return 0
    enqueue_notification(
        session,
        event_type="program_duration_review",
        due_at=now_utc,
        payload={"review_id": review.id, "reminder_sequence": review.reminder_sequence,
                 "review_fingerprint": review.review_fingerprint},
        idempotency_key=key,
    )
    return 1


def materialize_program_duration_review_notification(session: Session, *, review_id: object,
                                                      reminder_sequence: object,
                                                      fingerprint: object) -> tuple[str, None, None] | None:
    if isinstance(review_id, bool) or not isinstance(review_id, int) or isinstance(reminder_sequence, bool) or not isinstance(reminder_sequence, int):
        return None
    row = session.get(ProgramDurationReview, review_id)
    if row is None or row.status != "pending" or row.reminder_sequence != reminder_sequence or row.review_fingerprint != fingerprint:
        return None
    facts = build_program_duration_review_facts(session, _active_program(session))
    if facts is None or facts.fingerprint != row.review_fingerprint:
        return None
    name = " ".join(row.program_name_snapshot.split())[:120]
    return (
        f"Time to review {name}.\n"
        f"The routine's reviewed source duration is {row.source_duration_weeks} weeks.\n"
        "Elapsed time does not mean the program is complete or that a deload is required.\n"
        "Review it in the web app.",
        None,
        None,
    )


def record_program_duration_review_delivery(session: Session, *, review_id: object,
                                             reminder_sequence: object, now_utc: datetime) -> None:
    if isinstance(review_id, bool) or not isinstance(review_id, int) or isinstance(reminder_sequence, bool) or not isinstance(reminder_sequence, int):
        return
    row = session.get(ProgramDurationReview, review_id)
    if row and row.status == "pending" and row.reminder_sequence == reminder_sequence:
        row.notified_at = _activation_utc(now_utc) or datetime.utcnow()
        row.updated_at = row.notified_at


def completion_context(session: Session, review: ProgramDurationReview) -> ProgramDurationReviewContext:
    facts = build_program_duration_review_facts(session, _active_program(session))
    if facts is None or facts.fingerprint != review.review_fingerprint:
        return ProgramDurationReviewContext(0, review.source_session_count, 0, 0, None)
    count = session.query(ActivityProgramMatch).join(
        Activity, Activity.id == ActivityProgramMatch.activity_id
    ).filter(
        ActivityProgramMatch.program_id == facts.program_id,
        ActivityProgramMatch.program_session_id.in_(facts.ordered_source_session_ids),
        Activity.start_time >= facts.activation_utc,
    ).count()
    next_name = None
    cursor = session.get(ProgramCursor, facts.program_id)
    if cursor and cursor.next_program_session_id in facts.ordered_source_session_ids:
        next_name = session.get(ProgramSession, cursor.next_program_session_id).name
    return ProgramDurationReviewContext(
        matched_source_sessions=count,
        source_session_count=review.source_session_count,
        completed_source_cycles=count // review.source_session_count,
        remaining_matches_in_current_cycle=count % review.source_session_count,
        next_session_name=next_name,
    )


def pending_program_duration_review_card(session: Session) -> tuple[ProgramDurationReview, ProgramDurationReviewContext, ProgramDurationReviewFacts] | None:
    review = session.query(ProgramDurationReview).filter_by(status="pending").order_by(ProgramDurationReview.id.desc()).first()
    if review is None:
        return None
    facts = build_program_duration_review_facts(session, _active_program(session))
    if facts is None or facts.fingerprint != review.review_fingerprint:
        return None
    return review, completion_context(session, review), facts


def apply_program_duration_review_action(session: Session, *, review_id: int, fingerprint: str,
                                         action: str, local_today: date, now_utc: datetime) -> str:
    """Apply one web-only acknowledgement or snooze with current-state validation."""
    if action not in {"continue_unchanged", "deload_planned", "snooze"}:
        return "invalid"
    review = session.get(ProgramDurationReview, review_id)
    if review is None or review.review_fingerprint != fingerprint:
        return "stale"
    facts = build_program_duration_review_facts(session, _active_program(session))
    if facts is None or facts.fingerprint != review.review_fingerprint:
        return "stale"
    now_utc = _activation_utc(now_utc) or datetime.utcnow()
    if action == "snooze":
        if review.status == "snoozed":
            return "already"
        if review.status != "pending":
            return "stale"
        review.status = "snoozed"
        review.snooze_until = local_today.fromordinal(local_today.toordinal() + 7)
        review.reminder_sequence += 1
        review.notified_at = None
        review.updated_at = now_utc
        return "applied"
    if review.status == "resolved" and review.decision == action:
        return "already"
    if review.status != "pending":
        return "stale"
    review.status = "resolved"
    review.decision = action
    review.resolved_at = now_utc
    review.updated_at = now_utc
    return "applied"
