"""Non-mutating operator restore planning CLI (Phase 6B3A).

Creates a versioned, immutable restore plan for a verified Phase 6A backup
against the current application configuration without modifying any runtime
databases or service state.
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
    EXIT_INVALID_ARGUMENTS,
    EXIT_PRECONDITION_FAILED,
    EXIT_SUCCESS,
    EXIT_UNEXPECTED_FAILURE,
    RestorePlanError,
    _BACKUP_ID,
    _COMMIT,
    create_restore_plan,
)
from operator_storage import (
    DatabaseIntegrityError,
    discover_database_targets,
    has_symlink_component,
    safe_resolve,
)
from verified_backup import BackupError, _commit, load_validated_backup_snapshot, validate_backup_root


def _verify_project_root() -> None:
    current_cwd = Path.cwd()
    if has_symlink_component(current_cwd):
        raise RestorePlanError("Execution directory contains symlink components")
    project_root = Path(config.PROJECT_ROOT).resolve()
    if current_cwd.resolve() != project_root:
        raise RestorePlanError("Execution must be from the exact configured GarminCoach project root")


def _resolve_backup_directory(raw_id: str) -> tuple[str, Path]:
    backup_id = raw_id.strip()
    if backup_id.startswith("backup-"):
        backup_id = backup_id[7:]
    if not _BACKUP_ID.fullmatch(backup_id):
        raise RestorePlanError("Invalid or malformed backup ID format")
    if "/" in raw_id or "\\" in raw_id or ".." in raw_id:
        raise RestorePlanError("Backup ID cannot contain path separators or traversal components")

    root = validate_backup_root(config.OPERATOR_BACKUP_ROOT)
    backup_dir = root / f"backup-{backup_id}"
    if has_symlink_component(backup_dir) or backup_dir.is_symlink():
        raise RestorePlanError("Backup directory path contains symlink components")
    if not backup_dir.is_dir():
        raise RestorePlanError("Selected backup directory does not exist or is not a directory")
    resolved = safe_resolve(backup_dir, root=root)
    if resolved.parent != root:
        raise RestorePlanError("Backup directory is not located directly under the operator backup root")
    return backup_id, backup_dir


def _render_human_plan(plan_dict: dict[str, object], snapshot, *, show_paths: bool = False) -> str:
    lines = [
        "GarminCoach Guarded Restore Plan",
        "================================",
        f"Format Version:                  {plan_dict['format_version']}",
        f"Selected Backup ID:              {plan_dict['selected_backup_id']}",
        f"Selected Backup Manifest SHA-256:{plan_dict['selected_backup_manifest_sha256']}",
        f"Expected Application Commit:     {plan_dict['expected_application_commit']}",
        f"Runtime Mode:                    {plan_dict['runtime_mode']}",
        f"Target Keys ({len(plan_dict['target_keys'])}):              {', '.join(plan_dict['target_keys'])}",
        f"Target-Set Hash:                 {plan_dict['target_set_hash']}",
        f"Confirmation Value:              {plan_dict['confirmation_value']}",
        f"Plan Created At:                 {plan_dict['created_at']}",
    ]
    if show_paths:
        lines.append("")
        lines.append("Local Paths (Diagnostic):")
        lines.append(f"  Selected Backup Directory:     {snapshot.directory}")
        targets = discover_database_targets()
        target_map = {t.target_key: t.path for t in targets}
        for key in plan_dict["target_keys"]:
            path = target_map.get(key, "unknown")
            lines.append(f"  Target '{key}': {path}")
    return "\n".join(lines)


def plan_restore(
    backup_id_input: str,
    *,
    expected_commit_input: str | None = None,
    human: bool = False,
    show_local_paths: bool = False,
) -> tuple[str, int]:
    _verify_project_root()
    clean_id, backup_dir = _resolve_backup_directory(backup_id_input)

    expected_commit = expected_commit_input or _commit()
    if expected_commit != "unknown" and not _COMMIT.fullmatch(expected_commit):
        raise RestorePlanError("Invalid expected application commit format")

    try:
        snapshot = load_validated_backup_snapshot(backup_dir, against_current_config=True)
    except (BackupError, DatabaseIntegrityError, OSError, ValueError) as exc:
        raise RestorePlanError("Backup verification against current configuration failed") from exc

    plan = create_restore_plan(
        selected_backup_id=snapshot.backup_id,
        selected_backup_manifest_sha256=snapshot.manifest_sha256,
        expected_application_commit=expected_commit,
        runtime_mode=snapshot.runtime_mode,
        target_keys=snapshot.target_keys,
    )
    plan_dict = asdict(plan)

    if human:
        output = _render_human_plan(plan_dict, snapshot, show_paths=show_local_paths)
    else:
        output = json.dumps(plan_dict, indent=2, sort_keys=True)

    return output, EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Non-mutating operator restore planning for GarminCoach",
        prog="plan_verified_restore.py",
    )
    parser.add_argument(
        "--backup-id",
        dest="flag_backup_id",
        help="Exact verified backup ID (e.g. 20260803T090000Z-12345678)",
    )
    parser.add_argument(
        "positional_backup_id",
        nargs="?",
        help="Exact verified backup ID (positional fallback)",
    )
    parser.add_argument(
        "--expected-current-commit",
        "--expected-commit",
        dest="expected_commit",
        help="Expected application git commit SHA or 'unknown'",
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

    raw_backup_id = args.flag_backup_id or args.positional_backup_id
    if not raw_backup_id:
        print("ERROR: [Precondition failed] Backup ID is required (--backup-id <id>)", file=sys.stderr)
        return EXIT_INVALID_ARGUMENTS

    try:
        output, exit_code = plan_restore(
            raw_backup_id,
            expected_commit_input=args.expected_commit,
            human=args.human,
            show_local_paths=args.show_local_paths,
        )
        print(output)
        return exit_code
    except RestorePlanError as exc:
        print(f"ERROR: [Precondition failed] {exc}", file=sys.stderr)
        return EXIT_PRECONDITION_FAILED
    except Exception as exc:
        print(f"ERROR: [Unexpected failure] Restore planning failed internally: {exc.__class__.__name__}", file=sys.stderr)
        return EXIT_UNEXPECTED_FAILURE


if __name__ == "__main__":
    sys.exit(main())
