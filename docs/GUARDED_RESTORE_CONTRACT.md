# Guarded verified restore contract (Phase 6B1)

This is the authoritative design contract for a future guarded restore. It is
approved design only: GarminCoach currently has no restore command and performs
no restore mutation. Phase 6A remains the authority for target discovery,
read-only integrity inspection, verified backup creation, and backup
verification.

## 1. Goal and non-goals

The only supported future goal is to restore **all canonical runtime databases**
from one verified, current-configuration-compatible Phase 6A backup after an
explicit operator action. A runtime set is control plus single-user in
single-user mode, or control plus the existing canonical tenant `athlete.db`
files in multi-user mode. Target types and keys are `control`, `single-user`,
and `tenant:<canonical-uuid>`.

This excludes partial or selective-tenant restore, cross-profile or
cross-version restore, unverified directories, arbitrary SQLite files, WAL/SHM
files, schema downgrade, repair, migration, automatic/scheduled/cloud/remote
restore, retention deletion, web or Telegram controls, and restore while the
application is running. No later command may treat `--force` as a bypass for
these boundaries.

## 2. Existing primitives and unsafe assumptions

Phase 6B may reuse, without changing their contracts:

- `operator_storage.DatabaseTarget`, `TargetProfile.RUNTIME`, canonical UUID
  validation, original-component symlink detection, safe resolution, and
  read-only `inspect_sqlite`, schema fingerprint, migration marker, permission,
  and active-user mapping helpers;
- `verified_backup.verify_verified_backup(..., against_current_config=True)`,
  strict manifest validation, the non-reentrant `BackupLock`, canonical manifest
  checksums, and the public Phase 6A online snapshot operation; and
- `process_lock.acquire_process_lock` and `release_process_lock`; and
- engine disposal primitives only after the service has been proven stopped.

`db_migration.run_destructive_migrations` and `database_reset` are not restore
primitives. They create schemas, change data, remove sidecars, quarantine, and
may invoke service-state checks. Their migration backup and reset quarantine are
not verified restore sources. App lifespan currently acquires the application
lock, preflights existing files, runs migrations, initializes stores, and starts
schedulers; a restore engine must not invoke it. Tenant provisioning may create
a missing store, so it is not a restore discovery primitive.

## 3. Threat model and fail-closed controls

| Threat | Required control and failure result |
| --- | --- |
| Wrong backup, unverified directory, changed backup after verification | Strictly reverify the selected directory, canonical manifest bytes/hash, every file hash, and all metadata immediately before staging; fail before mutation. |
| Traversal, symlink replacement, wrong checkout | Validate every original component with `lstat` semantics, resolve under configured roots, revalidate destination identity immediately before replacement, and require the configured project root; fail safe. |
| Mapping, mode, schema, marker, package, or target-set drift | Verify immediately before the safety snapshot, then reverify after acquiring the long-held backup lock and before staging; require exact runtime mode, ordering, mappings, schema fingerprints, markers, source identity/hashes, and runtime distribution; fail safe. |
| Service/process starts or another backup/restore runs | Prove service stopped; hold application and restore locks through the safety snapshot; then exclusively acquire the Phase 6A backup lock for the mutation-critical interval. Any unavailable/uncertain lock or service state fails safe. |
| Disk or permission failure | Preflight bounded free space and private parent paths; stage/copy/fsync before replacement; fail safe without unlinking a destination. |
| Interrupted copy or replacement; partial multi-file replacement | Use per-target same-filesystem staging and a durable journal. Roll back only from the new verified safety backup; ambiguous journal state requires manual recovery. |
| Stale WAL/SHM sidecars | Never copy source sidecars. Handle a named target's sidecars only after exclusive service stop and immediately around replacement; never wildcard-delete. |
| Current corruption or safety-backup/rollback failure | Refuse restore when the current state cannot make a verified safety backup. A failed rollback moves the journal to manual recovery required and never restarts the service. |
| Secret, row, or path leakage | Journal and CLI use bounded metadata only; errors are sanitized. Absolute paths require an explicit local display flag. |

