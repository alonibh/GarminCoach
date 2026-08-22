# GarminCoach Product Milestones & Strategic Roadmap

This document serves as the canonical product backlog and milestone roadmap for GarminCoach. It preserves product design, architectural blueprints, value vs. effort evaluations, and phased execution plans for future development.

**Rule for Agents & Contributors**: Review this file before starting any feature design or major architectural refactoring.

---

## 🗺️ Milestone Overview

| Milestone | Focus Area | Primary Target | Status |
| :--- | :--- | :--- | :--- |
| **Phases 1–6** | Garmin Sync, Deterministic Policy, Safety & Guarded Restore | Garmin Athletes | **Complete** (See [`docs/PHASE_STATUS.md`](docs/PHASE_STATUS.md)) |
| **Milestone 7** | **Non-Garmin & Manual Input Foundation (PWA Gym Mode & Daily Check-in)** | All Athletes | 🎯 **Active Next Focus** |
| **Milestone 8** | **Web "Ask Coach" AI Hub & Personal Records (PR) Radar** | All Athletes | 📋 Planned |
| **Milestone 9** | **Smart Exercise Substitutions & Equipment Profiles** | Strength Athletes | 📋 Planned |
| **Milestone 10** | **Hybrid & Running Training Engine (5K, 10K, Half-Marathon)** | Runners & Hybrid | 📋 Planned |
| **Milestone 11** | **Multi-Ecosystem Ingestion (Strava, Apple Health, Whoop)** | Universal | 📋 Planned |
| **Milestone 12** | **Multi-Channel Outbox (PWA Web Push & Messaging Channels)** | Mobile Users | 📋 Planned |

---

## 🎯 Milestone 7: Non-Garmin & Manual Input Foundation (Active)

**Objective**: Make GarminCoach a fully self-contained, top-tier training platform that operates seamlessly **with or without a Garmin watch**. Enable real-time in-gym set logging, rest timing, and subjective readiness check-ins that integrate directly into the deterministic decision engine.

### 7A: Subjective Daily Check-in (Wearable-Free Morning Briefing)
* **Goal**: Provide morning workout recommendations and recovery adjustments for athletes without overnight biometric tracking (or on days when the watch was not worn).
* **User Experience**:
  * Clean morning prompt via Web Dashboard banner / Telegram bot:
    1. **Sleep Duration & Perceived Quality** (1–5 scale / hours).
    2. **Muscle Soreness & Recovery** (Fresh / Normal / Sore / Exhausted).
    3. **Subjective Energy & Stress** (High / Normal / Low).
  * Fast 10-second completion.
* **Deterministic Engine Integration**:
  * Translates subjective inputs into standard recovery signal bands.
  * When Garmin biometrics exist, objective data takes precedence; when missing, subjective check-in resolves the morning decision without requiring fallback timeouts.
* **Effort**: Low–Medium | **Value**: Very High.

---

### 7B: Live In-Gym Workout Mode (Mobile PWA & Web Live Mode)
* **Goal**: Enable athletes to execute, time, and record their workouts live in the gym without relying on smartwatch inputs.
* **User Experience (`/workout/live` or `/session/{id}/live`)**:
  * **Touch-Optimized UI**: High-contrast, large-button layout designed for gym usage.
  * **Real-Time Set Tracking**: Pre-filled with target reps and suggested progression weights from the active program. Tap `[✓ Done]` to log, or quickly adjust weight/reps with `[+]` / `[-]` steppers.
  * **Integrated Rest Timer**: Auto-starts countdown on set completion (e.g., 45s / 90s based on program rest rules) with visual countdown progress and optional audio/vibration cue.
  * **Warmup & Work Set Toggles**: Mark warmup sets vs. working sets to preserve progression evidence integrity.
  * **Session Summary & Completion**: On finishing, records an authoritative `Activity` + `ExerciseSet` rows, updates the `ProgramCursor`, and flags qualifying sets for the strength progression engine.
* **Effort**: Medium | **Value**: Critical / Maximum.

---

