# Guarded verified restore operator runbook (Phase 6B3A)

> [!IMPORTANT]
> **Phase 6B3A is non-mutating.**
> GarminCoach currently has no command to perform a database restore. Phase 6B3A provides only read-only planning and inspection tooling. No database, service, lock, backup, or file system mutation is performed by these tools.
> Phase 6B3B1 (configured-runtime restore preparation through `REPLACEMENT_READY`) is complete. It requires mandatory destination-baseline evidence, durable-parent proof before and after staging boundaries via parent-substitution verification, strict `STAGED_VERIFIED` and `REPLACEMENT_READY` re-entry validation, descriptor-bound permission finalization, no pathname `chmod` after staged publication, exact child-set and artifact ownership validation (including second child-set enumeration and complete before/after descriptor-fact comparisons to defeat directory/descriptor race conditions), clean publication cleanup-uncertainty handling without swallowing errors, and post-staging destination-baseline rereads. Configured databases and WAL/SHM sidecars remain unmodified.
> Phase 6B3B2 (configured replacement, postcheck, rollback, and re-entry) and Phase 6B3B3 (apply CLI) remain unimplemented and require separate review and approval. No production restore command or drill exists yet.

### Phase 6B3B1 completion evidence

The configured preparation implementation and deterministic tests establish all of the following before a future replacement phase can begin:

- Destination baseline evidence is mandatory and remains journal-bound at every preparation barrier.
- The durable parent identity is proven before staging and revalidated after staging/publication boundaries, backed by real parent-substitution test verification.
- Re-entry at both `STAGED_VERIFIED` and `REPLACEMENT_READY` performs strict journal, binding, stage-directory, child-set, artifact type/mode/link-count, and content validation.
- Binding and staged-artifact permissions are finalized through open descriptors; no pathname `chmod` is performed after staged publication.
- Exact child sets in the staging directories are validated before and after inspections (second child-set enumeration and complete before/after descriptor-fact comparisons) to defeat directory/descriptor race conditions.
- Publication fallback copy and verification are wrapped safely, and any cleanup uncertainty (like pathname identity changes) is handled cleanly, raising ownership errors rather than silently swallowing unlinking/cleanup failures.
- Destination database bytes, metadata, and named WAL/SHM sidecars are reread against their baselines and are not modified by Phase 6B3B1.


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

The inspector reads the durable journal from `operator_restore_operations/operation-<ID>/journal.json` read-only. It makes no file mutations, performs no automatic continuation, and does not run cleanup. Operators must **never manually normalize, edit, or delete** journal files.

---

## 5. Stages, assessments, and exit codes

### CLI Exit Codes

| Exit Code | Name | Meaning |
| --- | --- | --- |
| `0` | `EXIT_SUCCESS` | Command completed successfully; preconditions met; OR operation inspected is `COMPLETED` / `ready_for_replacement`. |
| `1` | `EXIT_PRECONDITION_FAILED` | Restore planning precondition or backup verification failed (wrong root, malformed ID, unverified backup, configuration drift). |
| `2` | `EXIT_INVALID_OPERATION` | Invalid or unavailable restore operation journal (malformed ID, missing journal, corrupted payload, path traversal). |
| `3` | `EXIT_FAILED_SAFE` | Inspected operation is in a safe terminal state (`FAILED_SAFE`). |
| `4` | `EXIT_ROLLBACK_REQUIRED` | Inspected operation failed mid-replacement; automatic rollback from safety backup is required before any further action. |
| `5` | `EXIT_MANUAL_RECOVERY_REQUIRED` | Inspected operation failed with mixed target states (`FAILED_MANUAL_RECOVERY_REQUIRED`). **Manual recovery required.** |
| `6` | `EXIT_UNEXPECTED_FAILURE` | Unexpected bounded internal failure. |
| `7` | `EXIT_PREPARATION_INCOMPLETE` | Operation is in a pre-staging or staging preparation stage (`PRECHECK` .. `STAGED_VERIFIED`); apply orchestration has not reached `REPLACEMENT_READY`. |
| `8` | `EXIT_OPERATION_IN_PROGRESS` | Operation is active, interrupted, or pending finalization (`REPLACING`, `REPLACED`, `POSTCHECK_PASSED`, `ROLLED_BACK`); requires guarded re-entry. |
| `64` | `EXIT_INVALID_ARGUMENTS` | Invalid command-line arguments or usage. |

