# Operator recovery contract (Phase 6A)

This is the authoritative operator-only contract for Phase 6A. It has no
product, coaching, scheduling, notification, Garmin, calendar, or web-admin
authority.

## Safety boundary

GarminCoach never replaces, quarantines, deletes, or recreates an existing
malformed database automatically.

A verified backup is not restored in Phase 6A.

Canonical targets are the configured control database, the configured
single-user database when that mode is active, and only `athlete.db` under a
canonical lowercase UUID directory directly below `MULTI_USER_DATA_ROOT`.
Resolved paths are case-insensitively deduplicated; symlink escapes, arbitrary
`.db` files, sidecars, temporary, recovered, quarantine, and backup files are
not targets. Active control user IDs map only to `tenant:<canonical-uuid>`.

Discovery is explicit: the runtime profile includes control plus tenants in
multi-user mode, or control plus the single-user database in single-user mode.
The legacy migration/reset maintenance profile may include both configured
families. Original path components are checked before resolution: symlinks,
path escapes, a backup root equal to tenant storage, and symlinked artifacts
are rejected.

`operator_health` is read-only. It uses SQLite URI read-only mode, `quick_check`
(and `integrity_check` only with `--deep`), foreign-key inspection, migration
ledger facts, target/path safety, private POSIX permission checks, partial
backup, latest-backup, lock, and bounded disk-space checks. It reports only
`healthy`, `warning`, or `critical` and exits 0, 1, or 2 respectively (64 for
invalid CLI use). It never prints rows, secrets, emails, raw exceptions, logs,
or absolute paths unless `--show-paths` is requested.

## Verified backup format

Backups are explicit local operator operations. `create_verified_backup` has a
dedicated nonblocking `.garmincoach-backup.lock`; it does not acquire the app
lock, stop/restart the service, or copy `-wal`/`-shm` files. A staging directory
`.partial-<id>` is private, then is atomically renamed to `backup-<id>` only
after every source is read-only inspected, copied with SQLite's online backup
API, deep-integrity verified, checksummed, and recorded.

The backup root is project-relative when configured relatively, cannot be a
database or database parent, and cannot be inside a tenant directory. POSIX
roots/directories are 0700 and files/lock are 0600. Windows mode changes are
best effort; ACL enforcement is not claimed. Failure removes only the command's
staging directory and never an older complete backup or source file.

`manifest.json` is `garmincoach-backup-v1`, canonical UTF-8 JSON with sorted
keys, compact separators, and one trailing LF. `manifest.sha256` hashes those
exact bytes. It records bounded identity, application/runtime metadata,
per-target filename/size/SHA-256/deep integrity/schema fingerprint/migration
markers/timestamps, and before/after anonymous user-ID-to-target mappings. The
set is individually consistent SQLite snapshots, not a cross-database
transaction; mapping change aborts publication.

Verification parses the complete manifest before trusting any file: exactly one
`000-control.sqlite` control entry, positive count, deterministic target names,
UTC ordered timestamps, valid runtime provenance, exact marker objects, hashes,
and identical UUID-to-tenant mappings are mandatory. An empty, partial, unknown,
or internally inconsistent set is invalid even if every listed file hashes.
`--against-current-config` additionally requires the runtime target set and the
installed `garminconnect` distribution version to match.

`verify_verified_backup` validates every manifest, file, checksum, simple
filename, target relationship, tenant UUID, migration marker, schema
fingerprint, and full integrity check without modifying the backup or configured
database. `--against-current-config` maps target keys and reports missing/new
targets. `--restore-plan-json` emits only `garmincoach-restore-plan-v1` with
`restorable: false` and would-replace operations.

## Phase 6B exclusion and acceptance

Phase 6A has no restore/apply/force option, replacement, sidecar deletion,
service control, migration against a backup, rollback mutation, retention,
encryption, compression, cloud upload, schedule, or web/Telegram control.
Phase 6B requires separate review, guarded service-stop and replacement design,
and boundary tests. Acceptance requires synthetic-fixture tests proving no
inspection alters source bytes/mtime/sidecars, malformed existing databases fail
closed, complete backups are atomic and verified, verifier is mutation-free,
and public `/health` remains bounded liveness while raw web log access is absent.