## 4. Required operator preconditions

The future engine must require the exact project root; supported Python and the
pinned `garminconnect` distribution; a clean verified source backup; and a
successful `verify_verified_backup(..., against_current_config=True)` immediately
before restore. It must rediscover current runtime targets, require their exact
ordering, mode, schema fingerprints, migration markers, package version, and
control-user-to-tenant mapping to match the source backup.

It must also require sufficient free disk space for staging plus a complete
safety backup; private, non-symlink destination parents; confirmed stopped
service; exclusive application and restore locks before the safety snapshot;
and later exclusive backup lock before staging; plus a deterministic non-secret
confirmation described below. A current malformed database is a refusal: Phase 6B2 must not silently
skip a safety backup or restore it anyway. A possible stronger-confirmation,
no-automatic-rollback recovery mode is explicitly out of scope for this
contract.

## 5. State machine and durable journal

The future restore journal is private, local, and atomic-rewrite (or
append-only) metadata at a path under a dedicated restore-operations root,
never in the selected backup or a database directory. It is 0600 on POSIX;
its parent is 0700. It contains format version, operation ID, selected backup
ID and manifest hash, safety-backup ID, expected commit, runtime mode, ordered
target keys, confirmation hash, current stage, per-target staged/replaced/
rolled-back facts, timestamps, and final result. It contains no rows, emails,
tokens, credentials, Garmin/Telegram payloads, or calendar data.

The requested operation ID is validated before journal-path construction and
must exactly equal the payload operation ID. Journal `updated_at` is monotonic
(`>=` the prior value, allowing equality for deterministic rapid updates) and
never earlier than `created_at`. A malformed identity or timestamp is rejected,
never normalized. Dedicated restore-lock acquisition releases any opened handle
on every failure and reports only a bounded lock error.

Allowed stages are:

`PRECHECK -> VERIFIED -> CURRENT_SNAPSHOT_CREATED -> RESTORE_STAGED ->
STAGED_VERIFIED -> REPLACEMENT_READY -> REPLACING -> REPLACED ->
POSTCHECK_PASSED -> COMPLETED`.

`PRECHECK` through `REPLACEMENT_READY` are mutation-free with respect to
configured databases. Safety-backup creation and strict verification transition
to `CURRENT_SNAPSHOT_CREATED`. Acquiring the long-held Phase 6A backup lock and
the repeated compatibility checks are mandatory before `RESTORE_STAGED`; failure
at that boundary becomes `FAILED_SAFE`. An error before `REPLACING` becomes
`FAILED_SAFE`.
An error during `REPLACING`, `REPLACED`, or postcheck becomes
`ROLLBACK_REQUIRED`; a complete verified rollback becomes `ROLLED_BACK`, then
`FAILED_SAFE`. A rollback error or an interrupted/unknown replacement state is
`FAILED_MANUAL_RECOVERY_REQUIRED` and blocks automatic continuation. Re-entry
may only inspect a journal whose state is unambiguous; it never guesses which
files changed. Automatic rollback is permitted only for target files recorded
as replaced and only from the recorded verified safety backup.

`FAILED_MANUAL_RECOVERY_REQUIRED` preserves the exact known per-target facts at
the failed rollback point: a mixture of `REPLACED`, `ROLLED_BACK`, and verified
but never-replaced targets is valid. It is reachable only from
`ROLLBACK_REQUIRED`, is terminal, and must not be rewritten as `FAILED_SAFE`.

## 6. Safety snapshot and staging

After precheck and before any replacement, create a new verified Phase 6A-format
backup of the complete current runtime state through the existing public backup
operation. Restore holds the application and restore locks, but **does not hold
`BackupLock`** for this call: the public operation acquires and releases its own
non-reentrant lock. The safety backup must have a distinct ID and directory,
never overwrite an older backup, pass strict verification, and be recorded in
the journal; its successful verification transitions to
`CURRENT_SNAPSHOT_CREATED`. It is the sole automatic rollback candidate.
Failure to create or verify it refuses restoration.

