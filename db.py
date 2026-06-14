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
    Integer,
    String,
    Text,
    create_engine,
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


class Sleep(Base):
    __tablename__ = "sleep"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    total_s: Mapped[Optional[float]] = mapped_column(Float)
    deep_s: Mapped[Optional[float]] = mapped_column(Float)
    light_s: Mapped[Optional[float]] = mapped_column(Float)
    rem_s: Mapped[Optional[float]] = mapped_column(Float)
    awake_s: Mapped[Optional[float]] = mapped_column(Float)
    score: Mapped[Optional[float]] = mapped_column(Float)


class DailyHealth(Base):
    __tablename__ = "daily_health"

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    resting_hr: Mapped[Optional[float]] = mapped_column(Float)
    hrv_overnight: Mapped[Optional[float]] = mapped_column(Float)
    body_battery_high: Mapped[Optional[float]] = mapped_column(Float)
    body_battery_low: Mapped[Optional[float]] = mapped_column(Float)
    stress_avg: Mapped[Optional[float]] = mapped_column(Float)
    steps: Mapped[Optional[int]] = mapped_column(Integer)


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


class CoachMessage(Base):
    __tablename__ = "coach_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role: Mapped[str] = mapped_column(String(16))  # suggestion | user | assistant
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime, index=True)
    data_snapshot: Mapped[Optional[str]] = mapped_column(Text)  # JSON of facts used


class WeeklySummary(Base):
    __tablename__ = "weekly_summaries"

    year_week: Mapped[str] = mapped_column(String(10), primary_key=True)  # e.g., "2026-W24"
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)


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


engine = create_engine(config.DB_URL, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)
    _migrate_add_columns()


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
}


def _migrate_add_columns() -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    existing = {c["name"] for c in insp.get_columns("activities")}
    missing = {k: v for k, v in _ACTIVITY_ADD_COLUMNS.items() if k not in existing}
    if not missing:
        return
    with engine.begin() as conn:
        for col, sqltype in missing.items():
            conn.execute(text(f"ALTER TABLE activities ADD COLUMN {col} {sqltype}"))


@contextmanager
def get_session() -> Iterator:
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
