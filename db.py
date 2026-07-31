"""SQLite schema + session helpers (SQLAlchemy 2.0 style).

Two kinds of tables:
  - Raw Garmin cache (re-syncable): activities, exercise_sets, sleep, daily_health.
  - Derived (never destroyed by a re-sync): daily_metrics, goals, coach_messages.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime
from typing import Iterator, Optional

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
    text,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    mapped_column,
    relationship,
    sessionmaker,
)

import config


class Base(DeclarativeBase):
    pass


class Activity(Base):
    __tablename__ = "activities"

    # Garmin's activityId is the natural primary key (idempotent upserts).
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=False)
    activity_type: Mapped[str] = mapped_column(String(64), default="")
    start_time: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    duration_s: Mapped[Optional[float]] = mapped_column(Float)
    distance_m: Mapped[Optional[float]] = mapped_column(Float)
    calories: Mapped[Optional[float]] = mapped_column(Float)
    avg_hr: Mapped[Optional[float]] = mapped_column(Float)
    max_hr: Mapped[Optional[float]] = mapped_column(Float)
    # Filled by the metrics engine (Phase 2); nullable until then.
    training_load: Mapped[Optional[float]] = mapped_column(Float)
    name: Mapped[Optional[str]] = mapped_column(String(255))
    # Garmin workout template id when the recorded activity exposes it. This is
    # the only exact provenance link used to complete a planned program session.
    source_workout_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    provenance_checked: Mapped[bool] = mapped_column(Boolean, default=False)

    # Cardio / outdoor fields (soccer, running, cycling…). Null for strength.
    moving_duration_s: Mapped[Optional[float]] = mapped_column(Float)
    avg_speed_mps: Mapped[Optional[float]] = mapped_column(Float)
    max_speed_mps: Mapped[Optional[float]] = mapped_column(Float)
    avg_cadence: Mapped[Optional[float]] = mapped_column(Float)
    avg_stride_cm: Mapped[Optional[float]] = mapped_column(Float)
    elevation_gain_m: Mapped[Optional[float]] = mapped_column(Float)
    elevation_loss_m: Mapped[Optional[float]] = mapped_column(Float)
    lap_count: Mapped[Optional[int]] = mapped_column(Integer)
    steps: Mapped[Optional[int]] = mapped_column(Integer)
    moderate_intensity_min: Mapped[Optional[int]] = mapped_column(Integer)
    vigorous_intensity_min: Mapped[Optional[int]] = mapped_column(Integer)
    training_effect_label: Mapped[Optional[str]] = mapped_column(String(32))
    aerobic_te_msg: Mapped[Optional[str]] = mapped_column(String(48))
    anaerobic_te_msg: Mapped[Optional[str]] = mapped_column(String(48))
    
    # RPE (Perceived Exertion) and Feel
    rpe: Mapped[Optional[int]] = mapped_column(Integer)
    
    # HR Zones (JSON string of 5 floats for time in zones 1-5, used for Edwards TRIMP)
    hr_zone_seconds: Mapped[Optional[str]] = mapped_column(Text)
    feel: Mapped[Optional[int]] = mapped_column(Integer)

    sets: Mapped[list["ExerciseSet"]] = relationship(
        back_populates="activity", cascade="all, delete-orphan"
    )


class ExerciseSet(Base):
    __tablename__ = "exercise_sets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), index=True
    )
    set_index: Mapped[int] = mapped_column(Integer)
    set_type: Mapped[str] = mapped_column(String(16), default="")  # ACTIVE | REST
    exercise_category: Mapped[Optional[str]] = mapped_column(String(64))
    exercise_name: Mapped[Optional[str]] = mapped_column(String(96))
    reps: Mapped[Optional[int]] = mapped_column(Integer)
    weight_kg: Mapped[Optional[float]] = mapped_column(Float)
    duration_s: Mapped[Optional[float]] = mapped_column(Float)
    # If the user corrected this set in the UI, protect it from re-sync overwrite.
    edited: Mapped[bool] = mapped_column(Boolean, default=False)

    activity: Mapped["Activity"] = relationship(back_populates="sets")

    __table_args__ = (
        Index("ix_exercise_sets_name_activity", "exercise_name", "activity_id"),
    )


class Workout(Base):
    """Pre-defined user workouts from Garmin Connect."""
    __tablename__ = "workouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    workout_id: Mapped[int] = mapped_column(Integer, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(255))
    sport_type: Mapped[str] = mapped_column(String(32))
    steps_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class Sleep(Base):
    __tablename__ = "sleep"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    sleep_start_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    sleep_end_time: Mapped[Optional[datetime]] = mapped_column(DateTime)
    total_s: Mapped[Optional[float]] = mapped_column(Float)
    deep_s: Mapped[Optional[float]] = mapped_column(Float)
    light_s: Mapped[Optional[float]] = mapped_column(Float)
    rem_s: Mapped[Optional[float]] = mapped_column(Float)
    awake_s: Mapped[Optional[float]] = mapped_column(Float)
    score: Mapped[Optional[float]] = mapped_column(Float)
    respiration_avg: Mapped[Optional[float]] = mapped_column(Float)
    sleep_stress_avg: Mapped[Optional[float]] = mapped_column(Float)


class DailyHealth(Base):
    __tablename__ = "daily_health"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    resting_hr: Mapped[Optional[float]] = mapped_column(Float)
    hrv_overnight: Mapped[Optional[float]] = mapped_column(Float)
    hrv_weekly_avg: Mapped[Optional[float]] = mapped_column(Float)
    hrv_status: Mapped[Optional[str]] = mapped_column(String(64))
    hrv_feedback_phrase: Mapped[Optional[str]] = mapped_column(String(128))
    hrv_7d_coverage_days: Mapped[Optional[int]] = mapped_column(Integer)
    hrv_baseline_low: Mapped[Optional[float]] = mapped_column(Float)
    hrv_baseline_high: Mapped[Optional[float]] = mapped_column(Float)
    body_battery_high: Mapped[Optional[float]] = mapped_column(Float)
    body_battery_low: Mapped[Optional[float]] = mapped_column(Float)
    body_battery_current: Mapped[Optional[float]] = mapped_column(Float)
    body_battery_charged: Mapped[Optional[int]] = mapped_column(Integer)
    body_battery_drained: Mapped[Optional[int]] = mapped_column(Integer)
    stress_avg: Mapped[Optional[float]] = mapped_column(Float)
    steps: Mapped[Optional[int]] = mapped_column(Integer)
    step_goal: Mapped[Optional[int]] = mapped_column(Integer)
    total_kcal: Mapped[Optional[int]] = mapped_column(Integer)
    active_kcal: Mapped[Optional[int]] = mapped_column(Integer)
    bmr_kcal: Mapped[Optional[int]] = mapped_column(Integer)
    # Daily Garmin summary values.  These deliberately differ from the
    # per-activity ``Activity.*_intensity_min`` fields.
    daily_moderate_intensity_minutes: Mapped[Optional[float]] = mapped_column(Float)
    daily_vigorous_intensity_minutes: Mapped[Optional[float]] = mapped_column(Float)
    training_readiness: Mapped[Optional[int]] = mapped_column(Integer)
    recovery_time_source_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    recovery_time_minutes: Mapped[Optional[int]] = mapped_column(Integer)
    recovery_time_change_phrase: Mapped[Optional[str]] = mapped_column(String(64))
    recovery_time_observed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    training_status: Mapped[Optional[str]] = mapped_column(String(32))


class DailyMetrics(Base):
    """Computed by the metrics engine (Phase 2). Derived — never raw."""

    __tablename__ = "daily_metrics"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    readiness: Mapped[Optional[float]] = mapped_column(Float)
    acute_load: Mapped[Optional[float]] = mapped_column(Float)
    chronic_load: Mapped[Optional[float]] = mapped_column(Float)
    acwr: Mapped[Optional[float]] = mapped_column(Float)
    sleep_debt_h: Mapped[Optional[float]] = mapped_column(Float)


class Goal(Base):
    """Single active goal row (id=1). The only thing the watch can't provide."""

    __tablename__ = "goals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    goal: Mapped[str] = mapped_column(Text, default="")
    custom_input: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class AthleteProfile(Base):
    """Structured single-user coaching profile collected during onboarding."""

    __tablename__ = "athlete_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    training_type: Mapped[str] = mapped_column(String(32), default="")
    experience_level: Mapped[str] = mapped_column(String(32), default="")
    primary_goal: Mapped[str] = mapped_column(String(64), default="")
    goal_detail: Mapped[str] = mapped_column(Text, default="")
    preferred_activities: Mapped[str] = mapped_column(Text, default="")  # JSON list
    activity_preferences: Mapped[str] = mapped_column(Text, default="")  # JSON roles/frequencies
    equipment_access: Mapped[str] = mapped_column(Text, default="")  # JSON list
    availability: Mapped[str] = mapped_column(Text, default="")
    timing_preferences: Mapped[str] = mapped_column(Text, default="")  # JSON timing constraints
    injuries_limitations: Mapped[str] = mapped_column(Text, default="")
    sport_commitments: Mapped[str] = mapped_column(Text, default="")
    scheduling_preferences: Mapped[str] = mapped_column(Text, default="")
    approval_mode: Mapped[str] = mapped_column(String(32), default="manual")
    onboarding_complete: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class TrainingProgram(Base):
    """A user-confirmed plan, routine, or schedule-only setup."""

    __tablename__ = "training_programs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    mode: Mapped[str] = mapped_column(String(32), default="schedule_my_routine")
    source_type: Mapped[str] = mapped_column(String(32), default="user_defined")
    source_url: Mapped[str] = mapped_column(Text, default="")
    attribution: Mapped[str] = mapped_column(String(255), default="")
    goal_tags: Mapped[str] = mapped_column(Text, default="")  # JSON list
    experience_level: Mapped[str] = mapped_column(String(32), default="")
    days_per_week: Mapped[Optional[int]] = mapped_column(Integer)
    equipment: Mapped[str] = mapped_column(Text, default="")  # JSON list
    active: Mapped[bool] = mapped_column(Boolean, default=False)
    # draft -> active only after the athlete reviews and approves it.
    status: Mapped[str] = mapped_column(String(16), default="draft")
    rationale: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    sessions: Mapped[list["ProgramSession"]] = relationship(
        back_populates="program", cascade="all, delete-orphan"
    )


