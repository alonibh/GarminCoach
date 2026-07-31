"""Durable, local notification intent for material strength proposals.

This module owns no transport and never commits.  The normal notification
outbox remains the only path that can talk to Telegram.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Collection

from sqlalchemy.orm import Session

from coach.strength_progression import fingerprint
from coach.strength_progression_actions import (
    format_weight_grams, revalidate_progression_proposals_for_notification,
)
from coach.strength_progression_integration import MaterialProposalChange
from db import (
    StrengthProgressionNotificationBatch, StrengthProgressionNotificationReceipt,
)
from notify.outbox import enqueue_notification

_PAYLOAD_VERSION = "v1"
_EVENT_TYPE = "strength_progression_ready"


@dataclass(frozen=True)
class NotificationRecordResult:
    batch_id: str | None
    receipts_created: int
    ignored: int


@dataclass(frozen=True)
class NotificationBridgeReport:
    bridged_batch_ids: tuple[str, ...]
    reused_batch_ids: tuple[str, ...]


@dataclass(frozen=True)
class ProgressionNotificationMaterialization:
    text: str
    parse_mode: str | None = None
    reply_markup: object | None = None


def _batch_fingerprint(boundary_id: str, material_fingerprints: Collection[str]) -> str:
    return fingerprint({"event_type": _EVENT_TYPE, "boundary_id": boundary_id,
                        "materials": sorted(material_fingerprints), "payload_version": _PAYLOAD_VERSION})


def record_material_proposals(
    session: Session, *, boundary_id: str, changes: Collection[MaterialProposalChange], now: datetime,
) -> NotificationRecordResult:
    """Record one boundary's new material facts, with global receipt deduplication."""
    ordered = sorted({change.material_fingerprint: change for change in changes}.values(),
                     key=lambda item: (item.material_fingerprint, item.proposal_id))
    if not ordered:
        return NotificationRecordResult(None, 0, 0)
    known = {
        value for (value,) in session.query(StrengthProgressionNotificationReceipt.material_fingerprint)
        .filter(StrengthProgressionNotificationReceipt.material_fingerprint.in_([item.material_fingerprint for item in ordered]))
        .all()
    }
    fresh = [item for item in ordered if item.material_fingerprint not in known]
    if not fresh:
        existing = session.query(StrengthProgressionNotificationBatch).filter_by(boundary_id=boundary_id).one_or_none()
        return NotificationRecordResult(existing.batch_id if existing else None, 0, len(ordered))
    batch_fingerprint = _batch_fingerprint(boundary_id, [item.material_fingerprint for item in fresh])
    batch = session.query(StrengthProgressionNotificationBatch).filter_by(boundary_id=boundary_id).one_or_none()
    if batch is None:
        batch = StrengthProgressionNotificationBatch(
            batch_id=fingerprint({"kind": "strength_progression_notification_batch", "boundary_id": boundary_id,
                                  "batch_fingerprint": batch_fingerprint}),
            boundary_id=boundary_id, batch_fingerprint=batch_fingerprint, payload_version=_PAYLOAD_VERSION,
            status="pending_outbox", proposal_count=len(fresh), created_at=now,
        )
        session.add(batch)
    else:
        # A completed deterministic boundary cannot acquire unrelated facts.
        return NotificationRecordResult(batch.batch_id, 0, len(ordered))
    for change in fresh:
        session.add(StrengthProgressionNotificationReceipt(
            receipt_id=fingerprint({"kind": "strength_progression_notification_receipt",
                                    "proposal_id": change.proposal_id,
                                    "material_fingerprint": change.material_fingerprint}),
            batch_id=batch.batch_id, proposal_id=change.proposal_id,
            proposal_id_snapshot=change.proposal_id, material_fingerprint=change.material_fingerprint,
            created_at=now,
        ))
    session.flush()
    return NotificationRecordResult(batch.batch_id, len(fresh), len(ordered) - len(fresh))


