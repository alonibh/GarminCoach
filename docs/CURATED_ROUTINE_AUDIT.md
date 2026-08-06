# Curated Routine Audit

**Audit date:** 2026-08-06
**Prior source review date:** 2026-07-22 (detailed exercise and operating-rule audit)
**Re-fetch attempt 2026-08-06:** All 25 muscleandstrength.com source pages timed out.
No silent changes were made. Prior audit data is retained as the authoritative record.

ACSM reference: [ACSM Resistance Training Guidelines Update 2026](https://acsm.org/resistance-training-guidelines-update-2026/)

---

## How to read this document

Each table below uses these columns:

| Field | Meaning |
|---|---|
| **Internal key** | `PROGRAMS` dict key in `coach/programs.py` |
| **Display name** | Name shown in the UI |
| **Source URL** | Muscle & Strength canonical page |
| **Reviewed** | Date of last successful source review |
| **Source sessions** | Session names as found in the source |
| **Implementation status** | Whether implementation matches source |
| **Confirmed matches** | Source-defined aspects that are correctly represented |
| **Corrections made** | Changes applied to align with source |
| **Garmin adaptations** | Intentional platform-driven adaptations |
| **Unresolved source facts** | Facts that could not be verified |

---

## 2-day routines

### full_body_2

| Field | Detail |
|---|---|
| **Internal key** | `full_body_2` |
| **Display name** | A/B Full Body · 2 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/a-b-2-day-workout-for-busy-people |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Full Body A; Full Body B |
| **Implementation status** | Matches source |
| **Confirmed matches** | Exercise order, sets/reps for both sessions; 120 s between-set rest (source: "as needed", 120 s starting prompt); two full-body weekly exposures; non-consecutive-day preference with consecutive-day exception |
| **Corrections made** | None |
| **Garmin adaptations** | Rest value is an adjustable starting prompt, not a source-imposed cap |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

---

## 3-day routines

### beginner_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `beginner_full_body_3` |
| **Display name** | Beginner Full Body · 3 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/3-day-workout-routine-and-diet-for-beginners |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Full Body A; Full Body B; Full Body C |
| **Implementation status** | Matches source |
| **Confirmed matches** | All three session exercise orders; 300 s main-lift rest; 90 s compound rest; 45 s arm/calf/core rest; one full recovery day between sessions |
| **Corrections made** | None |
| **Garmin adaptations** | None |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### ms_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `ms_full_body_3` |
| **Display name** | M&S Full Body · 3 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/muscle-strength-full-body-workout-routine |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Full Body A; Full Body B; Full Body C |
| **Implementation status** | Matches source; warm-up adaptation documented |
| **Confirmed matches** | Exercise order and movements for all three sessions; 120 s major-lift rest; 90 s all-other rest |
| **Corrections made** | None |
| **Garmin adaptations** | Source ramp-up sets replaced by the app's single movement warm-up set per compound (GarminCoach safety overlay) |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### total_package_3

| Field | Detail |
|---|---|
| **Internal key** | `total_package_3` |
| **Display name** | Total Package · 3 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/total-package-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Day 1 (Squat anchor); Day 2 (Bench anchor); Day 3 (Deadlift anchor) |
| **Implementation status** | Matches source |
| **Confirmed matches** | Exercise order for all three sessions; 180 s on Squat/Bench/Deadlift; 60 s on hypertrophy work; 1 rest day between sessions |
| **Corrections made** | None |
| **Garmin adaptations** | Weekday names (Mon/Wed/Fri) converted to rolling rest slots |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### dumbbell_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `dumbbell_full_body_3` |
| **Display name** | Dumbbell Full Body · 3 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/3-day-full-body-dumbbell-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Full Body 1; Full Body 2; Full Body 3 |
| **Implementation status** | Matches source |
| **Confirmed matches** | All three dumbbell-only session sequences; 60 s between-set rest; 1 rest day between sessions; optional cardio treated as separate from resistance routine |
| **Corrections made** | None |
| **Garmin adaptations** | None |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### planet_fitness_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `planet_fitness_full_body_3` |
| **Display name** | Planet Fitness Full Body · 3 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/3-day-full-body-planet-fitness-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Full Body A; Full Body B; Full Body C |
| **Implementation status** | Matches source (compound/primary movements) |
| **Confirmed matches** | Three machine-and-dumbbell lower, pull, and press exposures; source lower-bound rep targets retained |
| **Corrections made** | None |
| **Garmin adaptations** | 60 s between-set rest (ACSM-derived default; source rest guidance not documented in the 2026-07-22 audit) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### long_cycle_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `long_cycle_full_body_3` |
| **Display name** | Long Cycle Full Body · 3 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/beginner-long-cycle-muscle-strength-building-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Workout 1; Workout 2; Workout 3 |
| **Implementation status** | Matches source (compound movements); isolation work reduced as admitted |
| **Confirmed matches** | Three alternating squat/deadlift, press, and row/pull-up sessions; defining compounds retained |
| **Corrections made** | None |
| **Garmin adaptations** | Isolation work reduced vs. source (recorded as omitted component); 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### whole_body_toning_3

| Field | Detail |
|---|---|
| **Internal key** | `whole_body_toning_3` |
| **Display name** | Whole Body Toning · 3 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/3-day-whole-body-toning-workout.html |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Series 1; Series 2; Series 3 |
| **Implementation status** | Matches source; Garmin movement aliases applied |
| **Confirmed matches** | Three short high-repetition full-body sessions; commercial-gym equipment choices |
| **Corrections made** | None |
| **Garmin adaptations** | Dumbbell Fly → Cable Fly (noted in code); Horizontal Leg Raise → Hanging Leg Raise (noted); 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

---

## 4-day routines

### upper_lower_4

| Field | Detail |
|---|---|
| **Internal key** | `upper_lower_4` |
| **Display name** | Upper / Lower Bodybuilding · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/upper-lower-4-day-gym-bodybuilding-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper A; Lower A; Upper B; Lower B |
| **Implementation status** | Matches source |
| **Confirmed matches** | All four sessions' primary movements; 90 s compound rest; 60 s isolation/accessory rest; two-session blocks with intervening rest |
| **Corrections made** | None |
| **Garmin adaptations** | Source Pull Ups/Machine Rows alternative: app records Machine Row; 3-second eccentric tempo not representable in Garmin FIT |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### shul_4

| Field | Detail |
|---|---|
| **Internal key** | `shul_4` |
| **Display name** | SHUL Strength / Hypertrophy · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/shul-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Lower Strength; Upper Strength; Lower Hypertrophy; Upper Hypertrophy |
| **Implementation status** | Matches source (chosen exercise set) |
| **Confirmed matches** | Strength-before-hypertrophy session order; 300 s strength anchor rest; 120 s strength accessory rest; 60 s hypertrophy compound rest; 45 s hypertrophy isolation rest |
| **Corrections made** | None |
| **Garmin adaptations** | Front Squat chosen from (Safety Bar Squat / Goblet Squat / Front Squat) alternatives (source offers all three) |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### split_full_4

| Field | Detail |
|---|---|
| **Internal key** | `split_full_4` |
| **Display name** | Three-Way Split + Full Body · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/4-day-workout-to-build-muscle |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Back & Biceps; Legs; Chest, Shoulders & Triceps; Full Body |
| **Implementation status** | Matches source |
| **Confirmed matches** | Exercise order all four sessions; 45 s uniform rest throughout; three-session block + rest + full-body session order |
| **Corrections made** | None |
| **Garmin adaptations** | None |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### planet_fitness_upper_lower_4

| Field | Detail |
|---|---|
| **Internal key** | `planet_fitness_upper_lower_4` |
| **Display name** | Planet Fitness Upper / Lower · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/4-day-upper-lower-planet-fitness-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper A; Lower A; Upper B; Lower B |
| **Implementation status** | Matches source (compound/primary movements) |
| **Confirmed matches** | Two machine-and-dumbbell upper/lower pairs; isolation work reduced as admitted |
| **Corrections made** | None |
| **Garmin adaptations** | 60 s between-set rest (ACSM-derived default); isolation work reduced |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### optimized_volume_4

| Field | Detail |
|---|---|
| **Internal key** | `optimized_volume_4` |
| **Display name** | Optimized Volume · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/ovw-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper 1; Lower 1; Upper 2; Lower 2 |
| **Implementation status** | Matches source (defining compounds) |
| **Confirmed matches** | Upper/lower structure; source starting rep ranges (4×6 compounds, 3×8 accessories, 3×10/4×10 isolation) |
| **Corrections made** | None |
| **Garmin adaptations** | 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### phul_4

| Field | Detail |
|---|---|
| **Internal key** | `phul_4` |
| **Display name** | PHUL Power / Hypertrophy · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/phul-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper Power; Lower Power; Upper Hypertrophy; Lower Hypertrophy |
| **Implementation status** | Matches source; arm/calf isolation reduced as admitted |
| **Confirmed matches** | Power/hypertrophy session structure; 180 s power-session rest; 60 s hypertrophy rest; upper+lower power and hypertrophy days each train the corresponding region once |
| **Corrections made** | None |
| **Garmin adaptations** | Arm and calf isolation reduced (recorded as omitted component) |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### dumbbell_upper_lower_4

| Field | Detail |
|---|---|
| **Internal key** | `dumbbell_upper_lower_4` |
| **Display name** | Dumbbell Upper / Lower · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/dumbbell-only-upper-lower-workout-routine |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper A; Lower A; Upper B; Lower B |
| **Implementation status** | Matches source |
| **Confirmed matches** | Both upper/lower pairs and dumbbell-only exercise order |
| **Corrections made** | None |
| **Garmin adaptations** | 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### barbell_no_rack_4

| Field | Detail |
|---|---|
| **Internal key** | `barbell_no_rack_4` |
| **Display name** | Barbell Only (No Rack) · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/4-day-barbell-only-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper A; Lower A; Upper B; Lower B |
| **Implementation status** | Matches source; optional direct isolation reduced as admitted |
| **Confirmed matches** | Both upper/lower pairs; source 1½-rep Landmine Squat variation noted |
| **Corrections made** | None |
| **Garmin adaptations** | Optional direct isolation reduced (recorded as omitted component); 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### barbell_upper_lower_4

| Field | Detail |
|---|---|
| **Internal key** | `barbell_upper_lower_4` |
| **Display name** | Home / Gym Barbell · 4 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/home-gym-barbell-workout-routine |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Lower A; Upper A; Lower B; Upper B |
| **Implementation status** | Matches source |
| **Confirmed matches** | Both lower/upper pairs; add-weight progression rule preserved; Weighted Sit Up note retained |
| **Corrections made** | None |
| **Garmin adaptations** | 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

---

## 5-day routines

### muscle_strength_5

| Field | Detail |
|---|---|
| **Internal key** | `muscle_strength_5` |
| **Display name** | Muscle & Strength Building Split · 5 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/5-day-muscle-and-strength-building-workout-split |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper Strength; Lower Strength; Back & Shoulders Size; Chest & Arms Size; Legs Size |
| **Implementation status** | Matches source (main plan movements) |
| **Confirmed matches** | All five sessions' exercise order; 180 s strength-session rest; 90 s size-session rest; 8 published superset pairs correctly encoded; 1 rest after strength block, 1 rest after final size session |
| **Corrections made** | None |
| **Garmin adaptations** | Optional 3×/week ab workout (separate source page) not represented in the five gym sessions |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### maul_5

| Field | Detail |
|---|---|
| **Internal key** | `maul_5` |
| **Display name** | MAUL · 5 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/maul-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper Mechanical; Lower Mechanical; Upper Full; Shoulders & Arms; Lower Full |
| **Implementation status** | Matches source |
| **Confirmed matches** | Mechanical/full upper/lower structure; five-day adaptation split with repeated compound exposure |
| **Corrections made** | None |
| **Garmin adaptations** | 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### dumbbell_split_5

| Field | Detail |
|---|---|
| **Internal key** | `dumbbell_split_5` |
| **Display name** | Dumbbell Split · 5 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/5-day-dumbbell-only-workout-split |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Chest, Shoulders & Triceps; Legs & Core A; Back & Biceps; Legs & Core B; Complete Upper Body |
| **Implementation status** | Matches source |
| **Confirmed matches** | Push, lower, pull, lower, complete-upper sequence; two lower and two complete-upper-region exposures |
| **Corrections made** | None |
| **Garmin adaptations** | 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

---

## 6-day routines

### ppl_6

| Field | Detail |
|---|---|
| **Internal key** | `ppl_6` |
| **Display name** | Push / Pull / Legs A/B · 6 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/6-day-push-pull-legs-planet-fitness-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Push A; Pull A; Legs A; Push B; Pull B; Legs B |
| **Implementation status** | Matches source |
| **Confirmed matches** | All six session exercise orders; 45 s between-set rest (source "sweet spot"); 90 s between-exercise transition timer; Push/Pull/Legs A/B cycle followed by one off day |
| **Corrections made** | None |
| **Garmin adaptations** | Final movement `Leg Press Calf Raise` = source `Leg Press Calf Press` (same movement, normalised name) |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### powerbuilding_ppl_6

| Field | Detail |
|---|---|
| **Internal key** | `powerbuilding_ppl_6` |
| **Display name** | Powerbuilding PPL · 6 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/6-day-powerbuilding-split-meal-plan |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Push A; Pull A; Legs A; Push B; Pull B; Legs B |
| **Implementation status** | Matches source; meal plan excluded as outside resistance routine |
| **Confirmed matches** | Six PPL A/B anchors; rep-goal starting targets; 120 s compound anchor rest; source Intermediate level retained; source three-on/one-off schedule as rolling rest slots |
| **Corrections made** | None |
| **Garmin adaptations** | Source snatch-grip Deadlift variation noted; meal plan explicitly excluded; source progression translated to `powerbuilding_rep_goal_15_v1` rule (GarminCoach safety overlay, not source text) |
| **Unresolved source facts** | Re-fetch timed out 2026-08-06 |

### low_volume_high_intensity_6

| Field | Detail |
|---|---|
| **Internal key** | `low_volume_high_intensity_6` |
| **Display name** | Low-Volume High-Intensity · 6 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/6-day-low-volume-high-intensity-workout-split |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Chest & Triceps; Back Thickness; Quads; Shoulders & Biceps; Back Width; Hamstrings |
| **Implementation status** | Matches source |
| **Confirmed matches** | Six focused low-volume sessions; two-working-set method; source heavy starting rep targets |
| **Corrections made** | None |
| **Garmin adaptations** | Barbell Glute Bridge used for "barbell glute bridge" source note in Hamstrings session; 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### built_different_ppl_6

| Field | Detail |
|---|---|
| **Internal key** | `built_different_ppl_6` |
| **Display name** | Built Different PPL · 6 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/built-different-ppl-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Push 1; Pull 1; Legs 1; Push 2; Pull 2; Legs 2 |
| **Implementation status** | Matches source |
| **Confirmed matches** | Both PPL rotations; two distinct exposures for every major region |
| **Corrections made** | None |
| **Garmin adaptations** | Optional morning LISS omitted (not a resistance session); 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

### muscle_mania_6

| Field | Detail |
|---|---|
| **Internal key** | `muscle_mania_6` |
| **Display name** | Muscle Mania Upper / Lower · 6 days |
| **Source URL** | https://www.muscleandstrength.com/workouts/muscle-mania-10-week-muscle-growth-workout |
| **Reviewed** | 2026-07-22 |
| **Source sessions** | Upper 1; Lower 1; Upper 2; Lower 2; Upper 3; Lower 3 |
| **Implementation status** | Matches source; isolation work reduced as admitted |
| **Confirmed matches** | Three distinct upper/lower rotations; major muscle groups trained three times weekly |
| **Corrections made** | None |
| **Garmin adaptations** | Isolation work reduced (recorded as omitted component); 60 s between-set rest (ACSM-derived default) |
| **Unresolved source facts** | Between-set rest rule not captured in 2026-07-22 audit; re-fetch timed out 2026-08-06 |

---

## Summary

| Stat | Count |
|---|---|
| Total routines | 25 |
| Source pages successfully reviewed | 25 (2026-07-22) |
| Re-fetch attempt 2026-08-06 | 0 of 25 available (all timed out) |
| Routines with full exercise + rest + operating-rule audit | 10 (original catalog) |
| Routines with exercise + structural audit only | 15 (2026-07-22 expansion) |
| Corrections implemented this audit cycle | 0 |
| Garmin adaptations retained | 10 (movement aliases, tempo not representable, isolation reductions, warm-up overlay) |
| ACSM-derived defaults used | 15 between-set rest values (expansion routines where source rest guidance not documented) |
| Unresolved source facts | Between-set rest for 15 expansion routines (re-fetch failed 2026-08-06) |

## Distinction of sources for each field

| Field type | Source |
|---|---|
| Exercise order, sets, reps | Source-defined (Muscle & Strength) |
| Warm-up set count and rest (60 s) | GarminCoach safety overlay |
| Between-set rest for original 10 routines | Source-defined (audited 2026-07-18 and 2026-07-22) |
| Between-set rest for 15 expansion routines | ACSM-derived default (60 s) pending source re-fetch |
| Superset pairs (`muscle_strength_5`, `ppl_6`) | Source-defined |
| Training level badges | Source-defined (never inferred) |
| Session rolling rest slots | Source-defined (weekday names converted to rolling slots) |
| Optional cardio exclusion | Source-defined: app excludes cardio sessions entirely |
| Movement aliases (Cable Fly, Hanging Leg Raise, etc.) | Garmin technical limitation |
| Isolation reduction | Documented admission adaptation (catalog gate passed) |

## Prior detailed audits

- `docs/routine_source_audit.md` — detailed exercise comparison, between-set rest, and operating-rule audit (2026-07-18 through 2026-07-22)
- `docs/routine_catalog_admission.md` — admission criteria, replacement table, and enforcement (reviewed 2026-07-22)
- `docs/SOURCE_EXECUTION_FIDELITY_CONTRACT.md` — superset and transition encoding contract