class ProgramSession(Base):
    """A repeatable session inside a user-confirmed program."""

    __tablename__ = "program_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_id: Mapped[int] = mapped_column(
        ForeignKey("training_programs.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    sport_type: Mapped[str] = mapped_column(String(64), default="")
    sequence_order: Mapped[int] = mapped_column(Integer, default=0)
    focus_tags: Mapped[str] = mapped_column(Text, default="")  # JSON list
    duration_min: Mapped[Optional[int]] = mapped_column(Integer)
    base_workout_id: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    notes: Mapped[str] = mapped_column(Text, default="")
    # coach_strength | activity_anchor | optional_recovery
    session_role: Mapped[str] = mapped_column(String(32), default="coach_strength")
    target_frequency: Mapped[int] = mapped_column(Integer, default=1)
    # If True, this session is a finisher/add-on to another session, not standalone.
    is_addon: Mapped[bool] = mapped_column(Boolean, default=False)
    # True only for a session explicitly added by the athlete in the program editor.
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)

    program: Mapped["TrainingProgram"] = relationship(back_populates="sessions")
    exercises: Mapped[list["SessionExercise"]] = relationship(
        back_populates="program_session", cascade="all, delete-orphan",
        order_by="SessionExercise.order_index"
    )


class SessionExercise(Base):
    """User-defined baseline exercise for a program session.
    
    The AI coach uses these as the starting point when suggesting a workout,
    instead of pulling from Garmin templates.
    """

    __tablename__ = "session_exercises"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_session_id: Mapped[int] = mapped_column(
        ForeignKey("program_sessions.id", ondelete="CASCADE"), index=True
    )
    exercise_name: Mapped[str] = mapped_column(String(128))  # Garmin exercise enum value
    exercise_key: Mapped[str] = mapped_column(String(128), default="")
    garmin_category: Mapped[Optional[str]] = mapped_column(String(64))
    garmin_name: Mapped[Optional[str]] = mapped_column(String(128))
    movement_pattern: Mapped[str] = mapped_column(String(32), default="other")
    is_generic: Mapped[bool] = mapped_column(Boolean, default=False)
    sets: Mapped[Optional[int]] = mapped_column(Integer)
    reps: Mapped[Optional[int]] = mapped_column(Integer)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    weight_kg: Mapped[Optional[float]] = mapped_column(Float)  # None = bodyweight
    rest_seconds: Mapped[int] = mapped_column(Integer, default=60)
    warmup_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    warmup_reps: Mapped[Optional[int]] = mapped_column(Integer)
    warmup_duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)
    warmup_weight_kg: Mapped[Optional[float]] = mapped_column(Float)
    order_index: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str] = mapped_column(Text, default="")

    program_session: Mapped["ProgramSession"] = relationship(back_populates="exercises")


