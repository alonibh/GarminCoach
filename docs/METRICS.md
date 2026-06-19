# GarminCoach — Metrics Reference

This is the source of truth for every computed metric: the exact formula as
implemented, every constant, the citation(s), and whether it is **Validated**
(a published formula) or a **Heuristic** (a defensible choice where no single
validated formula exists). The unit tests in `tests/test_engine.py` assert the
numbers below — change them together.

> Scope note: this is a personal single-user tool, not a medical device. The
> readiness and ACWR labels are guidance, not diagnoses.

---

## 1. Per-activity training load (TRIMP)

**Where:** `metrics/engine.py` — `compute_training_load`, `banister_trimp`,
`edwards_trimp`, `estimate_hr_max`.

Tiered, most-accurate-first. We never invent a load: if no method applies, the
load is `None`.

### Tier 1 — Banister TRIMP — **Validated**
```
HRR   = (HRavg − HRrest) / (HRmax − HRrest)        # clamped to [0, 1]
TRIMP = duration_min × HRR × A × e^(B × HRR)
```
- `A, B` gender constants: male `0.64 / 1.92`, female `0.86 / 1.92`→`1.67`.
  When gender is unknown we use the **female** constants because they produce
  the higher (conservative, no-underestimate) load.
- `HRmax` falls back to `208 − 0.7 × age` (Tanaka) when not measured.
- `HRrest` comes from `DailyHealth.resting_hr` for the activity's day.

### Tier 2 — Edwards summated-HR-zone TRIMP — **Validated** (approximate on Garmin zones)
```
TRIMP = Σ (minutes_in_zone_i × w_i),  w = [1, 2, 3, 4, 5]  for zones 1–5
```
Edwards defines zones as 50–60 / 60–70 / 70–80 / 80–90 / 90–100 % HRmax.
**Caveat:** Garmin's native zones are threshold-based, so applying these
weights to Garmin zone times is approximate.

### Scale caveat — enforced, not just advised
Banister and Edwards differ ~1.5–2.2× in magnitude, so mixing them within one
ACWR series makes the ratio spike or drop purely from the formula switch rather
than from any real change in load. To prevent this, `recompute_all` calls
`choose_load_method` **once per recompute** to pin a single method for the whole
activity set: Banister when it can score a majority of activities (HRmax known
and most activities have avg HR + same-day resting HR), otherwise Edwards for the
entire set. `compute_training_load(..., method=...)` then never crosses scales —
when the pinned method's inputs are missing for an activity it returns `None`
rather than falling back to the other formula. (The `method=None` legacy auto
path remains only for scoring a single isolated activity, where intra-series
scale consistency is irrelevant.)

**Citations:** Banister EW (1991), *Physiological Testing of the High-Performance
Athlete*, Human Kinetics, pp.403–424 · Morton, Fitz-Clarke & Banister, *J Appl
Physiol* 1990;69(3):1171–7 · Edwards S (1993), *The Heart Rate Monitor Book* ·
Tanaka, Monahan & Seals, *JACC* 2001;37(1):153–6 · Akubat & Abt, *J Sci Med
Sport* 2011;14(3):249–53.

---

## 2. ACWR (Acute:Chronic Workload Ratio) — **Validated structure / Heuristic thresholds**

**Where:** `metrics/engine.py` — `compute_daily_loads`, `acwr_label`.

EWMA per Williams et al. 2016:
```
λ_acute   = 2/(7+1)  = 0.25
λ_chronic = 2/(28+1) ≈ 0.069
EWMA_today = Load_today × λ + (1 − λ) × EWMA_yesterday        # today is i=0
ACWR = EWMA_acute / EWMA_chronic                              # None if chronic = 0
```
Implemented as the equivalent weighted sum over a `3N`-day lookback, **starting
at i=0 so today's load is included** (the prior version started at i=1, leaving
every ratio one day stale).

**Labels (`acwr_label`) — thresholds are heuristic** (derived from
rolling-average team-sport studies, not validated for EWMA on individuals):

| ACWR | Label |
|------|-------|
| < 0.8 | underload |
| 0.8–1.3 | balanced (sweet spot) |
| 1.3–1.5 | elevated |
| > 1.5 | spike ⚠ |

**Citations:** Williams, West, Cross & Stokes, *BJSM* 2016;51(3):209–10 · Gabbett,
*BJSM* 2016;50(5):273–80 · Hulin et al., *BJSM* 2016;50(4):231–6 · Esmaeili et
al., *Front Physiol* 2018;9:1280 · Impellizzeri et al., *J Athl Train*
2020;55(9):893–901 (critique — why thresholds are guidance only).

---

## 3. Readiness (0–100) — **Heuristic composite of validated components**

**Where:** `metrics/engine.py` — `compute_readiness`, `_score_hrv`,
`_score_rhr`, `_score_sleep`, `_baselines`.

