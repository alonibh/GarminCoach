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
    event,
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
    hrv_baseline_low: Mapped[Optional[float]] = mapped_column(Float)
    hrv_baseline_high: Mapped[Optional[float]] = mapped_column(Float)
    body_battery_high: Mapped[Optional[float]] = mapped_column(Float)
    body_battery_low: Mapped[Optional[float]] = mapped_column(Float)
    stress_avg: Mapped[Optional[float]] = mapped_column(Float)
    steps: Mapped[Optional[int]] = mapped_column(Integer)
    step_goal: Mapped[Optional[int]] = mapped_column(Integer)
    total_kcal: Mapped[Optional[int]] = mapped_column(Integer)
    active_kcal: Mapped[Optional[int]] = mapped_column(Integer)
    bmr_kcal: Mapped[Optional[int]] = mapped_column(Integer)
    training_readiness: Mapped[Optional[int]] = mapped_column(Integer)
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


_DAILY_HEALTH_ADD_COLUMNS = {
    "hrv_baseline_low": "FLOAT",
    "hrv_baseline_high": "FLOAT",
    "step_goal": "INTEGER",
}


def _migrate_add_columns() -> None:
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    with engine.begin() as conn:
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