class PlannedSession(Base):
    """A dated session in the rolling plan, optionally linked to Garmin."""

    __tablename__ = "planned_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    program_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("program_sessions.id", ondelete="SET NULL"), index=True
    )
    activity_type: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(255), default="Workout")
    target_date: Mapped[date] = mapped_column(Date, index=True)
    suggested_time: Mapped[str] = mapped_column(String(5), default="")
    duration_min: Mapped[int] = mapped_column(Integer, default=60)
    intensity: Mapped[str] = mapped_column(String(32), default="normal")
    status: Mapped[str] = mapped_column(String(32), default="planned")
    garmin_workout_id: Mapped[Optional[int]] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(32), default="coach")
    notes: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_activity_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), index=True
    )
    completion_match_method: Mapped[Optional[str]] = mapped_column(String(32))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class ProgramCursor(Base):
    """Durable rolling position in one curated program; never week-based."""

    __tablename__ = "program_cursors"

    program_id: Mapped[int] = mapped_column(
        ForeignKey("training_programs.id", ondelete="CASCADE"), primary_key=True
    )
    next_program_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("program_sessions.id", ondelete="SET NULL"), index=True
    )
    last_completed_program_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("program_sessions.id", ondelete="SET NULL")
    )
    last_completed_activity_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL")
    )
    last_completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    policy_version: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class ActivityProgramMatch(Base):
    """Auditable, deterministic link between a synced activity and program session."""

    __tablename__ = "activity_program_matches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[int] = mapped_column(
        ForeignKey("activities.id", ondelete="CASCADE"), unique=True, index=True
    )
    program_id: Mapped[int] = mapped_column(
        ForeignKey("training_programs.id", ondelete="CASCADE"), index=True
    )
    program_session_id: Mapped[int] = mapped_column(
        ForeignKey("program_sessions.id", ondelete="CASCADE"), index=True
    )
    match_method: Mapped[str] = mapped_column(String(32))
    policy_version: Mapped[str] = mapped_column(String(32))
    matched_at: Mapped[datetime] = mapped_column(DateTime)


# Phase 4B1 progression rows are deliberately independent of the mutable
# program/activity relationships.  Nullable SET NULL foreign keys retain audit
# history while the numeric snapshots retain the original ownership meaning.
class StrengthProgressionPolicy(Base):
    __tablename__ = "strength_progression_policies"

    policy_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    global_increment_grams: Mapped[int] = mapped_column(Integer)
    weight_quantum_grams: Mapped[int] = mapped_column(Integer)
    required_consecutive: Mapped[int] = mapped_column(Integer)
    evidence_window_days: Mapped[int] = mapped_column(Integer)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    activated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        Index(
            "uq_strength_progression_one_active_policy", "is_active", unique=True,
            sqlite_where=(is_active.is_(True)),
        ),
    )


class StrengthProgressionEvidence(Base):
    __tablename__ = "strength_progression_evidence"

    evidence_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    activity_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("activities.id", ondelete="SET NULL"), index=True
    )
    activity_program_match_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("activity_program_matches.id", ondelete="SET NULL"), index=True
    )
    program_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("training_programs.id", ondelete="SET NULL"), index=True
    )
    program_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("program_sessions.id", ondelete="SET NULL"), index=True
    )
    session_exercise_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("session_exercises.id", ondelete="SET NULL"), index=True
    )
    activity_id_snapshot: Mapped[Optional[int]] = mapped_column(Integer, index=True)
    activity_program_match_id_snapshot: Mapped[Optional[int]] = mapped_column(Integer)
    program_id_snapshot: Mapped[Optional[int]] = mapped_column(Integer)
    program_session_id_snapshot: Mapped[Optional[int]] = mapped_column(Integer)
    session_exercise_id_snapshot: Mapped[int] = mapped_column(Integer, index=True)
    policy_version: Mapped[str] = mapped_column(
        ForeignKey("strength_progression_policies.policy_version", ondelete="RESTRICT"), index=True
    )
    prescription_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    source_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    appearance_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    classification: Mapped[str] = mapped_column(String(32), index=True)
    current_weight_grams: Mapped[Optional[int]] = mapped_column(Integer)
    candidate_weight_grams: Mapped[Optional[int]] = mapped_column(Integer)
    prescribed_sets: Mapped[Optional[int]] = mapped_column(Integer)
    target_reps: Mapped[Optional[int]] = mapped_column(Integer)
    decisive_sets_json: Mapped[str] = mapped_column(Text)
    reason_codes_json: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    supersedes_evidence_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("strength_progression_evidence.evidence_id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_strength_evidence_exercise_policy_prescription", "session_exercise_id_snapshot", "policy_version", "prescription_fingerprint"),
    )


class StrengthProgressionEvidenceHead(Base):
    __tablename__ = "strength_progression_evidence_heads"

    session_exercise_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    activity_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    current_evidence_id: Mapped[str] = mapped_column(
        ForeignKey("strength_progression_evidence.evidence_id", ondelete="RESTRICT"), unique=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StrengthProgressionEvidenceBoundary(Base):
    """An immutable rejection cutoff for one exact prescription."""
    __tablename__ = "strength_progression_evidence_boundaries"

    boundary_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_exercise_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("session_exercises.id", ondelete="SET NULL"), index=True
    )
    session_exercise_id_snapshot: Mapped[int] = mapped_column(Integer, index=True)
    policy_version: Mapped[str] = mapped_column(
        ForeignKey("strength_progression_policies.policy_version", ondelete="RESTRICT"), index=True
    )
    prescription_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    proposal_id: Mapped[str] = mapped_column(
        ForeignKey("strength_progression_proposals.proposal_id", ondelete="RESTRICT"), index=True
    )
    cause: Mapped[str] = mapped_column(String(32), default="proposal_rejected")
    cutoff_appearance_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    cutoff_evidence_id: Mapped[str] = mapped_column(
        ForeignKey("strength_progression_evidence.evidence_id", ondelete="RESTRICT")
    )
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)

    __table_args__ = (
        Index("ix_strength_boundary_exercise_policy_prescription", "session_exercise_id_snapshot", "policy_version", "prescription_fingerprint"),
        Index("ix_strength_boundary_cutoff", "cutoff_appearance_at", "cutoff_evidence_id"),
    )