Only after that boundary, acquire `BackupLock` again nonblockingly and hold it
through final source/current compatibility verification, staging, staged
verification, replacement, postcheck, automatic rollback if needed, and the
final journal transition. If it cannot be acquired, record `FAILED_SAFE`, keep
the valid safety backup, and perform no configured-database mutation. After
acquisition, reverify current mappings, schema, markers, selected-backup
identity, and selected-backup hashes because they may have changed while the
safety backup was being made. This design does not assume recursive locking and
does not introduce an unlocked backup helper; any future lock-token helper needs
separate Phase 6B2 review.

For each target, create a private deterministic staging file on the same
filesystem as its final destination; never stage inside the selected backup.
Copy only the strictly validated `.sqlite` backup file. Recheck its hash against
the verifier metadata, then open staging read-only and require `quick_check`,
full `integrity_check`, foreign-key check, exact schema fingerprint, and exact
migration markers. Stage files are fsynced with their directories where
supported. WAL and SHM files are neither copied nor treated as backup content.
Any mismatch fails before replacement.

## 7. Replacement and rollback strategy

This architecture uses individually configured database files, so a
directory-generation/indirection activation scheme would require a configuration
and application-startup redesign and is not compatible with the existing fixed
paths. Phase 6B2 should therefore use deterministic sequential replacement with
a durable journal and verified automatic rollback.

Replacement order is all tenant or single-user data targets first and control
last. Until control changes, its active-user mapping continues to describe the
old tenant generation; making control last reduces the interval in which it can
refer to restored tenants after a partial failure. The journal records every
successful target replacement. If any subsequent replacement or postcheck
fails, stage and verify safety-backup copies, then roll back already-replaced
targets in reverse order, with control rolled back first if it was replaced.
No target may be omitted: target completeness is rechecked before staging,
replacement, and postcheck.

For each file, require compatible filesystems, private modes, target identity
revalidation, deterministic non-colliding temporary names, fsync of staged data,
and `os.replace` only after all targets are staged and verified. Never unlink a
current database first. Sidecars are handled only while the service is proven
stopped, only by explicit `<database>-wal` and `<database>-shm` names, and only
after their state is journaled. Windows sharing violations are bounded failures,
not retries that assume ownership; they leave the journal for safe recovery.

## 8. Locking and service boundary

The concerns are distinct: the app process lock protects a running application,
the Phase 6A backup lock protects backup creation, and a new restore lock
protects restore operations. Future restore has this fixed lifecycle:

1. prove the application service stopped using an explicitly reviewed local
   procedure (the engine does not stop or start it);
2. acquire the application process lock exclusively, proving no app owns it;
3. acquire and hold the dedicated restore lock exclusively;
4. invoke the ordinary public Phase 6A backup operation for the safety snapshot;
   it alone acquires and releases `BackupLock`;
5. acquire `BackupLock` again and hold it through final verification, staging,
   replacement, postcheck, rollback, and final journal transition.

Every acquisition is nonblocking. On every safe exit release in reverse order:
backup lock, restore lock, then application process lock. This cannot deadlock:
ordinary backup takes only `BackupLock`; restore holds application and restore
locks while invoking ordinary backup; and no operation holding `BackupLock`
waits for the restore lock. A competing backup after the safety snapshot simply
makes the restore's nonblocking long-held acquisition fail safely before
mutation. Phase 6B2 does not invoke `systemctl`, start/stop a service, or reuse
the reset tool's service-control code. A Phase 6B3 wrapper, if approved, may
document service verification before and after but not weaken these checks.

## 9. Confirmation boundary and future CLI

A future noninteractive interface must require all of:

```text
--backup-id <exact-id>
--expected-current-commit <sha-or-unknown>
--confirm-target-set-hash <sha256>
--confirm-restore <derived-value>
```