### Global Operation Stages & Assessment Mapping

| Global Stage | Assessment | Exit Code | Terminal | Description & Safety Semantics |
| --- | --- | --- | --- | --- |
| `COMPLETED` | `completed` | `0` | `True` | Restore operation completed successfully. |
| `REPLACEMENT_READY` | `ready_for_replacement` | `0` | `False` | Staging and pre-replacement verification completed; ready for apply. |
| `PRECHECK` | `preparation_incomplete` | `7` | `False` | Precheck phase in progress; apply orchestration has not reached `REPLACEMENT_READY`. |
| `VERIFIED` | `preparation_incomplete` | `7` | `False` | Source backup verified; apply orchestration has not reached `REPLACEMENT_READY`. |
| `CURRENT_SNAPSHOT_CREATED` | `preparation_incomplete` | `7` | `False` | Safety backup created; apply orchestration has not reached `REPLACEMENT_READY`. |
| `RESTORE_STAGED` | `preparation_incomplete` | `7` | `False` | Staging copy created; apply orchestration has not reached `REPLACEMENT_READY`. |
| `STAGED_VERIFIED` | `preparation_incomplete` | `7` | `False` | Staged copies verified; apply orchestration has not reached `REPLACEMENT_READY`. |
| `REPLACING` | `operation_in_progress` | `8` | `False` | Target replacement is active or interrupted and requires guarded re-entry. Never treat as safe failure. |
| `REPLACED` | `operation_in_progress` | `8` | `False` | All target replacements durably recorded but postcheck remains required. Never treat as completed or safe failure. |
| `POSTCHECK_PASSED` | `operation_in_progress` | `8` | `False` | Postcheck passed but `COMPLETED` still must be durably persisted and reread. Never treat as completed. |
| `ROLLBACK_REQUIRED` | `rollback_required` | `4` | `False` | Failure during replacement or postcheck; automatic rollback from safety backup required. |
| `ROLLED_BACK` | `rollback_completed_pending_finalization` | `8` | `False` | Rollback is durably complete, but `FAILED_SAFE` has not yet been persisted and reread. Guarded re-entry is required only to finalize the journal. |
| `FAILED_SAFE` | `failed_safely` | `3` | `True` | Operation failed safely (either before replacement began, without destination replacement, or after verified rollback of all replaced targets). |
| `FAILED_MANUAL_RECOVERY_REQUIRED` | `manual_recovery_required` | `5` | `True` | Interrupted/failed rollback with mixed target states. **Manual recovery required; automatic continuation MUST NOT be attempted.** |

> [!IMPORTANT]
> **Mutation Evidence vs Global Stage**:
> Replacement intent (`replacement_intent_recorded`) records that replacement preparation began, but does NOT prove that `os.replace` changed a destination file.
> Actual destination changes are determined from per-target durable facts (`destination_replacement_completed`, `destination_rollback_completed`, `all_completed_replacements_rolled_back`).

---

## 6. Manual recovery rules

> [!CAUTION]
> **Do NOT attempt automatic recovery if status is `FAILED_MANUAL_RECOVERY_REQUIRED`.**
> `FAILED_MANUAL_RECOVERY_REQUIRED` indicates an interrupted replacement or failed rollback. The operation journal preserves exact per-target durable facts showing a mixture of `REPLACED`, `ROLLED_BACK`, and `STAGED_VERIFIED` targets.
> Because target databases are in an inconsistent generation state:
> 1. The application service MUST remain stopped.
> 2. No automatic re-entry or continuation script may be run.
> 3. Operators must **never edit, normalize, or delete** the operation journal.
> 4. An operator must inspect the durable facts and manually restore target databases from the recorded safety backup ID.