class StrengthProgressionStreak(Base):
    __tablename__ = "strength_progression_streaks"

    session_exercise_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    policy_version: Mapped[str] = mapped_column(String(64), primary_key=True)
    prescription_fingerprint: Mapped[str] = mapped_column(String(64), primary_key=True)
    increase_count: Mapped[int] = mapped_column(Integer, default=0)
    decrease_count: Mapped[int] = mapped_column(Integer, default=0)
    last_classification: Mapped[str] = mapped_column(String(32), default="unscorable")
    last_relevant_appearance_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    decisive_evidence_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StrengthProgressionProposal(Base):
    __tablename__ = "strength_progression_proposals"

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    program_id: Mapped[Optional[int]] = mapped_column(ForeignKey("training_programs.id", ondelete="SET NULL"), index=True)
    program_session_id: Mapped[Optional[int]] = mapped_column(ForeignKey("program_sessions.id", ondelete="SET NULL"), index=True)
    session_exercise_id: Mapped[Optional[int]] = mapped_column(ForeignKey("session_exercises.id", ondelete="SET NULL"), index=True)
    program_id_snapshot: Mapped[Optional[int]] = mapped_column(Integer)
    program_session_id_snapshot: Mapped[Optional[int]] = mapped_column(Integer)
    session_exercise_id_snapshot: Mapped[int] = mapped_column(Integer, index=True)
    policy_version: Mapped[str] = mapped_column(String(64), index=True)
    prescription_fingerprint: Mapped[str] = mapped_column(String(64), index=True)
    direction: Mapped[str] = mapped_column(String(16))
    current_weight_grams: Mapped[int] = mapped_column(Integer)
    suggested_weight_grams: Mapped[int] = mapped_column(Integer)
    approved_weight_grams: Mapped[Optional[int]] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    decisive_evidence_one_id: Mapped[str] = mapped_column(ForeignKey("strength_progression_evidence.evidence_id", ondelete="RESTRICT"))
    decisive_evidence_two_id: Mapped[str] = mapped_column(ForeignKey("strength_progression_evidence.evidence_id", ondelete="RESTRICT"))
    reason_codes_json: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    current_pending_key: Mapped[Optional[str]] = mapped_column(String(196), unique=True, index=True)
    supersedes_proposal_id: Mapped[Optional[str]] = mapped_column(ForeignKey("strength_progression_proposals.proposal_id", ondelete="SET NULL"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    resolved_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class CoachMessage(Base):
    __tablename__ = "coach_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16))  # suggestion | user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    data_snapshot: Mapped[Optional[str]] = mapped_column(Text)  # JSON of facts used
    pending_action_json: Mapped[Optional[str]] = mapped_column(Text)  # the staged action payload

    @property
    def pending_action_payload(self) -> dict | None:
        if self.pending_action_json:
            import json
            try:
                return json.loads(self.pending_action_json)
            except Exception:
                pass
        return None


class SyncState(Base):
    """Bookkeeping so we only fetch new data each sync."""

    __tablename__ = "sync_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[Optional[str]] = mapped_column(Text)


class MetricSnapshot(Base):
    """Latest value + last-different value for summary metrics (fitness age,
    VO2 max). Computed during sync so the dashboard reads instantly without
    live Garmin calls. Keyed by metric name (e.g. 'fitness_age', 'vo2max')."""

    __tablename__ = "metric_snapshot"

    metric: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[Optional[float]] = mapped_column(Float)
    value_date: Mapped[Optional[str]] = mapped_column(String(10))   # ISO date of current value
    prev_value: Mapped[Optional[float]] = mapped_column(Float)       # last value that differed
    prev_date: Mapped[Optional[str]] = mapped_column(String(10))
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


class MetricCapability(Base):
    """Tri-state capability evidence, isolated by its durable source scope."""

    __tablename__ = "device_capabilities"

    metric: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope_kind: Mapped[str] = mapped_column(String(16), primary_key=True)
    scope_key: Mapped[str] = mapped_column(String(96), primary_key=True)
    support_state: Mapped[str] = mapped_column(String(16), default="unknown")
    evidence_source: Mapped[str] = mapped_column(String(64), default="unresolved")
    first_observed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_observed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    override_state: Mapped[Optional[str]] = mapped_column(String(16))
    device_model_key: Mapped[Optional[str]] = mapped_column(String(96))
    registry_version: Mapped[Optional[str]] = mapped_column(String(32))
    source_verified_on: Mapped[Optional[date]] = mapped_column(Date)
    last_probe_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_probe_outcome: Mapped[Optional[str]] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(DateTime)

    __table_args__ = (
        Index("ix_device_capabilities_scope_metric", "scope_kind", "scope_key", "metric"),
    )


# Compatibility import for integrations that imported the old model name.
# There is one table and one source of truth.
DeviceCapability = MetricCapability


class ObservationFreshness(Base):
    """Per-signal fetch result for one local decision date."""

    __tablename__ = "observation_freshness"

    signal: Mapped[str] = mapped_column(String(64), primary_key=True)
    observed_for: Mapped[date] = mapped_column(Date, primary_key=True)
    state: Mapped[str] = mapped_column(String(24))
    fetched_at: Mapped[datetime] = mapped_column(DateTime)
    source_endpoint: Mapped[str] = mapped_column(String(128))
    device_upload_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    error_code: Mapped[Optional[str]] = mapped_column(String(64))
    detail: Mapped[str] = mapped_column(Text, default="")


class MorningBriefState(Base):
    """Durable one-per-day state for priority sync and the 11:30 flow."""

    __tablename__ = "morning_brief_state"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default="waiting")
    wait_notice_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    deadline_prompt_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    answer_anyway: Mapped[bool] = mapped_column(Boolean, default=False)
    briefing_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    last_priority_fetch_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(DateTime)


class DecisionRecord(Base):
    """Immutable typed coaching result with its exact facts and rule versions."""

    __tablename__ = "decision_records"

    decision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    decision_type: Mapped[str] = mapped_column(String(32), index=True)
    active_program_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("training_programs.id", ondelete="SET NULL"), index=True
    )
    program_policy_version: Mapped[Optional[str]] = mapped_column(String(32))
    planned_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("planned_sessions.id", ondelete="SET NULL")
    )
    next_program_session_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("program_sessions.id", ondelete="SET NULL")
    )
    earliest_eligible_date: Mapped[Optional[date]] = mapped_column(Date)
    observations_json: Mapped[str] = mapped_column(Text, default="[]")
    missing_json: Mapped[str] = mapped_column(Text, default="[]")
    rule_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    reason_codes_json: Mapped[str] = mapped_column(Text, default="[]")
    permitted_actions_json: Mapped[str] = mapped_column(Text, default="[]")
    result_json: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    supersedes_decision_id: Mapped[Optional[str]] = mapped_column(String(36))


class PendingInteraction(Base):
    """Versioned button action that must be revalidated before application."""

    __tablename__ = "pending_interactions"

    interaction_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    decision_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("decision_records.decision_id", ondelete="SET NULL"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(48), index=True)
    target_type: Mapped[str] = mapped_column(String(32))
    target_id: Mapped[Optional[int]] = mapped_column(Integer)
    payload_json: Mapped[str] = mapped_column(Text)
    program_version: Mapped[str] = mapped_column(String(64), default="")
    sync_version: Mapped[str] = mapped_column(String(128), default="")
    calendar_version: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    applied_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    failure_reason: Mapped[str] = mapped_column(Text, default="")