The target-set hash is canonical JSON over ordered non-secret target keys, mode,
source backup ID, and source manifest hash. The derived value is a documented
hash of that value and the expected commit. There is no generic confirmation or
`--force` override. Future commands may be `plan_verified_restore.py`,
`apply_verified_restore.py`, and a separate manual-recovery inspector rather
than automatic resume; ambiguous journals must never be resumed automatically.
JSON is versioned and bounded; stderr is sanitized; invalid arguments exit 64.
Later exit codes must distinguish precondition/verification failure, safe
failure, rollback completed, manual recovery required, and success.

## 10. Postcheck and rollback

Before `COMPLETED`, require every canonical target and no extra/missing target;
quick and full integrity checks; foreign-key checks; exact source-backup schema
fingerprints and markers; exact runtime set and active mapping; private modes;
no stale named sidecars; and operator health at its documented acceptable state.
Record the selected backup identity and restored-file hashes. Do not run
migrations.

Rollback stages, copies, and verifies the safety backup with the same strict
rules, then restores only journaled replaced targets in deterministic reverse
order. It never deletes either backup. A rollback failure leaves the application
stopped and records `FAILED_MANUAL_RECOVERY_REQUIRED`; it never attempts a
restart.

## 11. Phase 6B2 test matrix and slicing

Synthetic fixtures and fault injection, never sleeps, must cover single-user
and multi-user success; wrong mode; missing/extra tenants; mapping, schema,
marker, and package mismatch; changed source backup; traversal/symlink attempts;
all held locks; service uncertainty; insufficient disk; safety-backup and
staging failures; staged integrity failure; replacement failure at every target
index; interruption at every state; rollback success/failure; stale sidecars;
Windows locks; POSIX permissions; leakage; no source/older-backup mutation;
permitted idempotent re-entry; and refusal of ambiguous journals. It must also
fault-inject a competing backup owning the long-held `BackupLock` after safety
backup creation, prove the resulting failure precedes replacement, detect
mapping/schema/marker drift between safety-backup completion and long-held lock
acquisition, prove reverse lock release after safe failure, and prove no
recursive `BackupLock` acquisition occurs.

Implementation requires separate approvals:

1. **6B2A**: complete: pure planning/state-machine/journal primitives and a
   dedicated restore lock in `guarded_restore.py`; they neither inspect nor
   mutate databases, invoke backups, acquire application/backup locks, or
   control services. No staging or replacement is included.
2. **6B2B**: complete: synthetic-fixture-only offline staging and strict
   verification through `REPLACEMENT_READY`; configured destinations, replacement,
   rollback, and serialized staging paths remain absent.
3. **6B2C**: complete: guarded replacement, postcheck, and automatic rollback
   on temporary synthetic fixtures only. Configured application databases remain
   excluded and untargetable.
4. **6B3A**: complete: non-mutating operator restore planning CLI (`plan_verified_restore.py`), read-only restore-operation inspector CLI (`inspect_restore_operation.py`), operator runbook (`docs/GUARDED_RESTORE_RUNBOOK.md`), and deterministic tests. No apply or database mutation commands are included.
5. **6B3B1**: in progress: configured-runtime restore preparation through `REPLACEMENT_READY` (`guarded_restore_configured.py`, `guarded_restore_configured_staging.py`, and deterministic tests). Includes journal-bound baseline SHA-256 enforcement at every barrier, descriptor-bound `fchmod` permission finalization, hard-link count validation for binding and staged artifacts, persisted baseline parent-identity verification during staging, `publish_noreplace` parent/filename ownership checks, and post-write baseline SHA reread. No database replacement, rollback, or apply CLI is included.
6. **6B3B2**: unimplemented: configured replacement, postcheck, rollback, and re-entry.
7. **6B3B3**: unimplemented: operator apply CLI (`apply_verified_restore.py`) and separately approved production drill.
8. A production restore drill requires a separate explicit review.
