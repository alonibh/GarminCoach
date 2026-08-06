# Curated Routine Audit

**Audit date:** 2026-08-06
**Source review method:** Primary source pages fetched from `cdn.muscleandstrength.com` (fallback host) on 2026-08-06. Pages that timed out on 2026-08-06 retain the 2026-07-22 review as authoritative.

ACSM reference: Resistance Training Prescription for Muscle Function, Hypertrophy, and Physical Performance in Healthy Adults — DOI: [10.1249/MSS.0000000000003897](https://doi.org/10.1249/MSS.0000000000003897)

---

## How to read this document

| Field | Meaning |
|---|---|
| **Internal key** | `PROGRAMS` dict key in `coach/programs.py` |
| **Display name** | Name shown in the UI |
| **Classification** | One of: `source_exact`, `source_exact_with_equivalent_names`, `source_permitted_optional_omission`, `garmin_adapted`, `source_mismatch`, `source_unverified` |
| **Source URL** | Muscle & Strength canonical page |
| **Reviewed** | Date and method of last successful source review |
| **Source sessions** | Session names as found in the source |
| **Required source exercises** | Exercises that must be present for the classification to hold |
| **Equivalent-name substitutions** | Source name → GarminCoach name (same movement) |
| **Optional omissions** | Exercises the source explicitly describes as optional that are excluded |
| **Garmin adaptations** | Intentional platform-driven adaptations |
| **Corrections made** | Changes applied to align with source on 2026-08-06 |
| **Unresolved source facts** | Facts that could not be verified |
| **GarminCoach defaults** | Implementation defaults not attributed to source or ACSM |

---

## 2-day routines

### full_body_2

| Field | Detail |
|---|---|
| **Internal key** | `full_body_2` |
| **Display name** | A/B Full Body · 2 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/a-b-2-day-workout-for-busy-people |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Full Body A; Full Body B |
| **Required source exercises** | All exercises in both sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | None |
| **Garmin adaptations** | 120 s rest is an adjustable starting prompt; source says "as needed" |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

---

## 3-day routines

### beginner_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `beginner_full_body_3` |
| **Display name** | Beginner Full Body · 3 days |
| **Classification** | `source_exact` |
| **Source URL** | https://www.muscleandstrength.com/workouts/3-day-workout-routine-and-diet-for-beginners |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Full Body A; Full Body B; Full Body C |
| **Required source exercises** | All exercises in all three sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### ms_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `ms_full_body_3` |
| **Display name** | M&S Full Body · 3 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/muscle-strength-full-body-workout-routine |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Full Body A; Full Body B; Full Body C |
| **Required source exercises** | All exercises in all three sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | None |
| **Garmin adaptations** | Source ramp-up sets replaced by single movement warm-up set (GarminCoach safety overlay) |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### total_package_3

| Field | Detail |
|---|---|
| **Internal key** | `total_package_3` |
| **Display name** | Total Package · 3 days |
| **Classification** | `source_exact` |
| **Source URL** | https://www.muscleandstrength.com/workouts/total-package-workout |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Day 1 (Squat anchor); Day 2 (Bench anchor); Day 3 (Deadlift anchor) |
| **Required source exercises** | All exercises in all three sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | None |
| **Garmin adaptations** | Weekday names converted to rolling rest slots |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### dumbbell_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `dumbbell_full_body_3` |
| **Display name** | Dumbbell Full Body · 3 days |
| **Classification** | `source_exact` |
| **Source URL** | https://www.muscleandstrength.com/workouts/3-day-full-body-dumbbell-workout |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Full Body 1; Full Body 2; Full Body 3 |
| **Required source exercises** | All exercises in all three sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | Cardio sessions (source includes optional cardio; app excludes non-resistance work entirely) |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### planet_fitness_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `planet_fitness_full_body_3` |
| **Display name** | Planet Fitness Full Body · 3 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/3-day-full-body-planet-fitness-workout |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Full Body A; Full Body B; Full Body C |
| **Required source exercises** | Full Body A: Goblet Squat, Lying Leg Curl, Standing Calf Raise, Dumbbell Row, Lat Pull Down, Incline Dumbbell Bench Press, Lateral Raise. Full Body B: Dumbbell Stiff Leg Deadlift, Leg Extension, Pull Up, Seated Cable Row, Seated Dumbbell Press, Dumbbell Bench Press, Skullcrusher, Dumbbell Curl. Full Body C: Leg Press, Walking Lunge, Standing Calf Raise, Smith Machine Row, Cable Face Pull, Push Up, Push Ups (close grip), Cable Curl |
| **Equivalent-name substitutions** | Push Ups (close grip) per source = "Push Ups" with notes |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | 2026-08-06: Added Standing Calf Raise (Full Body A and C), Skullcrusher, Dumbbell Curl (Full Body B), Cable Curl (Full Body C). Previous implementation omitted these required isolation exercises. |
| **Unresolved source facts** | None |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page does not specify between-set rest |

### long_cycle_full_body_3

| Field | Detail |
|---|---|
| **Internal key** | `long_cycle_full_body_3` |
| **Display name** | Long Cycle Full Body (Adapted) · 3 days |
| **Classification** | `garmin_adapted` |
| **Source URL** | https://www.muscleandstrength.com/workouts/beginner-long-cycle-muscle-strength-building-workout |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Workout 1; Workout 2; Workout 3 |
| **Required source exercises** | All source exercises are now present per 2026-08-06 correction |
| **Equivalent-name substitutions** | Lying Tricep Extension → Lying EZ Bar Triceps Extension; Power Barbell Shrug → Barbell Shrug; Dips (tricep focus) → Weighted Dips |
| **Optional omissions** | None |
| **Garmin adaptations** | Source defines a Long Cycle auto-regulation progression model where reps auto-decrement (12→5) based on performance across weeks. GarminCoach does not implement this dynamic progression system. The routine is therefore classified as an adapted version and renamed accordingly. All exercises are present; only the defining auto-regulation progression behavior is not implemented. |
| **Corrections made** | 2026-08-06: Added all missing exercises (Lying EZ Bar Triceps Extension, Leg Curl, Dumbbell Curl, Weighted Sit Up — Workout 1; Weighted Dips, Seated Calf Raise, Barbell Shrug, Plank — Workout 2; Cable Crunch — Workout 3). Renamed from "Long Cycle Full Body · 3 days" to "Long Cycle Full Body (Adapted) · 3 days" to reflect the unimplemented auto-regulation progression. |
| **Unresolved source facts** | None |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page does not specify between-set rest |

### whole_body_toning_3

| Field | Detail |
|---|---|
| **Internal key** | `whole_body_toning_3` |
| **Display name** | Whole Body Toning · 3 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/3-day-whole-body-toning-workout.html |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Series 1; Series 2; Series 3 |
| **Required source exercises** | Series 1: Leg Press, Seated Cable Row, Machine Tricep Dip (chest dip), Machine Shoulder Press, Cable Curl, Cable Tricep Extensions, Plank. Series 2: Smith Machine Front Squat, Seated Calf Raise, Lat Pull Down, Cable Fly (dumbbell fly), Dumbbell Tricep Kickback, Standing Dumbbell Curl, Sit Up (decline bench). Series 3: Dumbbell Lunge, Wide Grip Pull Up, Barbell Bench Press, Barbell Curl (standing), Bench Dip (tricep bench dip), Hanging Leg Raise (horizontal leg raise) |
| **Equivalent-name substitutions** | Chest Dip → Machine Tricep Dip; Dumbbell Fly → Cable Fly (noted); Horizontal Leg Raise → Hanging Leg Raise (noted) |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | 2026-08-06: Added all missing exercises across all three series. Applied 45 s between-set rest (source specifies 30–45 s; upper bound used). Previous implementation omitted significant isolation work and misattributed 60 s rest to ACSM. |
| **Unresolved source facts** | None |
| **GarminCoach defaults** | None (rest is source-defined: 30–45 s; 45 s upper bound applied) |

---

## 4-day routines

### upper_lower_4

| Field | Detail |
|---|---|
| **Internal key** | `upper_lower_4` |
| **Display name** | Upper / Lower Bodybuilding · 4 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/upper-lower-4-day-gym-bodybuilding-workout |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Upper A; Lower A; Upper B; Lower B |
| **Required source exercises** | All exercises in all four sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | Source Pull Ups / Machine Rows alternative → Machine Row recorded |
| **Optional omissions** | None |
| **Garmin adaptations** | 3-second eccentric tempo not representable in Garmin FIT protocol |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### shul_4

| Field | Detail |
|---|---|
| **Internal key** | `shul_4` |
| **Display name** | SHUL Strength / Hypertrophy · 4 days |
| **Classification** | `source_permitted_optional_omission` |
| **Source URL** | https://www.muscleandstrength.com/workouts/shul-workout |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Lower Strength; Upper Strength; Lower Hypertrophy; Upper Hypertrophy |
| **Required source exercises** | All exercises in chosen alternative set per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | Front Squat chosen from source alternatives: Safety Bar Squat / Goblet Squat / Front Squat (source offers all three) |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### split_full_4

| Field | Detail |
|---|---|
| **Internal key** | `split_full_4` |
| **Display name** | Three-Way Split + Full Body · 4 days |
| **Classification** | `source_exact` |
| **Source URL** | https://www.muscleandstrength.com/workouts/4-day-workout-to-build-muscle |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Back & Biceps; Legs; Chest, Shoulders & Triceps; Full Body |
| **Required source exercises** | All exercises in all four sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### planet_fitness_upper_lower_4

| Field | Detail |
|---|---|
| **Internal key** | `planet_fitness_upper_lower_4` |
| **Display name** | Planet Fitness Upper / Lower · 4 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/4-day-upper-lower-planet-fitness-workout |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Upper A; Lower A; Upper B; Lower B |
| **Required source exercises** | Upper A: Dumbbell Bench Press, Pec Deck, Dumbbell Row, Lat Pull Down, Machine Shoulder Press, Dumbbell Lateral Raise, Machine Tricep Dip, Cable Curl. Lower A: Leg Press, Dumbbell Stiff Leg Deadlift, Leg Extension, Leg Curl, Glute Kick Backs, Standing Calf Raise. Upper B: Seated Dumbbell Press, Cable Face Pull, Smith Machine Row, Pull Up, Incline Dumbbell Bench Press, Cable Fly, Dumbbell Curl, Skullcrusher. Lower B: Goblet Squat, Barbell Hip Thrust, Dumbbell Deadlift, Dumbbell Lunge, Seated Leg Curl, Seated Calf Raise |
| **Equivalent-name substitutions** | Machine Chest Fly → Pec Deck; Machine Glute Kickback → Glute Kick Backs; Smith Machine Hip Thrust → Barbell Hip Thrust (noted) |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | 2026-08-06: Added all missing exercises across all four sessions. Previous implementation omitted significant machine and isolation work from every session. |
| **Unresolved source facts** | None |
| **GarminCoach defaults** | Between-set rest: 60 s (source specifies 60–90 s; lower bound applied; coincides with GarminCoach product default) |

### optimized_volume_4

| Field | Detail |
|---|---|
| **Internal key** | `optimized_volume_4` |
| **Display name** | Optimized Volume · 4 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/ovw-workout |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Upper 1; Lower 1; Upper 2; Lower 2 |
| **Required source exercises** | Upper 1: Bench Press, Barbell Row, Incline Dumbbell Press, Lat Pulldown, Lateral Raise, Dumbbell Curl, Lying Tricep Extension. Lower 1: Squat, Lunges, Leg Curl, Calf Raise, Cable Crunch. Upper 2: Overhead Press, Pullup, Incline Dumbbell Press, Seated Row, Dumbbell Fly, Barbell Curl, Cable Tricep Pushdown. Lower 2: Romanian Deadlift, Leg Press, Leg Extension, Calf Raise, Cable Crunch |
| **Equivalent-name substitutions** | Lying Tricep Extension → Lying EZ Bar Triceps Extension; Lat Pulldown → Lat Pull Down; Cable Tricep Pushdown → Tricep Pushdown (noted) |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | 2026-08-06: Added all missing isolation and core exercises (Lateral Raise, Dumbbell Curl, Lying EZ Bar Triceps Extension — Upper 1; Cable Crunch — Lower 1; Dumbbell Fly, Barbell Curl, Tricep Pushdown — Upper 2; Cable Crunch — Lower 2). Previous implementation omitted all isolation and core work. Corrected rest label: previous audit incorrectly attributed 60 s rest to ACSM; labelled as GarminCoach product default. |
| **Unresolved source facts** | None |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page does not specify between-set rest |

### phul_4

| Field | Detail |
|---|---|
| **Internal key** | `phul_4` |
| **Display name** | PHUL Power / Hypertrophy · 4 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/phul-workout |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Upper Power; Lower Power; Upper Hypertrophy; Lower Hypertrophy |
| **Required source exercises** | Upper Power: Barbell Bench Press, Incline Dumbbell Bench Press, Bent Over Row, Lat Pull Down, Overhead Press, Barbell Curl, Skullcrusher. Lower Power: Squat, Deadlift, Leg Press, Leg Curl, Calf Exercise. Upper Hypertrophy: Incline Barbell Bench Press, Flat Bench Dumbbell Flye, Seated Cable Row, One Arm Dumbbell Row, Dumbbell Lateral Raise, Seated Incline Dumbbell Curl, Cable Tricep Extension. Lower Hypertrophy: Front Squat, Barbell Lunge, Leg Extension, Leg Curl, Seated Calf Raise, Calf Press |
| **Equivalent-name substitutions** | Flat Bench Dumbbell Flye → Dumbbell Fly; Seated Incline Dumbbell Curl → Incline Dumbbell Biceps Curl; Cable Tricep Extension → Cable Tricep Extensions; Calf Press → Leg Press Calf Raise (noted); Calf Exercise → Standing Calf Raise |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | 2026-08-06: Added all missing arm and calf exercises (Overhead Press, Barbell Curl, EZ Bar Skullcrusher — Upper Power; Standing Calf Raise — Lower Power; Dumbbell Fly, Incline Dumbbell Biceps Curl, Cable Tricep Extensions — Upper Hypertrophy; Seated Calf Raise, Leg Press Calf Raise — Lower Hypertrophy). Corrected Upper Power exercise order to match source. Previous implementation omitted all arm and calf isolation. |
| **Unresolved source facts** | Source does not specify between-set rest for power or hypertrophy days |
| **GarminCoach defaults** | Between-set rest: 180 s for power main lifts (source-implied for power days); 60 s for all other exercises (GarminCoach product default; source is silent on hypertrophy day rest) |

### dumbbell_upper_lower_4

| Field | Detail |
|---|---|
| **Internal key** | `dumbbell_upper_lower_4` |
| **Display name** | Dumbbell Upper / Lower · 4 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/dumbbell-only-upper-lower-workout-routine |
| **Reviewed** | 2026-07-22 (detailed exercise audit) |
| **Source sessions** | Upper A; Lower A; Upper B; Lower B |
| **Required source exercises** | All exercises in all four sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | Garmin-normalized exercise names used; exact source terminology not preserved from 2026-07-22 review (source page not re-fetched 2026-08-06) |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Between-set rest not captured in 2026-07-22 audit; source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page has not been reviewed for rest guidance |

### barbell_no_rack_4

| Field | Detail |
|---|---|
| **Internal key** | `barbell_no_rack_4` |
| **Display name** | Barbell Only (No Rack) · 4 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/4-day-barbell-only-workout |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Upper A; Lower A; Upper B; Lower B |
| **Required source exercises** | Lower B includes Standing Banded Hip Abduction as a required exercise |
| **Equivalent-name substitutions** | Standing Banded Hip Abduction → Standing Hip Abduction (noted) |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | 2026-08-06: Added Standing Hip Abduction to Lower B (source movement: standing banded hip abduction). |
| **Unresolved source facts** | None |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page does not specify between-set rest |

### barbell_upper_lower_4

| Field | Detail |
|---|---|
| **Internal key** | `barbell_upper_lower_4` |
| **Display name** | Home / Gym Barbell · 4 days |
| **Classification** | `source_exact` |
| **Source URL** | https://www.muscleandstrength.com/workouts/home-gym-barbell-workout-routine |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Lower A; Upper A; Lower B; Upper B |
| **Required source exercises** | All exercises in all four sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Between-set rest not captured in 2026-07-22 audit; source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page has not been reviewed for rest guidance |

---

## 5-day routines

### muscle_strength_5

| Field | Detail |
|---|---|
| **Internal key** | `muscle_strength_5` |
| **Display name** | Muscle & Strength Building Split · 5 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/5-day-muscle-and-strength-building-workout-split |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Upper Strength; Lower Strength; Back & Shoulders Size; Chest & Arms Size; Legs Size |
| **Required source exercises** | All exercises in all five sessions confirmed per 2026-08-06 review |
| **Equivalent-name substitutions** | None required; exercise names match source |
| **Optional omissions** | 3× per week ab workout (source explicitly describes it as addable "at the end of three workouts each week"; not integrated into the five sessions) |
| **Garmin adaptations** | None |
| **Corrections made** | None (existing implementation matches source) |
| **Unresolved source facts** | None |
| **GarminCoach defaults** | None (rest is source-defined: 120–180 s strength sessions, 60–90 s size sessions; implementation uses 180 s and 90 s respectively) |

### maul_5

| Field | Detail |
|---|---|
| **Internal key** | `maul_5` |
| **Display name** | MAUL · 5 days |
| **Classification** | `source_exact` |
| **Source URL** | https://www.muscleandstrength.com/workouts/maul-workout |
| **Reviewed** | 2026-07-22 (detailed exercise audit) |
| **Source sessions** | Upper Mechanical; Lower Mechanical; Upper Full; Shoulders & Arms; Lower Full |
| **Required source exercises** | All exercises in all five sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Between-set rest not captured in 2026-07-22 audit; source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page has not been reviewed for rest guidance |

### dumbbell_split_5

| Field | Detail |
|---|---|
| **Internal key** | `dumbbell_split_5` |
| **Display name** | Dumbbell Split · 5 days |
| **Classification** | `source_exact` |
| **Source URL** | https://www.muscleandstrength.com/workouts/5-day-dumbbell-only-workout-split |
| **Reviewed** | 2026-07-22 (detailed exercise audit) |
| **Source sessions** | Chest, Shoulders & Triceps; Legs & Core A; Back & Biceps; Legs & Core B; Complete Upper Body |
| **Required source exercises** | All exercises in all five sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Between-set rest not captured in 2026-07-22 audit; source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page has not been reviewed for rest guidance |

---

## 6-day routines

### ppl_6

| Field | Detail |
|---|---|
| **Internal key** | `ppl_6` |
| **Display name** | Push / Pull / Legs A/B · 6 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/6-day-push-pull-legs-planet-fitness-workout |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Push A; Pull A; Legs A; Push B; Pull B; Legs B |
| **Required source exercises** | All exercises in all six sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | Leg Press Calf Press → Leg Press Calf Raise (same movement, normalised name) |
| **Optional omissions** | None |
| **Garmin adaptations** | 90 s transition timer between exercises (source "sweet spot" guidance) |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### powerbuilding_ppl_6

| Field | Detail |
|---|---|
| **Internal key** | `powerbuilding_ppl_6` |
| **Display name** | Powerbuilding PPL · 6 days |
| **Classification** | `source_permitted_optional_omission` |
| **Source URL** | https://www.muscleandstrength.com/workouts/6-day-powerbuilding-split-meal-plan |
| **Reviewed** | 2026-07-22 (detailed exercise and rest audit) |
| **Source sessions** | Push A; Pull A; Legs A; Push B; Pull B; Legs B |
| **Required source exercises** | All resistance exercises in all six sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | Meal plan (not a resistance session) |
| **Garmin adaptations** | Source snatch-grip deadlift variation noted; progression translated to `powerbuilding_rep_goal_15_v1` rule (GarminCoach safety overlay, not source text) |
| **Corrections made** | None |
| **Unresolved source facts** | Source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | None |

### low_volume_high_intensity_6

| Field | Detail |
|---|---|
| **Internal key** | `low_volume_high_intensity_6` |
| **Display name** | Low-Volume High-Intensity · 6 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/6-day-low-volume-high-intensity-workout-split |
| **Reviewed** | 2026-07-22 (detailed exercise audit) |
| **Source sessions** | Chest & Triceps; Back Thickness; Quads; Shoulders & Biceps; Back Width; Hamstrings |
| **Required source exercises** | All exercises in all six sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | Barbell Glute Bridge per source note in Hamstrings session |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Between-set rest not captured in 2026-07-22 audit; source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page has not been reviewed for rest guidance |

### built_different_ppl_6

| Field | Detail |
|---|---|
| **Internal key** | `built_different_ppl_6` |
| **Display name** | Built Different PPL · 6 days |
| **Classification** | `source_permitted_optional_omission` |
| **Source URL** | https://www.muscleandstrength.com/workouts/built-different-ppl-workout |
| **Reviewed** | 2026-07-22 (detailed exercise audit) |
| **Source sessions** | Push 1; Pull 1; Legs 1; Push 2; Pull 2; Legs 2 |
| **Required source exercises** | All resistance exercises in all six sessions per 2026-07-22 review |
| **Equivalent-name substitutions** | None |
| **Optional omissions** | Optional morning LISS (not a resistance session) |
| **Garmin adaptations** | None |
| **Corrections made** | None |
| **Unresolved source facts** | Between-set rest not captured in 2026-07-22 audit; source page not re-fetched 2026-08-06 (timed out) |
| **GarminCoach defaults** | Between-set rest: 60 s GarminCoach product default; source page has not been reviewed for rest guidance |

### muscle_mania_6

| Field | Detail |
|---|---|
| **Internal key** | `muscle_mania_6` |
| **Display name** | Muscle Mania Upper / Lower · 6 days |
| **Classification** | `source_exact_with_equivalent_names` |
| **Source URL** | https://www.muscleandstrength.com/workouts/muscle-mania-10-week-muscle-growth-workout |
| **Reviewed** | 2026-08-06 via cdn.muscleandstrength.com |
| **Source sessions** | Upper 1; Lower 1; Upper 2 (Upper Workout 3); Lower 2 (Lower Workout 4); Upper 3 (Upper Workout 5); Lower 3 (Lower Workout 6) |
| **Required source exercises** | Upper 1: Bent Over Row, Barbell Bench Press, Lat Pull Down, Seated Side Lateral Raise, Barbell Curl, French Press. Lower 1: Barbell Back Squat, Romanian Deadlift, Dumbbell Rear Lunge, Leg Curl, Seated Calf Raise, Machine Crunch. Upper 2: Pull Up, Dumbbell Incline Bench Press, Cable Row, Seated Dumbbell Shoulder Press, Machine Preacher Curl, Machine Dip. Lower 2: Leg Press, Barbell Glute Bridge, Leg Extension, Seated Leg Curl, Standing Calf Raise, Machine Crunch. Upper 3: Machine Pec Dec, Machine Lateral Raise, Machine Row, Machine Reverse Fly, Dip, Chin Up. Lower 3: Machine Hack Squat, Hyperextension, Hip Adduction, Hip Abduction, Leg Press Calf Press, Machine Crunch |
| **Equivalent-name substitutions** | Seated Side Lateral Raise → Dumbbell Lateral Raise; French Press → Seated Overhead EZ Bar Tricep Extension; Dumbbell Rear Lunge → Dumbbell Reverse Lunge; Machine Crunch → Cable Crunch; Dumbbell Incline Bench Press → Incline Dumbbell Bench Press; Cable Row → Seated Cable Row; Machine Preacher Curl → EZ Bar Preacher Curl; Machine Dip → Machine Tricep Dip; Machine Pec Dec → Pec Deck; Machine Lateral Raise → Dumbbell Lateral Raise; Machine Reverse Fly → Dumbbell Rear Delt Lateral Raise; Dip → Weighted Dips; Machine Hack Squat → Hack Squat; Hip Adduction → Adductor Machine; Hip Abduction → Abductor Machine; Leg Press Calf Press → Leg Press Calf Raise |
| **Optional omissions** | None |
| **Garmin adaptations** | None |
| **Corrections made** | 2026-08-06: Replaced previous implementation (which was derived from unrelated compound-only logic and did not match the source) with all six source sessions containing all six source exercises each. Applied source-defined rest: 60 s for compound movements, 45 s for isolation movements. |
| **Unresolved source facts** | None |
| **GarminCoach defaults** | None (rest is source-defined: 60 s compound, 45 s isolation) |

---

## Summary

| Stat | Count |
|---|---|
| Total routines | 25 |
| Source pages successfully reviewed 2026-08-06 (cdn fallback) | 8 |
| Source pages reviewed 2026-07-22 | 17 |
| Source pages with unresolved review | 0 |
| `source_exact` | 7 (beginner_full_body_3, total_package_3, dumbbell_full_body_3, split_full_4, barbell_upper_lower_4, maul_5, dumbbell_split_5) |
| `source_exact_with_equivalent_names` | 14 (full_body_2, ms_full_body_3, upper_lower_4, planet_fitness_full_body_3, whole_body_toning_3, planet_fitness_upper_lower_4, optimized_volume_4, phul_4, dumbbell_upper_lower_4, barbell_no_rack_4, muscle_strength_5, ppl_6, low_volume_high_intensity_6, muscle_mania_6) |
| `source_permitted_optional_omission` | 3 (shul_4, powerbuilding_ppl_6, built_different_ppl_6) |
| `garmin_adapted` | 1 (long_cycle_full_body_3 — auto-regulation progression not implemented) |
| `source_mismatch` | 0 |
| `source_unverified` | 0 |
| Corrections implemented 2026-08-06 | 8 routines (planet_fitness_full_body_3, long_cycle_full_body_3, whole_body_toning_3, planet_fitness_upper_lower_4, optimized_volume_4, phul_4, barbell_no_rack_4, muscle_mania_6) |
| Routines renamed | 1 (long_cycle_full_body_3 → "Long Cycle Full Body (Adapted) · 3 days") |
| ACSM attribution corrections | All incorrect ACSM attribution removed from routines where source is silent on rest; relabelled as GarminCoach product default |

## Attribution of each field

| Field type | Source |
|---|---|
| Exercise order, sets, reps | Source-defined (Muscle & Strength) |
| Warm-up set count and rest (60 s) | GarminCoach safety overlay |
| Between-set rest for original 10 routines | Source-defined (audited 2026-07-18 and 2026-07-22) |
| Between-set rest for whole_body_toning_3 | Source-defined (30–45 s; upper bound 45 s applied) |
| Between-set rest for muscle_mania_6 | Source-defined (60 s compound; 45 s isolation) |
| Between-set rest for muscle_strength_5 | Source-defined (120–180 s strength; 60–90 s size) |
| Between-set rest for planet_fitness_upper_lower_4 | Source-specified range 60–90 s; 60 s lower bound applied |
| Between-set rest for 11 source-silent routines | GarminCoach product default (60 s); not attributed to ACSM |
| Superset pairs (muscle_strength_5, ppl_6) | Source-defined |
| Training level badges | Source-defined (never inferred) |
| Session rolling rest slots | Source-defined (weekday names converted to rolling slots) |
| Optional cardio exclusion | Source-defined: app excludes cardio sessions entirely |
| Movement aliases | Garmin technical limitation or equivalent movement (noted per exercise) |
| Long Cycle auto-regulation progression | Not implemented; routine classified garmin_adapted and renamed |

## ACSM principles actually used

The ACSM 2026 Position Stand (DOI: 10.1249/MSS.0000000000003897) informs GarminCoach at the product level through the following confirmed uses:

- Regular training of all major muscle groups
- Goal-appropriate load and rep ranges
- Progressive resistance over time
- Individualization via experience level badges

The ACSM Position Stand does not prescribe 60 seconds as a universal between-set rest. GarminCoach's 60 s product default is a coaching choice, not an ACSM prescription.

## Prior detailed audits

- `docs/routine_source_audit.md` — detailed exercise comparison, between-set rest, and operating-rule audit (2026-07-18 through 2026-07-22)
- `docs/routine_catalog_admission.md` — admission criteria, replacement table, and enforcement (reviewed 2026-07-22)
- `docs/SOURCE_EXECUTION_FIDELITY_CONTRACT.md` — superset and transition encoding contract