class NotificationOutbox(Base):
    """Durable, idempotent notification job processed after app restarts."""

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_type: Mapped[str] = mapped_column(String(48), index=True)
    due_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    quiet_hour_policy: Mapped[str] = mapped_column(String(24), default="defer")
    payload_json: Mapped[str] = mapped_column(Text)
    decision_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("decision_records.decision_id", ondelete="SET NULL"), index=True
    )
    status: Mapped[str] = mapped_column(String(24), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


# The web request threads and the background sync thread both write to this
# SQLite file. Without a busy timeout an overlapping write fails immediately
# with "database is locked"; WAL mode lets readers and a writer coexist.
engine = create_engine(
    config.DB_URL,
    future=True,
    # 30s busy timeout: wait for a competing writer instead of erroring out.
    connect_args={"timeout": 30},
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_conn, _record):
    """Enable WAL + sane durability on every new SQLite connection."""
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")      # concurrent reads during a write
    cur.execute("PRAGMA synchronous=NORMAL")    # safe with WAL, much faster
    cur.execute("PRAGMA foreign_keys=ON")       # honor FK cascades (exercise_sets)
    cur.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def dispose_engine() -> None:
    engine.dispose()


def init_db(target_engine: Engine | None = None) -> None:
    eng = target_engine or engine
    Base.metadata.create_all(eng)
    _migrate_add_columns(eng)
    
    with eng.begin() as conn:
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_exercise_sets_name_activity ON exercise_sets (exercise_name, activity_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_activities_source_workout_id ON activities (source_workout_id)"))
        conn.execute(text("CREATE INDEX IF NOT EXISTS ix_planned_sessions_completed_activity_id ON planned_sessions (completed_activity_id)"))


# SQLite can't add columns via create_all on an existing table, so add any
# missing columns explicitly. Idempotent: skips columns that already exist.
_ACTIVITY_ADD_COLUMNS = {
    "moving_duration_s": "FLOAT",
    "avg_speed_mps": "FLOAT",
    "max_speed_mps": "FLOAT",
    "avg_cadence": "FLOAT",
    "avg_stride_cm": "FLOAT",
    "elevation_gain_m": "FLOAT",
    "elevation_loss_m": "FLOAT",
    "lap_count": "INTEGER",
    "steps": "INTEGER",
    "moderate_intensity_min": "INTEGER",
    "vigorous_intensity_min": "INTEGER",
    "training_effect_label": "VARCHAR(32)",
    "aerobic_te_msg": "VARCHAR(48)",
    "anaerobic_te_msg": "VARCHAR(48)",
    "rpe": "INTEGER",
    "feel": "INTEGER",
    "hr_zone_seconds": "TEXT",
    "source_workout_id": "INTEGER",
    "provenance_checked": "INTEGER NOT NULL DEFAULT 0",
}


_PLANNED_SESSION_ADD_COLUMNS = {
    "completed_activity_id": "INTEGER",
    "completion_match_method": "VARCHAR(32)",
    "completed_at": "DATETIME",
}


_DAILY_HEALTH_ADD_COLUMNS = {
    "hrv_weekly_avg": "FLOAT",
    "hrv_status": "VARCHAR(64)",
    "hrv_feedback_phrase": "VARCHAR(128)",
    "hrv_7d_coverage_days": "INTEGER",
    "hrv_baseline_low": "FLOAT",
    "hrv_baseline_high": "FLOAT",
    "step_goal": "INTEGER",
    "body_battery_current": "FLOAT",
    "body_battery_charged": "INTEGER",
    "body_battery_drained": "INTEGER",
    "recovery_time_source_minutes": "INTEGER",
    "recovery_time_minutes": "INTEGER",
    "recovery_time_change_phrase": "VARCHAR(64)",
    "recovery_time_observed_at": "DATETIME",
    "daily_moderate_intensity_minutes": "FLOAT",
    "daily_vigorous_intensity_minutes": "FLOAT",
}


_SLEEP_ADD_COLUMNS = {
    "sleep_start_time": "DATETIME",
    "sleep_end_time": "DATETIME",
}


_DEVICE_CAPABILITY_ADD_COLUMNS = {
    "device_model_key": "VARCHAR(96)",
    "registry_version": "VARCHAR(32)",
    "source_verified_on": "DATE",
    "last_probe_at": "DATETIME",
    "last_probe_outcome": "VARCHAR(32)",
}

_CAPABILITY_SCOPE_MIGRATION_KEY = "metric_capabilities_scoped_identity_2026_07_30_v1"
_BODY_COMPOSITION_CONTRACT_GATE_MIGRATION_KEY = "body_composition_capability_contract_gate_2026_07_30_v1"
_STRENGTH_PROGRESSION_FOUNDATION_MIGRATION_KEY = "strength_progression_foundation_2026_07_30_v1"
_STRENGTH_PROGRESSION_REVIEW_ACTIONS_MIGRATION_KEY = "strength_progression_review_actions_2026_07_31_v1"


def _seed_strength_progression_policy(conn) -> None:
    """Seed and validate the one initial policy inside the migration transaction."""
    active = conn.execute(text(
        "SELECT policy_version FROM strength_progression_policies WHERE is_active = 1"
    )).all()
    if len(active) > 1:
        raise RuntimeError("strength progression requires exactly one active policy")
    if not active:
        conn.execute(text("""
            INSERT INTO strength_progression_policies (
                policy_version, global_increment_grams, weight_quantum_grams,
                required_consecutive, evidence_window_days, is_active, created_at,
                activated_at
            ) VALUES (
                'strength-progression-v1', 2500, 250, 2, 35, 1,
                CURRENT_TIMESTAMP, CURRENT_TIMESTAMP
            )
        """))
    active_count = conn.execute(text(
        "SELECT COUNT(*) FROM strength_progression_policies WHERE is_active = 1"
    )).scalar_one()
    if active_count != 1:
        raise RuntimeError("strength progression policy seed validation failed")


def _validate_strength_progression_review_actions(conn) -> None:
    """Validate the new immutable cutoff schema before recording its marker."""
    columns = {row[1] for row in conn.execute(text("PRAGMA table_info('strength_progression_evidence_boundaries')"))}
    expected = {
        "boundary_id", "session_exercise_id", "session_exercise_id_snapshot", "policy_version",
        "prescription_fingerprint", "proposal_id", "cause", "cutoff_appearance_at",
        "cutoff_evidence_id", "idempotency_key", "created_at",
    }
    if not expected.issubset(columns):
        raise RuntimeError("strength progression review-actions schema validation failed")
    indexes = {row[1] for row in conn.execute(text("PRAGMA index_list('strength_progression_evidence_boundaries')"))}
    if "ix_strength_boundary_exercise_policy_prescription" not in indexes:
        raise RuntimeError("strength progression review-actions index validation failed")


def _migrate_capability_scopes(conn) -> None:
    """Rebuild the legacy metric-primary-key table as a scoped capability table."""
    from sqlalchemy import inspect, text
    from metrics.capability_registry import legacy_capability_ref

    inspector = inspect(conn)
    if "device_capabilities" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("device_capabilities")}
    primary_key = inspector.get_pk_constraint("device_capabilities").get("constrained_columns") or []
    if {"scope_kind", "scope_key"}.issubset(columns) and set(primary_key) == {
        "metric", "scope_kind", "scope_key",
    }:
        return

    current_model = conn.execute(text(
        "SELECT value FROM sync_state WHERE key = 'garmin_device_model_key'"
    )).scalar()
    rows = conn.execute(text("SELECT * FROM device_capabilities")).mappings().all()
    conn.execute(text("""
        CREATE TABLE device_capabilities_scoped_new (
            metric VARCHAR(64) NOT NULL,
            scope_kind VARCHAR(16) NOT NULL,
            scope_key VARCHAR(96) NOT NULL,
            support_state VARCHAR(16),
            evidence_source VARCHAR(64),
            first_observed_at DATETIME,
            last_observed_at DATETIME,
            override_state VARCHAR(16),
            device_model_key VARCHAR(96),
            registry_version VARCHAR(32),
            source_verified_on DATE,
            last_probe_at DATETIME,
            last_probe_outcome VARCHAR(32),
            updated_at DATETIME NOT NULL,
            PRIMARY KEY (metric, scope_kind, scope_key)
        )
    """))
    for row in rows:
        device_key = row.get("device_model_key") or current_model
        ref = legacy_capability_ref(row["metric"], device_key)
        conn.execute(text("""
            INSERT INTO device_capabilities_scoped_new (
                metric, scope_kind, scope_key, support_state, evidence_source,
                first_observed_at, last_observed_at, override_state, device_model_key,
                registry_version, source_verified_on, last_probe_at, last_probe_outcome,
                updated_at
            ) VALUES (
                :metric, :scope_kind, :scope_key, :support_state, :evidence_source,
                :first_observed_at, :last_observed_at, :override_state, :device_model_key,
                :registry_version, :source_verified_on, :last_probe_at, :last_probe_outcome,
                :updated_at
            )
        """), {
            **dict(row),
            "scope_kind": ref.scope_kind,
            "scope_key": ref.scope_key,
            "device_model_key": ref.scope_key if ref.scope_kind == "device" else row.get("device_model_key"),
        })
    conn.execute(text("DROP TABLE device_capabilities"))
    conn.execute(text("ALTER TABLE device_capabilities_scoped_new RENAME TO device_capabilities"))
    conn.execute(text(
        "CREATE INDEX ix_device_capabilities_scope_metric "
        "ON device_capabilities (scope_kind, scope_key, metric)"
    ))


def _normalize_body_composition_contract_gate(conn) -> None:
    """Remove any pre-contract Body Composition capability evidence."""
    from sqlalchemy import text

    conn.execute(text("""
        UPDATE device_capabilities
        SET support_state = 'unknown', override_state = NULL,
            first_observed_at = NULL, last_observed_at = NULL,
            last_probe_at = NULL, last_probe_outcome = NULL,
            evidence_source = 'contract_gate', source_verified_on = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE metric = 'body_composition'
    """))
    conn.execute(text("""
        INSERT INTO device_capabilities (
            metric, scope_kind, scope_key, support_state, evidence_source, updated_at
        )
        SELECT 'body_composition', 'scale', 'scale', 'unknown', 'contract_gate', CURRENT_TIMESTAMP
        WHERE NOT EXISTS (
            SELECT 1 FROM device_capabilities
            WHERE metric = 'body_composition' AND scope_kind = 'scale' AND scope_key = 'scale'
        )
    """))


_ATHLETE_PROFILE_ADD_COLUMNS = {
    "training_type": "VARCHAR(32)",
    "goal_detail": "TEXT NOT NULL DEFAULT ''",
    "activity_preferences": "TEXT NOT NULL DEFAULT ''",
    "timing_preferences": "TEXT NOT NULL DEFAULT ''",
}

_PROGRAM_SESSION_ADD_COLUMNS = {
    "is_addon": "INTEGER NOT NULL DEFAULT 0",
    "session_role": "VARCHAR(32) NOT NULL DEFAULT 'coach_strength'",
    "target_frequency": "INTEGER NOT NULL DEFAULT 1",
    "is_custom": "INTEGER NOT NULL DEFAULT 0",
}


_TRAINING_PROGRAM_ADD_COLUMNS = {
    "status": "VARCHAR(16) NOT NULL DEFAULT 'draft'",
    "rationale": "TEXT NOT NULL DEFAULT ''",
    "activated_at": "DATETIME",
}


_SESSION_EXERCISES_CREATE = """
    CREATE TABLE IF NOT EXISTS session_exercises (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        program_session_id INTEGER NOT NULL
            REFERENCES program_sessions(id) ON DELETE CASCADE,
        exercise_name VARCHAR(128) NOT NULL,
        exercise_key VARCHAR(128) NOT NULL DEFAULT '',
        garmin_category VARCHAR(64),
        garmin_name VARCHAR(128),
        movement_pattern VARCHAR(32) NOT NULL DEFAULT 'other',
        is_generic INTEGER NOT NULL DEFAULT 0,
        sets INTEGER,
        reps INTEGER,
        duration_seconds INTEGER,
        weight_kg FLOAT,
        rest_seconds INTEGER NOT NULL DEFAULT 60,
        warmup_enabled INTEGER NOT NULL DEFAULT 0,
        warmup_reps INTEGER,
        warmup_duration_seconds INTEGER,
        warmup_weight_kg FLOAT,
        order_index INTEGER NOT NULL DEFAULT 0,
        notes TEXT NOT NULL DEFAULT ''
    )
"""

_SESSION_EXERCISE_ADD_COLUMNS = {
    "exercise_key": "VARCHAR(128) NOT NULL DEFAULT ''",
    "garmin_category": "VARCHAR(64)",
    "garmin_name": "VARCHAR(128)",
    "movement_pattern": "VARCHAR(32) NOT NULL DEFAULT 'other'",
    "is_generic": "INTEGER NOT NULL DEFAULT 0",
    "duration_seconds": "INTEGER",
    "rest_seconds": "INTEGER NOT NULL DEFAULT 60",
    "warmup_enabled": "INTEGER NOT NULL DEFAULT 0",
    "warmup_reps": "INTEGER",
    "warmup_duration_seconds": "INTEGER",
    "warmup_weight_kg": "FLOAT",
}


def _migrate_add_columns(target_engine: Engine | None = None) -> None:
    from sqlalchemy import inspect, text

    eng = target_engine or engine
    insp = inspect(eng)
    with eng.begin() as conn:
        # Migrate activities
        existing_act = {c["name"] for c in insp.get_columns("activities")}
        missing_act = {k: v for k, v in _ACTIVITY_ADD_COLUMNS.items() if k not in existing_act}
        for col, sqltype in missing_act.items():
            conn.execute(text(f"ALTER TABLE activities ADD COLUMN {col} {sqltype}"))

        # Migrate daily_health
        existing_dh = {c["name"] for c in insp.get_columns("daily_health")}
        missing_dh = {k: v for k, v in _DAILY_HEALTH_ADD_COLUMNS.items() if k not in existing_dh}
        for col, sqltype in missing_dh.items():
            conn.execute(text(f"ALTER TABLE daily_health ADD COLUMN {col} {sqltype}"))

        # Migrate sleep
        existing_sleep = {c["name"] for c in insp.get_columns("sleep")}
        missing_sleep = {k: v for k, v in _SLEEP_ADD_COLUMNS.items() if k not in existing_sleep}
        for col, sqltype in missing_sleep.items():
            conn.execute(text(f"ALTER TABLE sleep ADD COLUMN {col} {sqltype}"))

        # Device capability registry provenance and bounded-probe journal.
        existing_capabilities = {c["name"] for c in insp.get_columns("device_capabilities")}
        for col, sqltype in _DEVICE_CAPABILITY_ADD_COLUMNS.items():
            if col not in existing_capabilities:
                conn.execute(text(f"ALTER TABLE device_capabilities ADD COLUMN {col} {sqltype}"))

        conn.execute(text(
            "CREATE TABLE IF NOT EXISTS app_migrations ("
            "migration_key VARCHAR(128) PRIMARY KEY, applied_at DATETIME NOT NULL)"
        ))
        already_scoped = conn.execute(
            text("SELECT 1 FROM app_migrations WHERE migration_key = :key"),
            {"key": _CAPABILITY_SCOPE_MIGRATION_KEY},
        ).first()
        if not already_scoped:
            _migrate_capability_scopes(conn)
            conn.execute(text(
                "INSERT INTO app_migrations (migration_key, applied_at) "
                "VALUES (:key, CURRENT_TIMESTAMP)"
            ), {"key": _CAPABILITY_SCOPE_MIGRATION_KEY})

        body_gate_applied = conn.execute(
            text("SELECT 1 FROM app_migrations WHERE migration_key = :key"),
            {"key": _BODY_COMPOSITION_CONTRACT_GATE_MIGRATION_KEY},
        ).first()
        if not body_gate_applied:
            _normalize_body_composition_contract_gate(conn)
            conn.execute(text(
                "INSERT INTO app_migrations (migration_key, applied_at) VALUES (:key, CURRENT_TIMESTAMP)"
            ), {"key": _BODY_COMPOSITION_CONTRACT_GATE_MIGRATION_KEY})

        # New tables are supplied by metadata.create_all().  Keep this marker
        # separate from the older capability/body-composition migrations and
        # write it only after policy seeding and validation succeeds.
        progression_applied = conn.execute(
            text("SELECT 1 FROM app_migrations WHERE migration_key = :key"),
            {"key": _STRENGTH_PROGRESSION_FOUNDATION_MIGRATION_KEY},
        ).first()
        if not progression_applied:
            _seed_strength_progression_policy(conn)
            conn.execute(text(
                "INSERT INTO app_migrations (migration_key, applied_at) VALUES (:key, CURRENT_TIMESTAMP)"
            ), {"key": _STRENGTH_PROGRESSION_FOUNDATION_MIGRATION_KEY})
        else:
            _seed_strength_progression_policy(conn)

        review_actions_applied = conn.execute(
            text("SELECT 1 FROM app_migrations WHERE migration_key = :key"),
            {"key": _STRENGTH_PROGRESSION_REVIEW_ACTIONS_MIGRATION_KEY},
        ).first()
        if not review_actions_applied:
            _validate_strength_progression_review_actions(conn)
            conn.execute(text(
                "INSERT INTO app_migrations (migration_key, applied_at) VALUES (:key, CURRENT_TIMESTAMP)"
            ), {"key": _STRENGTH_PROGRESSION_REVIEW_ACTIONS_MIGRATION_KEY})

        # Migrate athlete_profile
        existing_profile = {c["name"] for c in insp.get_columns("athlete_profile")}
        missing_profile = {k: v for k, v in _ATHLETE_PROFILE_ADD_COLUMNS.items() if k not in existing_profile}
        for col, sqltype in missing_profile.items():
            conn.execute(text(f"ALTER TABLE athlete_profile ADD COLUMN {col} {sqltype}"))

        # Migrate program_sessions
        existing_ps = {c["name"] for c in insp.get_columns("program_sessions")}
        missing_ps = {k: v for k, v in _PROGRAM_SESSION_ADD_COLUMNS.items() if k not in existing_ps}
        for col, sqltype in missing_ps.items():
            conn.execute(text(f"ALTER TABLE program_sessions ADD COLUMN {col} {sqltype}"))

        # Migrate training_programs
        existing_programs = {c["name"] for c in insp.get_columns("training_programs")}
        missing_programs = {
            k: v for k, v in _TRAINING_PROGRAM_ADD_COLUMNS.items()
            if k not in existing_programs
        }
        for col, sqltype in missing_programs.items():
            conn.execute(text(f"ALTER TABLE training_programs ADD COLUMN {col} {sqltype}"))

        # Migrate planned_sessions completion audit fields.
        existing_planned = {c["name"] for c in insp.get_columns("planned_sessions")}
        for col, sqltype in _PLANNED_SESSION_ADD_COLUMNS.items():
            if col not in existing_planned:
                conn.execute(text(f"ALTER TABLE planned_sessions ADD COLUMN {col} {sqltype}"))
        # Existing programs predate proposal review and were already active.
        conn.execute(text(
            "UPDATE training_programs SET status = 'active' "
            "WHERE active = 1 AND status = 'draft'"
        ))
        conn.execute(text(
            "UPDATE training_programs SET activated_at = "
            "COALESCE(created_at, updated_at, CURRENT_TIMESTAMP) "
            "WHERE active = 1 AND activated_at IS NULL"
        ))

        # Create session_exercises table if it doesn't exist yet
        conn.execute(text(_SESSION_EXERCISES_CREATE))
        existing_exercises = {c["name"] for c in inspect(conn).get_columns("session_exercises")}
        for col, sqltype in _SESSION_EXERCISE_ADD_COLUMNS.items():
            if col not in existing_exercises:
                conn.execute(text(f"ALTER TABLE session_exercises ADD COLUMN {col} {sqltype}"))

        # One-time data fixes are tracked so a later user edit is never
        # overwritten on every startup.
        rest_migration = "total_package_default_rest_60_v1"
        already_applied = conn.execute(
            text("SELECT 1 FROM app_migrations WHERE migration_key = :key"),
            {"key": rest_migration},
        ).first()
        if not already_applied:
            conn.execute(text(
                "UPDATE session_exercises SET rest_seconds = 60 "
                "WHERE rest_seconds = 90 AND program_session_id IN ("
                "SELECT ps.id FROM program_sessions ps "
                "JOIN training_programs tp ON tp.id = ps.program_id "
                "WHERE tp.goal_tags LIKE '%total_package_3%'"
                ")"
            ))
            conn.execute(
                text(
                    "INSERT INTO app_migrations (migration_key, applied_at) "
                    "VALUES (:key, CURRENT_TIMESTAMP)"
                ),
                {"key": rest_migration},
            )

        total_package_name_migration = "total_package_session_names_2026_07_19_v1"
        already_applied = conn.execute(
            text("SELECT 1 FROM app_migrations WHERE migration_key = :key"),
            {"key": total_package_name_migration},
        ).first()
        if not already_applied:
            # Rename only unchanged legacy labels in the curated Total Package
            # program. Other programs and user-created sessions stay untouched.
            conn.execute(text(
                "UPDATE program_sessions SET name = CASE name "
                "WHEN 'Day 1' THEN 'Full Body 1' "
                "WHEN 'Day 2' THEN 'Full Body 2' "
                "WHEN 'Day 3' THEN 'Full Body 3' "
                "END "
                "WHERE name IN ('Day 1', 'Day 2', 'Day 3') "
                "AND program_id IN ("
                "SELECT id FROM training_programs "
                "WHERE goal_tags LIKE '%total_package_3%'"
                ")"
            ))
            conn.execute(
                text(
                    "INSERT INTO app_migrations (migration_key, applied_at) "
                    "VALUES (:key, CURRENT_TIMESTAMP)"
                ),
                {"key": total_package_name_migration},
            )

        source_rest_migration = "source_rest_periods_2026_07_18_v1"
        already_applied = conn.execute(
            text("SELECT 1 FROM app_migrations WHERE migration_key = :key"),
            {"key": source_rest_migration},
        ).first()
        if not already_applied:
            # Only replace values that are still equal to the previous catalog
            # default. Matching the source program, session, and exercise keeps
            # custom routines and already-customized timers out of scope.
            from coach.programs import PROGRAMS

            beginner_anchors = {"Trap Bar Deadlift", "Front Squat", "Bench Press"}
            shul_strength_anchors = {
                "Front Squat", "Trap Bar Deadlift", "Dumbbell Bench Press",
                "One Arm Dumbbell Row", "Overhead Press",
            }
            shul_hypertrophy_isolations = {
                "Leg Extension", "Standing Machine Calf Raise", "Face Pull",
                "Lateral Raise", "Barbell Curl", "Incline Skullcrusher",
            }

            def previous_rest(program_key: str, session_name: str, exercise_name: str, current: int) -> int:
                if program_key == "beginner_full_body_3":
                    if exercise_name in beginner_anchors:
                        return 180
                    if exercise_name == "Farmer's Carry":
                        return 90
                elif program_key == "ms_full_body_3" and exercise_name == "Romanian Deadlift":
                    return 120
                elif program_key == "total_package_3":
                    return 60
                elif program_key == "upper_lower_4":
                    return 60
                elif program_key == "shul_4":
                    if session_name in {"Lower Strength", "Upper Strength"}:
                        return 180 if exercise_name in shul_strength_anchors else 90
                    if exercise_name in shul_hypertrophy_isolations:
                        return 60
                elif program_key == "muscle_strength_5":
                    return 120 if session_name in {"Upper Strength", "Lower Strength"} else 60
                return current

            legacy_session_names = {
                "beginner_full_body_3": {
                    "Full Body 1": "Full Body A",
                    "Full Body 2": "Full Body B",
                    "Full Body 3": "Full Body C",
                },
                "ms_full_body_3": {
                    "Full Body 1": "Workout A",
                    "Full Body 2": "Workout B",
                    "Full Body 3": "Workout C",
                },
                "total_package_3": {
                    "Full Body 1": "Day 1",
                    "Full Body 2": "Day 2",
                    "Full Body 3": "Day 3",
                },
            }

            for program_key in (
                "beginner_full_body_3", "ms_full_body_3", "total_package_3",
                "upper_lower_4", "shul_4", "muscle_strength_5",
            ):
                for routine in PROGRAMS[program_key]["sessions"]:
                    for exercise in routine["exercises"]:
                        new_rest = exercise["rest_seconds"]
                        old_rest = previous_rest(
                            program_key, routine["name"], exercise["exercise_name"], new_rest,
                        )
                        if old_rest == new_rest:
                            continue
                        conn.execute(
                            text(
                                "UPDATE session_exercises SET rest_seconds = :new_rest "
                                "WHERE exercise_name = :exercise_name "
                                "AND rest_seconds = :old_rest "
                                "AND program_session_id IN ("
                                "SELECT ps.id FROM program_sessions ps "
                                "JOIN training_programs tp ON tp.id = ps.program_id "
                                "WHERE (ps.name = :session_name "
                                "OR ps.name = :legacy_session_name) "
                                "AND tp.goal_tags LIKE :program_key"
                                ")"
                            ),
                            {
                                "new_rest": new_rest,
                                "old_rest": old_rest,
                                "exercise_name": exercise["exercise_name"],
                                "session_name": routine["name"],
                                "legacy_session_name": legacy_session_names.get(
                                    program_key, {}
                                ).get(routine["name"], routine["name"]),
                                "program_key": f"%{program_key}%",
                            },
                        )
            conn.execute(
                text(
                    "INSERT INTO app_migrations (migration_key, applied_at) "
                    "VALUES (:key, CURRENT_TIMESTAMP)"
                ),
                {"key": source_rest_migration},
            )

        purge_empty_migration = "purge_empty_activities_2026_07_25_v1"
        already_applied = conn.execute(
            text("SELECT 1 FROM app_migrations WHERE migration_key = :key"),
            {"key": purge_empty_migration},
        ).first()
        if not already_applied:
            conn.execute(text(
                "DELETE FROM activities WHERE duration_s IS NULL OR duration_s <= 0 "
                "OR id IN (7001, 7002, 8101, 8102, 8103, 9100, 9101, 9102, 9103, 9300, 9301, 9302, 9303, 9304, 9305, 9901)"
            ))
            conn.execute(
                text(
                    "INSERT INTO app_migrations (migration_key, applied_at) "
                    "VALUES (:key, CURRENT_TIMESTAMP)"
                ),
                {"key": purge_empty_migration},
            )



@contextmanager
def get_session() -> Iterator:
    if config.MULTI_USER_ENABLED:
        # Imported lazily to avoid a module cycle: tenant_store uses this
        # module's athlete schema when provisioning a physical user database.
        from tenant_store import get_current_user_session

        with get_current_user_session() as session:
            yield session
        return
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
