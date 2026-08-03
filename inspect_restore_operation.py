"""Read-only restore-operation inspection CLI (Phase 6B3A).

Inspects an existing guarded restore journal from the configured restore-operation
root, reporting its bounded operational facts, current stage, per-target durable
state, mutation evidence, and safety assessment without mutating or continuing any operation.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sys

import config
from guarded_restore import (
    EXIT_FAILED_SAFE,
    EXIT_INVALID_ARGUMENTS,
    EXIT_INVALID_OPERATION,
    EXIT_MANUAL_RECOVERY_REQUIRED,
    EXIT_OPERATION_IN_PROGRESS,
    EXIT_PREPARATION_INCOMPLETE,
    EXIT_ROLLBACK_REQUIRED,
    EXIT_SUCCESS,
    EXIT_UNEXPECTED_FAILURE,
    RestoreJournal,
    RestoreJournalError,
    RestoreStage,
    TargetRestoreState,
    _OPERATION_ID,
    load_restore_journal,
    validate_restore_root,
)


@dataclass(frozen=True)
class _StageMetadata:
    assessment: str
    exit_code: int
    terminal: bool
    postcheck_required: bool
    automatic_reentry_required: bool
    manual_recovery_required: bool
    static_description: str | None = None


_STAGE_METADATA: dict[RestoreStage, _StageMetadata] = {
    RestoreStage.COMPLETED: _StageMetadata(
        assessment="completed",
        exit_code=EXIT_SUCCESS,
        terminal=True,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=False,
        static_description="Restore operation completed successfully",
    ),
    RestoreStage.REPLACEMENT_READY: _StageMetadata(
        assessment="ready_for_replacement",
        exit_code=EXIT_SUCCESS,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=False,
        static_description="Staging and pre-replacement verification completed; ready for future apply",
    ),
    RestoreStage.PRECHECK: _StageMetadata(
        assessment="preparation_incomplete",
        exit_code=EXIT_PREPARATION_INCOMPLETE,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=False,
        static_description="Operation is in stage PRECHECK; apply orchestration has not reached REPLACEMENT_READY",
    ),
    RestoreStage.VERIFIED: _StageMetadata(
        assessment="preparation_incomplete",
        exit_code=EXIT_PREPARATION_INCOMPLETE,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=False,
        static_description="Operation is in stage VERIFIED; apply orchestration has not reached REPLACEMENT_READY",
    ),
    RestoreStage.CURRENT_SNAPSHOT_CREATED: _StageMetadata(
        assessment="preparation_incomplete",
        exit_code=EXIT_PREPARATION_INCOMPLETE,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=False,
        static_description="Operation is in stage CURRENT_SNAPSHOT_CREATED; apply orchestration has not reached REPLACEMENT_READY",
    ),
    RestoreStage.RESTORE_STAGED: _StageMetadata(
        assessment="preparation_incomplete",
        exit_code=EXIT_PREPARATION_INCOMPLETE,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=False,
        static_description="Operation is in stage RESTORE_STAGED; apply orchestration has not reached REPLACEMENT_READY",
    ),
    RestoreStage.STAGED_VERIFIED: _StageMetadata(
        assessment="preparation_incomplete",
        exit_code=EXIT_PREPARATION_INCOMPLETE,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=False,
        static_description="Operation is in stage STAGED_VERIFIED; apply orchestration has not reached REPLACEMENT_READY",
    ),
    RestoreStage.REPLACING: _StageMetadata(
        assessment="operation_in_progress",
        exit_code=EXIT_OPERATION_IN_PROGRESS,
        terminal=False,
        postcheck_required=True,
        automatic_reentry_required=True,
        manual_recovery_required=False,
        static_description="Target replacement is active or interrupted and requires guarded re-entry",
    ),
    RestoreStage.REPLACED: _StageMetadata(
        assessment="operation_in_progress",
        exit_code=EXIT_OPERATION_IN_PROGRESS,
        terminal=False,
        postcheck_required=True,
        automatic_reentry_required=True,
        manual_recovery_required=False,
        static_description="All target replacements are durably recorded but postcheck remains required",
    ),
    RestoreStage.POSTCHECK_PASSED: _StageMetadata(
        assessment="operation_in_progress",
        exit_code=EXIT_OPERATION_IN_PROGRESS,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=True,
        manual_recovery_required=False,
        static_description="Postcheck passed but COMPLETED still must be durably persisted and reread",
    ),
    RestoreStage.ROLLBACK_REQUIRED: _StageMetadata(
        assessment="rollback_required",
        exit_code=EXIT_ROLLBACK_REQUIRED,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=True,
        manual_recovery_required=False,
        static_description="Operation failed mid-replacement; rollback from safety backup is required",
    ),
    RestoreStage.ROLLED_BACK: _StageMetadata(
        assessment="rollback_completed_pending_finalization",
        exit_code=EXIT_OPERATION_IN_PROGRESS,
        terminal=False,
        postcheck_required=False,
        automatic_reentry_required=True,
        manual_recovery_required=False,
        static_description="Rollback is durably complete, but FAILED_SAFE has not yet been persisted and reread. Guarded re-entry is required only to finalize the journal.",
    ),
    RestoreStage.FAILED_SAFE: _StageMetadata(
        assessment="failed_safely",
        exit_code=EXIT_FAILED_SAFE,
        terminal=True,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=False,
        static_description=None,  # Derived dynamically
    ),
    RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED: _StageMetadata(
        assessment="manual_recovery_required",
        exit_code=EXIT_MANUAL_RECOVERY_REQUIRED,
        terminal=True,
        postcheck_required=False,
        automatic_reentry_required=False,
        manual_recovery_required=True,
        static_description="CRITICAL: Operation failed with mixed target states. Manual recovery is required. Automatic continuation MUST NOT be attempted",
    ),
}


@dataclass(frozen=True)
class _InspectionAnalysis:
    metadata: _StageMetadata
    description: str
    replacement_intent_recorded: bool
    destination_replacement_completed: bool
    rollback_intent_recorded: bool
    destination_rollback_completed: bool
    all_completed_replacements_rolled_back: bool


def _derive_evidence_and_description(journal: RestoreJournal) -> _InspectionAnalysis:
    stage = journal.stage
    if not isinstance(stage, RestoreStage) or stage not in _STAGE_METADATA:
        raise RestoreJournalError("Unrecognized or unsupported global restore stage")

    meta = _STAGE_METADATA[stage]

    replacement_intent_recorded = any(t.replacement_intent for t in journal.targets)
    destination_replacement_completed = any(
        t.replacement_completed or t.state in (TargetRestoreState.REPLACED, TargetRestoreState.ROLLED_BACK)
        for t in journal.targets
    )
    rollback_intent_recorded = any(t.rollback_intent for t in journal.targets)
    destination_rollback_completed = any(
        t.rollback_completed or t.state == TargetRestoreState.ROLLED_BACK
        for t in journal.targets
    )

    replaced_targets = [
        t for t in journal.targets
        if t.replacement_completed or t.state in (TargetRestoreState.REPLACED, TargetRestoreState.ROLLED_BACK)
    ]
    all_completed_replacements_rolled_back = bool(replaced_targets) and all(
        t.rollback_completed and t.state == TargetRestoreState.ROLLED_BACK
        for t in replaced_targets
    )

    if stage is RestoreStage.FAILED_SAFE:
        if not replacement_intent_recorded:
            description = "Operation failed safely before replacement began."
        elif not destination_replacement_completed:
            description = "Operation failed safely with no durably completed destination replacement."
        elif all_completed_replacements_rolled_back:
            description = "Operation failed safely after verified rollback of all replaced targets."
        else:
            description = "Operation failed safely."
    else:
        description = meta.static_description or ""

    return _InspectionAnalysis(
        metadata=meta,
        description=description,
        replacement_intent_recorded=replacement_intent_recorded,
        destination_replacement_completed=destination_replacement_completed,
        rollback_intent_recorded=rollback_intent_recorded,
        destination_rollback_completed=destination_rollback_completed,
        all_completed_replacements_rolled_back=all_completed_replacements_rolled_back,
    )


def _resolve_operation_id(raw_id: str) -> str:
    operation_id = raw_id.strip()
    if "/" in operation_id or "\\" in operation_id or ".." in operation_id:
        raise RestoreJournalError("Operation ID cannot contain path separators or traversal components")
    if not _OPERATION_ID.fullmatch(operation_id):
        raise RestoreJournalError("Invalid or malformed operation ID format")
    return operation_id


def _render_human_inspection(journal_dict: dict[str, object], analysis: _InspectionAnalysis, *, show_paths: bool = False, operation_dir: Path | None = None) -> str:
    meta = analysis.metadata
    lines = [
        "GarminCoach Restore Operation Inspection",
        "========================================",
        f"Operation ID:                    {journal_dict['operation_id']}",
        f"Status Assessment:               {journal_dict['assessment']} ({analysis.description})",
        f"Global Stage:                    {journal_dict['stage']}",
        f"Terminal Stage:                  {journal_dict['terminal']}",
        f"Replacement Intent Recorded:     {journal_dict['replacement_intent_recorded']}",
        f"Destination Replace Completed:   {journal_dict['destination_replacement_completed']}",
        f"Rollback Intent Recorded:        {journal_dict['rollback_intent_recorded']}",
        f"Destination Rollback Completed:  {journal_dict['destination_rollback_completed']}",
        f"All Replaced Rolled Back:        {journal_dict['all_completed_replacements_rolled_back']}",
        f"Postcheck Required:              {journal_dict['postcheck_required']}",
        f"Automatic Re-entry Required:    {journal_dict['automatic_reentry_required']}",
        f"Manual Recovery Required:        {journal_dict['manual_recovery_required']}",
        f"Final Result:                    {journal_dict['final_result']}",
        f"Selected Backup ID:              {journal_dict['selected_backup_id']}",
        f"Selected Backup Manifest SHA-256:{journal_dict['selected_backup_manifest_sha256']}",
        f"Safety Backup ID:                {journal_dict['safety_backup_id']}",
        f"Expected Application Commit:     {journal_dict['expected_application_commit']}",
        f"Runtime Mode:                    {journal_dict['runtime_mode']}",
        f"Target-Set Hash:                 {journal_dict['target_set_hash']}",
        f"Confirmation Value:              {journal_dict['confirmation_value']}",
        f"Created At:                      {journal_dict['created_at']}",
        f"Updated At:                      {journal_dict['updated_at']}",
        "",
        f"Target Durable Facts ({len(journal_dict['targets'])}):",
    ]
    for target in journal_dict["targets"]:
        lines.append(
            f"  - {target['target_key']}: state={target['state']}, "
            f"replacement_intent={target['replacement_intent']}, replacement_completed={target['replacement_completed']}, "
            f"rollback_intent={target['rollback_intent']}, rollback_completed={target['rollback_completed']}, "
            f"wal_present={target['wal_present']}, shm_present={target['shm_present']}"
        )
    if show_paths and operation_dir is not None:
        lines.append("")
        lines.append("Local Paths (Diagnostic):")
        lines.append(f"  Operation Directory:           {operation_dir}")
        lines.append(f"  Journal File:                  {operation_dir / 'journal.json'}")

    return "\n".join(lines)


def inspect_operation(
    operation_id_input: str,
    *,
    human: bool = False,
    show_local_paths: bool = False,
) -> tuple[str, int]:
    clean_op_id = _resolve_operation_id(operation_id_input)
    root = validate_restore_root(config.OPERATOR_RESTORE_ROOT)

    try:
        journal = load_restore_journal(clean_op_id, root=root)
    except (RestoreJournalError, OSError, ValueError) as exc:
        raise RestoreJournalError(f"Restore operation '{clean_op_id}' journal is invalid or unavailable") from exc

    analysis = _derive_evidence_and_description(journal)
    meta = analysis.metadata

    journal_dict = {
        "format_version": journal.format_version,
        "operation_id": journal.operation_id,
        "assessment": meta.assessment,
        "stage": journal.stage.value,
        "terminal": meta.terminal,
        "replacement_intent_recorded": analysis.replacement_intent_recorded,
        "replacement_attempted": analysis.replacement_intent_recorded,
        "replacement_started": analysis.replacement_intent_recorded,
        "destination_replacement_completed": analysis.destination_replacement_completed,
        "rollback_intent_recorded": analysis.rollback_intent_recorded,
        "destination_rollback_completed": analysis.destination_rollback_completed,
        "all_completed_replacements_rolled_back": analysis.all_completed_replacements_rolled_back,
        "postcheck_required": meta.postcheck_required,
        "automatic_reentry_required": meta.automatic_reentry_required,
        "manual_recovery_required": meta.manual_recovery_required,
        "final_result": journal.final_result.value if journal.final_result else None,
        "selected_backup_id": journal.selected_backup_id,
        "selected_backup_manifest_sha256": journal.selected_backup_manifest_sha256,
        "safety_backup_id": journal.safety_backup_id,
        "expected_application_commit": journal.expected_application_commit,
        "runtime_mode": journal.runtime_mode,
        "target_keys": list(journal.target_keys),
        "target_set_hash": journal.target_set_hash,
        "confirmation_value": journal.confirmation_value,
        "targets": [
            {
                "target_key": fact.target_key,
                "state": fact.state.value,
                "wal_present": fact.wal_present,
                "shm_present": fact.shm_present,
                "wal_removed": fact.wal_removed,
                "shm_removed": fact.shm_removed,
                "replacement_intent": fact.replacement_intent,
                "replacement_completed": fact.replacement_completed,
                "rollback_intent": fact.rollback_intent,
                "rollback_completed": fact.rollback_completed,
            }
            for fact in journal.targets
        ],
        "created_at": journal.created_at,
        "updated_at": journal.updated_at,
    }

    op_dir = root / f"operation-{clean_op_id}"

    if human:
        output = _render_human_inspection(journal_dict, analysis, show_paths=show_local_paths, operation_dir=op_dir)
    else:
        output = json.dumps(journal_dict, indent=2, sort_keys=True)

    return output, meta.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only restore-operation inspection CLI for GarminCoach",
        prog="inspect_restore_operation.py",
    )
    parser.add_argument(
        "--operation-id",
        dest="flag_operation_id",
        help="Exact operation ID (e.g. restore-20260803T090000Z-12345678)",
    )
    parser.add_argument(
        "positional_operation_id",
        nargs="?",
        help="Exact operation ID (positional fallback)",
    )
    parser.add_argument(
        "--human",
        "--human-readable",
        action="store_true",
        dest="human",
        help="Produce concise human-readable output instead of JSON",
    )
    parser.add_argument(
        "--show-local-paths",
        action="store_true",
        dest="show_local_paths",
        help="Show absolute local paths (available in human-readable mode)",
    )

    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_INVALID_ARGUMENTS if exc.code != 0 else EXIT_SUCCESS

    raw_operation_id = args.flag_operation_id or args.positional_operation_id
    if not raw_operation_id:
        print("ERROR: [Invalid operation] Operation ID is required (--operation-id <id>)", file=sys.stderr)
        return EXIT_INVALID_ARGUMENTS

    try:
        output, exit_code = inspect_operation(
            raw_operation_id,
            human=args.human,
            show_local_paths=args.show_local_paths,
        )
        print(output)
        return exit_code
    except RestoreJournalError as exc:
        print(f"ERROR: [Invalid operation] {exc}", file=sys.stderr)
        return EXIT_INVALID_OPERATION
    except Exception as exc:
        print(f"ERROR: [Unexpected failure] Restore operation inspection failed internally: {exc.__class__.__name__}", file=sys.stderr)
        return EXIT_UNEXPECTED_FAILURE


if __name__ == "__main__":
    sys.exit(main())
