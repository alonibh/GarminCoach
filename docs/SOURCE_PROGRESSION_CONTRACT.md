# Source progression contract

`powerbuilding_rep_goal_15_v1` is the only supported source-specific rule. It
is owned by an exact, server-tagged `SessionExercise`, never inferred from its
name, and is based on the Powerbuilding PPL source at
https://www.muscleandstrength.com/workouts/6-day-powerbuilding-split-meal-plan.

It applies only to order-zero `5 x 3`, non-generic, weighted rows: Push A
Barbell Bench Press; Pull A Deadlift; Legs A Barbell Back Squat; Push B Standing
Overhead Barbell Press; Pull B Deadlift; and Legs B Front Squat. The two
Deadlift rows are independent owners.

The first five chronological working sets must be complete, have positive
integer reps, and match the current normalized template weight exactly. Rest
and exact warm-ups are excluded; ambiguous preparation, invalid data, or an
incomplete payload is unscorable. A known-complete payload with fewer than five
sets is neutral. Extra sets remain audit-only and cannot replace the first five.

The source's 15-rep goal is calculated locally: 15–18 total reps is the low
tier (+1,250 grams / +1.25 kg); 19 or more is high (+2,250 grams / +2.25 kg);
0–14 is neutral. These are conservative lower published pound bounds converted
once with decimal round-half-up to the existing 250 g quantum. Two consecutive
qualified appearances with the same policy, prescription and exact rule are
required within 35 days; GarminCoach deliberately adds this safety overlay and
does not attribute it to the source. The lower tier wins across the two
appearances. Unscorable evidence preserves a streak subject to expiry; neutral
evidence resets it. There is no source decrease proposal.

Evidence is immutable and stores rule ownership, total/target reps, source
increment, selected-set facts, exclusions, and correction provenance. Pending
proposals are replaced only when their material source tier changes, stale on a
new neutral result or prescription/rule change, and are reset behind rejection
boundaries. Approval is web-only and revalidates the exact current row,
prescription, policy, evidence heads, tiers and boundary before changing only
`SessionExercise.weight_kg`; rejection requires two entirely new qualifying
appearances. Notifications are deduplicated outbox summaries with no actions.

Rule metadata is server-owned. Cosmetic edits preserve it; identity, mapping,
session ownership, order, sets, reps, duration, deletion, or making the source
session custom clears it and stales pending source work. Reset restores the
catalog tag with a new prescription. The tenant migration is additive and only
tags uncustomized exact catalog rows; it preserves row IDs, notes, weights,
warm-ups, scheduling, cursors, and Garmin IDs. No control migration exists.

This deterministic local rule has no Garmin, biometric, AI, calendar, or
scheduling authority. It never changes already compiled/scheduled workouts;
approved local weights are consumed only by future compilation. All ordinary
rows retain the generic contract and all other source systems remain unsupported.
