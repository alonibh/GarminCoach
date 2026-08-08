# Source execution fidelity contract

The compiler produces deterministic sequential exercise blocks. `SessionExercise`
retains independent stable rows and IDs. `transition_rest_seconds` is a nullable
integer from 0 through 600. `NULL` retains legacy compilation semantics (final
rest included in the repeat group); zero deliberately means no transition timer.

The editor/API validates scalars and then validates detached complete submitted
rows through the compiler's pure block builder before invalidating, deleting,
or writing durable rows.

All `ppl_6` rows are straight blocks with 45 seconds between sets and 90 seconds
of transition rest between exercises.

The pure compiler orders by `(order_index, id)`. Legacy straight rows retain
their old repeat-group payload. Structured straight rows use a repeat group with
`skipLastRestStep=true`, then an optional standalone transition rest before the
next block. The final block has no transition. Enabled warm-ups precede their
block. Every top-level and nested step counts toward Garmin's 50-step limit and
is reindexed deterministically.

Editor/API payloads round-trip `transition_rest_seconds`. Legacy clients may
send `superset_group`; it is silently ignored. Missing old-client fields become
`NULL`; invalid structure fails locally with 422 and rolls back. Returned IDs
remain authoritative. Execution fields participate in pending scheduling's
program version, but do not enter strength identity, evidence, streaks, or
proposals.

Tenant migration key `session_exercise_execution_fidelity_2026_08_01_v1`
guardedly migrates only unchanged PPL source structure. A session requires every
row to match source shape before adding transitions. Weights, notes, Garmin
identities, warm-ups, IDs, custom rows, custom rests, existing execution fields,
cursor, planned sessions, and Garmin IDs are never overwritten.

Migration, editing, and pure compilation make no external calls and tenant
isolation applies. Only future confirmed compilation can upload; scheduled
workouts are immutable. Read-back verifies nested type, iteration count, child
order, identities, prescriptions, rests, transitions, and `skipLastRestStep`;
rejection follows safe cleanup and creates no planned session.