Each component is normalized against the user's **own 7-day rolling baseline**
(mean + SD), then blended. Missing components are skipped and remaining weights
renormalized.

```
z              = (today − mean_7d) / SD_7d
HRV_score      = 50 + 50·tanh(z_HRV / 2)
RHR_score      = 50 − 50·tanh(z_RHR / 2)            # inverted: high RHR is worse
dur_score      = clamp(sleep_hours / 8 × 100, 0, 100)
eff_score      = clamp((sleep_eff% − 50) / 40 × 100, 0, 100)   # if available
Sleep_score    = 0.6·dur_score + 0.4·eff_score      # else dur_score alone
Readiness      = 0.50·HRV + 0.25·RHR + 0.25·Sleep
```
- 7-day baseline (Plews et al.) tracks **acute** readiness, not long-term drift.
- **Body Battery is intentionally excluded** from the composite — it's a
  proprietary Garmin score that already embeds HRV, so including it
  double-counts. Display it separately instead.
- **Heuristic parts (labelled in code):** the `0.50/0.25/0.25` weights, the
  `tanh(z/2)` sensitivity, and the `0.6/0.4` sleep split. No peer-reviewed RCT
  validates a specific weight vector for a consumer-wearable composite.

**Citations:** Plews et al., *Sports Med* 2013;43(9):773–81 & *Eur J Appl
Physiol* 2012;112(11):3729–41 · Buchheit, *Front Physiol* 2014;5:73 · Coyne et
al., *J Sports Sci Med* 2021;20:482–91 · Costa et al., *Front Physiol* 2021 ·
Watson et al., *Sleep* 2015;38(6):843–4.

---

## 4. Sleep debt (hours) — **Validated structure / Heuristic window**

**Where:** `metrics/engine.py` — `compute_sleep_debt`.

```
sleep_debt = min( Σ max(0, T − hours_i) over the last N nights, CAP )
T = 7.0 h     # AASM/SRS adult minimum
N = 7 nights  # heuristic window
CAP = 14 h    # = 7 nights × 2 h/night max plausible shortfall (heuristic)
```
- **Linear, no decay** — Van Dongen et al. 2003 found near-linear deficit
  accumulation; there is no published inter-day forgetting factor (the old
  `weight *= 0.8` was invented).
- Nights with no data are **excluded**, not imputed as 0 h (which would add a
  spurious full-target deficit).
- If the user sets a personal sleep goal, it replaces `T`.

**Citations:** Watson et al., *Sleep* 2015;38(6):843–4 · Van Dongen et al.,
*Sleep* 2003;26(2):117–26.

---

## 5. VO₂max fitness category — **Validated lookup table**

**Where:** `app.py` — `COOPER_VO2_NORMS`, `_cooper_norms`, `_vo2_max_details`.

Category (Poor / Fair / Good / Excellent / Superior) from the Cooper Institute
normative table — the 40th/60th/80th/95th percentile floors by sex and 10-year
age band (20–29 … 70–79). Ages <20 use 20–29; >79 use 70–79.

```
Poor      val < Fair_floor
Fair      Fair_floor      ≤ val < Good_floor
Good      Good_floor      ≤ val < Excellent_floor
Excellent Excellent_floor ≤ val < Superior_floor
Superior  val ≥ Superior_floor
```
**On missing age/sex we show the raw value with no category** — we do not
fabricate a default (the previous code mis-bucketed everyone as a 28-year-old
male, and its boundary values were also 4–6+ units below the real table).

**Citations:** The Cooper Institute, *Physical Fitness Assessments and Norms for
Adults and Law Enforcement* (2013), reprinted in the Garmin Forerunner 935
owner's manual · ACSM, *Guidelines for Exercise Testing and Prescription*, 11th
ed. (2021), Table 4.7.

---

## 6. Strength — volume load + estimated 1RM

**Where:** `app.py` — `workout_detail`, `_epley_1rm`, `_session_e1rm`.

### Volume load (tonnage) — **Validated**
```
VL = Σ (reps_i × weight_i)   over all working sets
```

### Estimated 1RM (Epley) — **Validated**
```
e1RM = weight × (1 + reps / 30)      # reps > 1
e1RM = weight                        # reps ≤ 1 (the set is itself a 1RM)
```
- Only computed for `reps ≤ 12` (all e1RM equations degrade above that).
- Bodyweight sets (weight = 0) are skipped.
- Session e1RM for an exercise = the max e1RM across its sets.

### Progression — **Heuristic window**
Compared against the **best e1RM over the last 5 sessions** of the same exercise
(a rolling baseline is far less noisy than a single-prior-session delta and is
robust to rep-scheme changes). The 5-session window is a heuristic.

**Citations:** Epley B (1985), *Boyd Epley Workout* · Schoenfeld et al., *Sports*
2021;9(2):32 (volume-load definition) · Wood et al., *Meas Phys Educ Exerc Sci*
2002;6(2):67–94 (Epley accuracy in the 2–10 rep range).
