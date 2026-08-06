# Maintenance notes

This document records optional architectural restructuring that was identified
during the Phase 1–6 reconciliation (Gate D2, 2026-08-06) but deliberately
not rewritten. Each item satisfies the deferral criteria: the boundary is not
fully cohesive, the extraction would require end-to-end retesting across
multiple integration fixtures, and the change does not reduce immediate review
risk more than the risk it introduces.

These are not defects. They are bounded debt items for a future sprint with
dedicated test coverage and a complete review of the surrounding integration
contracts.

---

## sync/sync_service.py (2563 lines)

### State-key access helpers

Lines 126–154 define `_get_state`, `_set_state`, `_clear_state`,
`_parse_state_date`, and `_local_today`. These could be extracted to a
`sync/_state.py` module. However, the helpers share context with the
surrounding sync-cursor and rate-limiting helpers (lines 158–243) and the
correct extraction boundary is not clear without reviewing all 15+ call sites
across the two stage functions. Extraction is deferred.

**Constraint:** `SyncState` key names must not change. Any extraction must use
compatibility re-exports.

### Rate-limiting helpers

Lines 226–243 (`_is_in_cooldown`, `_note_rate_limited`, `_clear_cooldown`)
are cohesive but small. Extracting them alone leaves an asymmetric module with
only three functions; extracting them together with the state-key helpers
increases scope. Deferred.

### Stage 1 / Stage 2 body size

`_sync_stage1` (line 1228) and `_run_sync` (line 2072) are each several
hundred lines. The natural sub-functions (activity sync, sleep sync, daily
health sync, workout sync) already exist as separate `_sync_*` functions. No
extraction would improve review without reorganising the call graph, which
changes fetch order semantics. Deferred.

**Constraint:** The fetch order for Garmin API resources must not change.
Sync protocol keys must not be renamed.

---

## guarded_restore_configured_replacement.py (3214 lines)

### Rollback-binding I/O group

Lines 345–696 (`_rollback_dir_name`, `_rollback_artifact_name`,
`_rollback_binding_bytes`, `_write_rollback_binding`, `_verify_rollback_binding`,
`_copy_rollback_file`) form a cohesive rollback-artifact I/O group. However,
they share the journal format and error types with the surrounding transition
and postcheck functions (lines 1136–1589). Extracting the I/O group alone
leaves orphaned references to `RestoreJournal` and the stage-transition
contract. Deferred.

**Constraint:** Restore journal format must not change. Restore stage
transitions must not change. Public CLI behavior must not change.

### Evidence cleanup

Lines 2230–2350 (`_cleanup_evidence`, `_cleanup_single_dir`, and the cleanup
orchestration block) are standalone but depend on the same operation-directory
structure as the rest of the replacement engine. Extracting them would require
a shared path-convention module. Deferred.

---

## Completed D1 actions (not deferred)

- `db.naive_utc()` helper added; all `datetime.utcnow` column defaults and
  call sites in first-party modules replaced.
- `GarminConnectNotFoundError` and `garminconnect.workout` import failures
  handled with compatibility shims; tests that require garminconnect>=0.3
  skip gracefully with a platform-specific reason.
