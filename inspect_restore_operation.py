"""Read-only restore-operation inspection CLI (Phase 6B3A).

Inspects an existing guarded restore journal from the configured restore-operation
root, reporting its bounded operational facts, current stage, per-target durable
state, and safety assessment without mutating or continuing any operation.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict
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
    EXIT_ROLLBACK_REQUIRED,
    EXIT_SUCCESS,
    EXIT_UNEXPECTED_FAILURE,
    RestoreJournalError,
    RestoreStage,
    _OPERATION_ID,
    load_restore_journal,
    validate_restore_root,
)


def _classify_stage(stage: RestoreStage) -> tuple[str, str, int]:
    """Return (assessment_key, human_description, exit_code)."""
    if stage is RestoreStage.COMPLETED:
        return "completed", "Restore operation completed successfully", EXIT_SUCCESS
    if stage in {
        RestoreStage.PRECHECK,
        RestoreStage.VERIFIED,
        RestoreStage.CURRENT_SNAPSHOT_CREATED,
        RestoreStage.RESTORE_STAGED,
        RestoreStage.STAGED_VERIFIED,
        RestoreStage.REPLACEMENT_READY,
    }:
        return "safe_to_proceed_to_apply", "Operation is at a valid pre-mutation stage; safe to proceed to future apply", EXIT_SUCCESS
    if stage in {RestoreStage.FAILED_SAFE, RestoreStage.ROLLED_BACK}:
        return "failed_safely", "Operation terminated safely without unrolled state mutations", EXIT_FAILED_SAFE
    if stage is RestoreStage.ROLLBACK_REQUIRED:
        return "rollback_required", "Operation failed mid-replacement; rollback from safety backup is required", EXIT_ROLLBACK_REQUIRED
    if stage is RestoreStage.FAILED_MANUAL_RECOVERY_REQUIRED:
        return "manual_recovery_required", "CRITICAL: Operation failed with mixed target states. Manual recovery is required. Automatic continuation MUST NOT be attempted", EXIT_MANUAL_RECOVERY_REQUIRED

    return "failed_safely", "Operation is in a safe terminal state", EXIT_FAILED_SAFE


def _resolve_operation_id(raw_id: str) -> str:
    operation_id = raw_id.strip()
    if "/" in operation_id or "\\" in operation_id or ".." in operation_id:
        raise RestoreJournalError("Operation ID cannot contain path separators or traversal components")
    if not _OPERATION_ID.fullmatch(operation_id):
        raise RestoreJournalError("Invalid or malformed operation ID format")
    return operation_id


def _render_human_inspection(journal_dict: dict[str, object], assessment_desc: str, *, show_paths: bool = False, operation_dir: Path | None = None) -> str:
    lines = [
        "GarminCoach Restore Operation Inspection",
        "========================================",
        f"Operation ID:                    {journal_dict['operation_id']}",
        f"Status Assessment:               {journal_dict['assessment']} ({assessment_desc})",
        f"Global Stage:                    {journal_dict['stage']}",
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

    assessment_key, assessment_desc, exit_code = _classify_stage(journal.stage)

    journal_dict = {
        "format_version": journal.format_version,
        "operation_id": journal.operation_id,
        "assessment": assessment_key,
        "stage": journal.stage.value,
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
        output = _render_human_inspection(journal_dict, assessment_desc, show_paths=show_local_paths, operation_dir=op_dir)
    else:
        output = json.dumps(journal_dict, indent=2, sort_keys=True)

    return output, exit_code


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
