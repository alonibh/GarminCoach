# Guarded verified restore operator runbook (Phase 6B3A)

> [!IMPORTANT]
> **Phase 6B3A is non-mutating.**
> GarminCoach currently has no command to perform a database restore. Phase 6B3A provides only read-only planning and inspection tooling. No database, service, lock, backup, or file system mutation is performed by these tools.
> Phase 6B3B (mutation apply orchestration) remains unimplemented and requires separate review and approval. No production restore drill has been approved.

---

## 1. Prerequisites

Before running any restore planning or inspection commands:

1. **Project Root**: All commands must be executed directly from the exact configured GarminCoach project root directory.
2. **Read-Only Access**: Verify that operator backup storage (`operator_backups`) and restore operation storage (`operator_restore_operations`) are readable and uncorrupted.
3. **Environment**: Python 3.12+ with standard project dependencies installed (specifically `garminconnect`).
4. **Service Awareness**: While Phase 6B3A planning does not touch running services, a future apply command (Phase 6B3B) will require the application service (e.g. systemd `garmincoach.service`) to be stopped manually before execution. No current command automatically invokes `systemctl`, stops/starts services, deploys code, or mutates databases.

---

## 2. Identifying and verifying a backup

Verify an existing Phase 6A backup using the verification tool:

```bash
python scripts/verify_verified_backup.py operator_backups/backup-<BACKUP_ID> --against-current-config
```

Alternatively, use `plan_verified_restore.py` to verify and plan in a single non-mutating step.

Requirements for a valid backup:
- Must exist directly under the configured `OPERATOR_BACKUP_ROOT`.
- Must pass canonical manifest checksum verification (`manifest.sha256`).
- Must pass deep SQLite integrity checks (`inspect_sqlite(..., deep=True)`).
- Must match current configuration: mode (`single_user` or `multi_user`), target set ordering, target keys, schema fingerprints, migration markers, and package version.

Selected source backups and generated safety backups must **never be modified or deleted**.

---

## 3. Running the restore planning CLI

Run `plan_verified_restore.py` with an exact verified backup ID:

```bash
python plan_verified_restore.py --backup-id 20260803T090000Z-12345678
```

Options:
- `--backup-id <ID>`: Exact Phase 6A backup identifier (format: `YYYYMMDDTHHMMSSZ-XXXXXXXX`).
- `--expected-current-commit <SHA>`: Expected application git commit SHA (defaults to current repository HEAD commit SHA or `unknown`).
- `--human`: Output concise human-readable text format instead of default machine-readable JSON.
- `--show-local-paths`: Display local filesystem paths for diagnostics (available in `--human` mode; omitted from machine-readable JSON by default).

### Preserving planning artifacts

Record and preserve the following values from the planning output for future audit:
1. **Selected Backup ID** (`selected_backup_id`)
2. **Selected Backup Manifest SHA-256** (`selected_backup_manifest_sha256`)
3. **Expected Application Commit** (`expected_application_commit`)
4. **Target-Set Hash** (`target_set_hash`)
5. **Confirmation Value** (`confirmation_value`)

---

## 4. Inspecting an operation journal

Inspect any existing restore operation journal using `inspect_restore_operation.py`:

```bash
python inspect_restore_operation.py --operation-id restore-20260803T090000Z-12345678
```

Options:
- `--operation-id <ID>`: Exact restore operation identifier (format: `restore-YYYYMMDDTHHMMSSZ-XXXXXXXX`).
- `--human`: Output concise human-readable text format instead of default machine-readable JSON.
- `--show-local-paths`: Display local operation directory paths.

The inspector reads the durable journal from `operator_restore_operations/operation-<ID>/journal.json` read-only. It makes no file mutations, performs no automatic continuation, and does not run cleanup.

---

## 5. Stages, assessments, and exit codes

### CLI Exit Codes

| Exit Code | Name | Meaning |
| --- | --- | --- |
| `0` | `EXIT_SUCCESS` | Command completed successfully; planning preconditions met OR operation inspected is `COMPLETED` / `safe_to_proceed_to_apply`. |
| `1` | `EXIT_PRECONDITION_FAILED` | Restore planning precondition or backup verification failed (wrong root, malformed ID, unverified backup, configuration drift). |
| `2` | `EXIT_INVALID_OPERATION` | Invalid or unavailable restore operation journal (malformed ID, missing journal, corrupted payload, path traversal). |
| `3` | `EXIT_FAILED_SAFE` | Inspected operation is in a safe terminal state (`FAILED_SAFE` or `ROLLED_BACK`). |
| `4` | `EXIT_ROLLBACK_REQUIRED` | Inspected operation failed mid-replacement; automatic rollback from safety backup is required before any further action. |
| `5` | `EXIT_MANUAL_RECOVERY_REQUIRED` | Inspected operation failed with mixed target states (`FAILED_MANUAL_RECOVERY_REQUIRED`). **Manual recovery required.** |
| `6` | `EXIT_UNEXPECTED_FAILURE` | Unexpected internal failure. |
| `64` | `EXIT_INVALID_ARGUMENTS` | Invalid command-line arguments or usage. |

### Global Operation Stages

- `PRECHECK` / `VERIFIED`: Initial verification stages (mutation-free).
- `CURRENT_SNAPSHOT_CREATED`: Verified safety backup of current live databases created and verified.
- `RESTORE_STAGED` / `STAGED_VERIFIED` / `REPLACEMENT_READY`: Offline staging prepared and verified on destination filesystems. Safe to proceed to apply.
- `REPLACING`: Active database replacement in progress.
- `REPLACED` / `POSTCHECK_PASSED` / `COMPLETED`: Replacement finished and postcheck passed.
- `ROLLBACK_REQUIRED`: Failure occurred during replacement/postcheck; rollback needed.
- `ROLLED_BACK` / `FAILED_SAFE`: Rollback succeeded or safe failure recorded. Terminal state.
- `FAILED_MANUAL_RECOVERY_REQUIRED`: Rollback failed or replacement state is ambiguous. Terminal state.

---

## 6. Manual recovery rules

> [!CAUTION]
> **Do NOT attempt automatic recovery if status is `FAILED_MANUAL_RECOVERY_REQUIRED`.**
> `FAILED_MANUAL_RECOVERY_REQUIRED` indicates an interrupted replacement or failed rollback. The operation journal preserves exact per-target durable facts showing a mixture of `REPLACED`, `ROLLED_BACK`, and `STAGED_VERIFIED` targets.
> Because target databases are in an inconsistent generation state:
> 1. The application service MUST remain stopped.
> 2. No automatic re-entry or continuation script may be run.
> 3. An operator must inspect the durable facts and manually restore target databases from the recorded safety backup ID.
