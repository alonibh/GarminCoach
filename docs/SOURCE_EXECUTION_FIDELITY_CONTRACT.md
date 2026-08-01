# Source execution fidelity contract (Phase 5A)

Phase 5A represents only an ordinary exercise block and a consecutive
two-exercise superset block. `SessionExercise` retains independent stable rows
and IDs. `superset_group` is nullable `String(32)`, uses ASCII letters, digits,
`_`, and `-`, and a non-null group is exactly two consecutive rows with equal
positive sets, `rest_seconds`, and `transition_rest_seconds`.

`transition_rest_seconds` is a nullable integer from 0 through 600. `NULL`
retains legacy compilation; zero deliberately means no transition timer. The
editor/API validates scalars and then validates detached complete submitted
rows through the compiler's pure block builder before invalidating, deleting,
or writing durable rows. A one-row delete is rejected for a grouped member;
both members must be cleared or removed in one full-session save.

The `muscle_strength_5` size sessions use 90 seconds for both round/block rests and encode eight published pairs: Wide/Narrow Grip Pull Down; Straight Arm Rope Pull Down/Lower Back Hyperextensions; Cable EZ Bar Upright Row/Rope Face Pull; Flat Machine Chest Press/Incline Dumbbell Fly; Seated Hamstring Curl/Leg Extension; Leg Press/Barbell Walking Lunge; Abductor/Adductor Machine; Seated Calf Raise/Single Leg Calf Press. Its strength sessions remain legacy. All `ppl_6` rows are straight blocks with 45 seconds between sets and 90 seconds between exercises.

The pure compiler orders by `(order_index, id)`. Legacy straight rows retain their old repeat-group payload. Structured straight rows use a repeat group with `skipLastRestStep=true`, then an optional standalone transition rest before another block. A superset repeat group contains A work, B work, then round rest; it skips only the final round rest when a transition is specified. The final block has no transition. Enabled member warm-ups precede their block in member order. Every top-level and nested step counts toward Garmin's 50-step limit and is reindexed deterministically.

Editor/API payloads round-trip both fields. Missing old-client fields become `NULL`; invalid structure fails locally with 422 and rolls back. Returned IDs remain authoritative. Execution fields participate in pending scheduling's program version, but do not enter strength identity, evidence, streaks, or proposals.

Tenant migration key `session_exercise_execution_fidelity_2026_08_01_v1` adds
the columns and guardedly migrates only unchanged source structure. A muscle
pair requires exact source session, name, order, sets, reps/duration, rest,
non-generic rows, and null execution fields; a failed member skips the whole
pair. Each ungrouped size row is independently guarded. PPL requires every
row in the session to match the same shape before adding any transitions.
Weights, notes, Garmin identities, warm-ups, IDs, custom rows, custom rests,
existing execution fields, cursor, planned sessions, and Garmin IDs are never
overwritten.

Migration, editing, and pure compilation make no external calls and tenant
isolation applies. Only future confirmed compilation can upload; scheduled
workouts are immutable. Read-back verifies nested type, iteration count, child
order, identities, prescriptions, rests, transitions, and `skipLastRestStep`;
rejection follows safe cleanup and creates no planned session. Execution
metadata participates in scheduling revalidation but not strength identity,
progression evidence, proposals, coaching, biometric, or decision authority.
Acceptance tests cover legacy parity, grouped/transition payloads, 50/51 step
boundaries, migration guards, editor atomicity, read-back rejection, cursor,
scheduled-workout, progression, and no-external-operation boundaries.