### 7C: Manual Activity Ingestion & File Dropzone
* **Goal**: Backfill or manually log past workouts and cardio sessions.
* **Features**:
  * **Manual Activity Form**: Log past strength sessions, runs, swims, or walks (date, duration, RPE, estimated calories, distance).
  * **Direct `.FIT` / `.GPX` File Parser**: Upload standard workout files directly on the web interface without Garmin Connect sync.
* **Effort**: Low–Medium | **Value**: High.

---

## 📋 Milestone 8: Web AI Coaching & Personal Records (PR) Radar

### 8A: Embedded Web "Ask Coach" Chat Drawer
* **Goal**: Bring Gemini-powered *Ask Coach* from Telegram directly into the web dashboard.
* **Features**:
  * Slide-over interactive chat drawer on the web dashboard.
  * Context-aware: Allows questioning 28-day trends, volume balance, or program rules directly on the web.
  * Uses the existing zero-storage, privacy-safe `advisory_snapshot` read model.
* **Effort**: Low | **Value**: Very High.

### 8B: Personal Records (PR) & Estimated 1RM Milestones
* **Goal**: Celebrate strength progress and visualize all-time achievements.
* **Features**:
  * Automatic e1RM calculation (Brzycki / Epley formulas) per exercise set.
  * PR badges on activity pages (Heaviest Weight, Max Reps, Max Volume).
  * "Trophy Case" & Strength Radar on the Progression page.
* **Effort**: Low–Medium | **Value**: High.

### 8C: Body Weight & Composition Trend Overlay
* **Goal**: Correlate body mass fluctuations with strength progression and caloric load.
* **Features**:
  * 7-day rolling weight average chart overlaid against weekly training load.
  * Manual weight entry or smart scale integration.
* **Effort**: Low | **Value**: High.

---

## 📋 Milestone 9: Smart Exercise Substitutions & Equipment Profiles

### 9A: 1-to-1 Biomechanical Exercise Swapper
* **Goal**: Dynamically replace unavailable or uncomfortable exercises without breaking program progression.
* **Features**:
  * Clustered movement patterns (`horizontal_push`, `vertical_pull`, `knee_flexion`, `hip_hinge`, etc.).
  * In-gym or in-plan quick substitution (e.g., Barbell Bench Press ↔ Dumbbell Bench Press ↔ Chest Press Machine).
  * Intelligent weight scaling factor between barbell, dumbbell, and machine variants.
* **Effort**: Medium | **Value**: High.

### 9B: Equipment Availability Profiles
* **Goal**: Tailor programs to Home Gym, Dumbbell-Only, Commercial Gym, or Calisthenics setups.
* **Effort**: Low–Medium | **Value**: Medium–High.

---

## 📋 Milestone 10: Hybrid Athlete & Structured Running Engine

### 10A: Curated Running & Cardio Programs
* **Goal**: Expand GarminCoach to serve the runner and hybrid athlete community.
* **Features**:
  * 5K, 10K, Half-Marathon, and 80/20 Polarized Zone 2 base-building programs.
  * Workout compilation with pace targets, HR zone boundaries, and interval structures.
  * Conflict prevention: Warns against high-fatigue leg days before scheduled hard running intervals.
* **Effort**: High | **Value**: Very High.

---

## 📋 Milestone 11: Multi-Ecosystem Ingestion (Strava, Apple Health, Whoop)

### 11A: Strava OAuth Ingestion Bridge
* **Goal**: Ingest non-Garmin cardio and run data automatically via universal Strava Webhooks.
* **Effort**: Medium | **Value**: High.

### 11B: Apple HealthKit / Health Connect Import
* **Goal**: Ingest Apple Watch sleep, resting HR, and workouts via standardized JSON payloads.
* **Effort**: Medium | **Value**: Very High.

---

## 📋 Milestone 12: Modern Multi-Channel Outbox & Push Notifications

### 12A: PWA Web Push Notifications
* **Goal**: Deliver morning briefings and 1-hour pre-workout reminders on iOS, Android, and Desktop without requiring Telegram.
* **Effort**: Medium | **Value**: Medium–High.

### 12B: WhatsApp / Custom Messaging Bridge
* **Goal**: Optional WhatsApp/Twilio integration for athletes who prefer WhatsApp over Telegram.
* **Effort**: Medium | **Value**: Medium.