def bridge_pending_progression_notifications(
    session: Session, *, now: datetime, limit: int = 25, batch_ids: Collection[str] | None = None,
) -> NotificationBridgeReport:
    """Convert durable intent into ordinary outbox jobs; never sends or commits."""
    if limit < 1:
        return NotificationBridgeReport((), ())
    query = session.query(StrengthProgressionNotificationBatch).filter_by(status="pending_outbox")
    if batch_ids is not None:
        ids = sorted(set(batch_ids))
        if not ids:
            return NotificationBridgeReport((), ())
        query = query.filter(StrengthProgressionNotificationBatch.batch_id.in_(ids))
    batches = query.order_by(StrengthProgressionNotificationBatch.created_at, StrengthProgressionNotificationBatch.batch_id).limit(limit).all()
    bridged: list[str] = []
    reused: list[str] = []
    for batch in batches:
        receipts = (session.query(StrengthProgressionNotificationReceipt)
                    .filter_by(batch_id=batch.batch_id)
                    .order_by(StrengthProgressionNotificationReceipt.material_fingerprint).all())
        if not receipts:
            batch.status, batch.terminal_at, batch.terminal_reason = "cancelled", now, "empty_batch"
            continue
        key = _batch_fingerprint(batch.boundary_id, [receipt.material_fingerprint for receipt in receipts])
        row = enqueue_notification(session, event_type=_EVENT_TYPE, due_at=now,
            payload={"batch_id": batch.batch_id, "payload_version": _PAYLOAD_VERSION},
            idempotency_key=key, quiet_hour_policy="defer")
        if batch.outbox_id == row.id and batch.status == "queued":
            reused.append(batch.batch_id)
            continue
        batch.status, batch.outbox_id, batch.outbox_id_snapshot, batch.queued_at = "queued", row.id, row.id, now
        bridged.append(batch.batch_id)
    return NotificationBridgeReport(tuple(bridged), tuple(reused))


def _safe_name(value: str) -> str:
    cleaned = " ".join((value or "Exercise").split())
    return cleaned[:79] + "…" if len(cleaned) > 80 else cleaned


def materialize_progression_summary(
    session: Session, *, batch_id: str, now: datetime,
) -> ProgressionNotificationMaterialization | None:
    batch = session.get(StrengthProgressionNotificationBatch, batch_id)
    if batch is None or batch.status not in {"queued", "pending_outbox"}:
        return None
    proposal_ids = tuple(receipt.proposal_id_snapshot for receipt in
                         session.query(StrengthProgressionNotificationReceipt)
                         .filter_by(batch_id=batch_id)
                         .order_by(StrengthProgressionNotificationReceipt.material_fingerprint))
    facts = revalidate_progression_proposals_for_notification(session, proposal_ids, now=now)
    if not facts:
        batch.status, batch.terminal_at, batch.terminal_reason = "cancelled", now, "no_actionable_proposals"
        return None
    count = len(facts)
    lines = ["Strength progression ready", "", f"{count} weight proposal{' is' if count == 1 else 's are'} ready for review:"]
    for fact in facts[:8]:
        lines.append(f"• {_safe_name(fact.exercise_name)}: {format_weight_grams(fact.current_weight_grams)} → {format_weight_grams(fact.suggested_weight_grams)}")
    if count > 8:
        lines.append(f"• +{count - 8} more")
    lines.extend(("", "Open GarminCoach → Progression to review each proposal.",
                  "Decisions are web-only. Already scheduled workouts are unchanged."))
    # Hostile names are bounded above; keep a final hard bound as Telegram evolves.
    return ProgressionNotificationMaterialization("\n".join(lines)[:4000], parse_mode=None, reply_markup=None)


def reconcile_progression_notification_outcome(
    session: Session, *, outbox_id: int, outcome: str, now: datetime, reason: str | None = None,
) -> None:
    batch = session.query(StrengthProgressionNotificationBatch).filter_by(outbox_id=outbox_id).one_or_none()
    if batch is None or outcome not in {"sent", "cancelled", "failed"}:
        return
    batch.status, batch.terminal_at, batch.terminal_reason = outcome, now, (reason or outcome)[:64]
