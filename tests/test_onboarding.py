from datetime import datetime, timedelta

from coach.onboarding import analyze_user_history
from db import Activity, ExerciseSet, PlannedSession, SyncState, Workout


def _activity(session, activity_id: int, activity_type: str, days_ago: int, name: str = "") -> None:
    session.add(
        Activity(
            id=activity_id,
            activity_type=activity_type,
            start_time=datetime.now() - timedelta(days=days_ago),
            name=name,
        )
    )


def _workout(session, workout_id: int, name: str, sport_type: str = "strength_training") -> None:
    session.add(
        Workout(
            workout_id=workout_id,
            name=name,
            sport_type=sport_type,
            steps_json="[]",
        )
    )


def _strength_session(session, activity_id: int, days_ago: int, name: str, exercises: list[str]) -> None:
    _activity(session, activity_id, "strength_training", days_ago, name)
    for index, exercise in enumerate(exercises):
        session.add(ExerciseSet(
            activity_id=activity_id,
            set_index=index,
            exercise_category=exercise,
            exercise_name=exercise,
            reps=8,
            weight_kg=40,
        ))


def test_no_history_defaults_to_low_history(session):
    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "low_history"
    assert analysis["defaults"]["primary_goal"] == "Feel fitter & more consistent"
    assert analysis["defaults"]["days_per_week"] == 2
    assert analysis["total_activities"] == 0


def test_strength_history_selects_strength_defaults_without_templates(session):
    for idx in range(10):
        _activity(session, idx + 1, "strength_training", idx * 3)
    _activity(session, 20, "running", 8)
    _workout(session, 501, "Upper Strength")
    _workout(session, 502, "Lower Strength")
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "strength_focused"
    assert analysis["defaults"]["primary_goal"] == "Build strength & muscle"
    assert "Strength" in analysis["defaults"]["preferred_activities"]
    assert analysis["defaults"]["plan_mode"] == "schedule_my_routine"
    assert analysis["defaults"]["selected_templates"] == []


def test_coach_created_workouts_are_excluded_without_name_prefix(session):
    _workout(session, 501, "User Workout")
    _workout(session, 502, "Scheduled Workout @ 18:00")
    _workout(session, 503, "Legacy Coach Workout")
    session.query(Workout).filter_by(workout_id=503).one().name = "\U0001f3cb\ufe0f Legacy Coach Workout"
    session.add(PlannedSession(
        activity_type="strength_training",
        title="Scheduled Workout",
        target_date=datetime.now().date(),
        garmin_workout_id=502,
    ))
    session.add(SyncState(key="last_coach_workout_id", value="504"))
    _workout(session, 504, "Latest Scheduled Workout")
    session.commit()

    analysis = analyze_user_history(session)

    assert [template["name"] for template in analysis["templates"]] == ["User Workout"]


def test_recent_routine_uses_one_90_day_dataset_while_background_keeps_all_history(session):
    for idx in range(27):
        _activity(session, idx + 1, "strength_training", 100 + idx)
    for idx in range(16):
        _activity(session, 100 + idx, "strength_training", idx * 4)
    for idx in range(3):
        _activity(session, 130 + idx, "yoga", 10 + idx)
        _activity(session, 140 + idx, "soccer", 20 + idx)
    _activity(session, 150, "indoor_cardio", 5)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "strength_focused"
    assert analysis["total_activities"] == 23
    assert sum(row["sessions"] for row in analysis["activity_patterns"]) == 23
    assert analysis["recent_routine"]["total_activities"] == 23
    assert analysis["training_background"]["total_activities"] == 50
    assert analysis["training_background"]["experience_level"] == "six_to_twenty_four_months"


def test_mixed_strength_and_cardio_history(session):
    for idx in range(5):
        _activity(session, idx + 1, "strength_training", idx)
        _activity(session, idx + 20, "running", idx + 10)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "mixed_fitness"
    assert analysis["defaults"]["primary_goal"] == "Feel fitter & more consistent"


def test_sport_recreational_history(session):
    for idx in range(6):
        _activity(session, idx + 1, "soccer", idx)
    for idx in range(4):
        _activity(session, idx + 20, "running", idx + 20)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["classification"]["training_type"] == "sport_recreational"
    assert analysis["defaults"]["primary_goal"] == "Improve a sport/activity"


def test_history_recommends_push_pull_legs_from_repeated_exercise_patterns(session):
    for index, (name, exercises) in enumerate([
        ("Push", ["BENCH_PRESS", "OVERHEAD_PRESS", "TRICEPS_EXTENSION"]),
        ("Pull", ["LAT_PULL_DOWN", "BENT_OVER_ROW", "BICEP_CURL"]),
        ("Legs", ["SQUAT", "LUNGE", "CALF_RAISE"]),
    ] * 2):
        _strength_session(session, index + 1, 20 - index, name, exercises)
    session.commit()

    analysis = analyze_user_history(session)

    assert analysis["plan_recommendation"]["key"] == "ppl_6"
    assert "exercise-backed" in analysis["plan_recommendation"]["reason"]
    assert analysis["plan_matches"]["ppl_6"]["is_best"] is True
    assert analysis["plan_matches"]["ppl_6"]["rank"] == 1
    assert analysis["plan_matches"]["ppl_6"]["score"] >= 80
    assert len(analysis["plan_matches"]) == 25


def test_history_recommends_upper_lower_from_repeated_exercise_patterns(session):
    for index, (name, exercises) in enumerate([
        ("Upper", ["BENCH_PRESS", "BENT_OVER_ROW", "OVERHEAD_PRESS"]),
        ("Lower", ["SQUAT", "LUNGE", "LEG_CURL"]),
    ] * 3):
        _strength_session(session, index + 1, 20 - index, name, exercises)
    session.commit()

    assert analyze_user_history(session)["plan_recommendation"]["key"] == "maul_5"


def test_history_recommends_matching_full_body_routine_from_frequent_sessions(session):
    for index in range(6):
        _strength_session(session, index + 1, 13 - index * 2, "Full Body", ["SQUAT", "BENCH_PRESS", "BENT_OVER_ROW"])
    session.commit()

    assert analyze_user_history(session)["plan_recommendation"]["key"] == "beginner_full_body_3"


def test_sparse_or_name_only_history_falls_back_to_full_body_two_days(session):
    for index in range(6):
        _activity(session, index + 1, "strength_training", index, "Push")
    session.commit()

    recommendation = analyze_user_history(session)["plan_recommendation"]
    assert recommendation["key"] == "full_body_2"
    assert "No reliable split" in recommendation["reason"]


def test_every_routine_has_an_explainable_ranked_match_score(session):
    for index in range(6):
        _strength_session(
            session,
            index + 1,
            13 - index * 2,
            "Full Body",
            ["SQUAT", "BENCH_PRESS", "BENT_OVER_ROW"],
        )
    session.commit()

    analysis = analyze_user_history(session)
    matches = analysis["plan_matches"]

    assert sorted(match["rank"] for match in matches.values()) == list(range(1, 26))
    assert all(0 <= match["score"] <= 100 for match in matches.values())
    assert all(match["label"] in {"Strong match", "Good match", "Possible match", "Poor fit"} for match in matches.values())
    assert all("structure" in match["evidence"] for match in matches.values())
    assert sum(match["is_best"] for match in matches.values()) == 1


def test_recommendation_uses_inferred_intermediate_experience(session):
    for index in range(20):
        _strength_session(
            session,
            index + 1,
            100 + index,
            "Full Body",
            ["SQUAT", "BENCH_PRESS", "BENT_OVER_ROW"],
        )
    recent_days = [14, 11, 8, 5, 2, 0]
    for index, days_ago in enumerate(recent_days):
        _strength_session(
            session,
            100 + index,
            days_ago,
            "Full Body",
            ["SQUAT", "BENCH_PRESS", "BENT_OVER_ROW"],
        )
    session.commit()

    recommendation = analyze_user_history(session)["plan_recommendation"]

    assert recommendation["key"] == "total_package_3"
    assert "inferred 6-24 months training background" in recommendation["reason"]


def test_recommendation_uses_inferred_expert_level_for_upper_lower(session):
    for index in range(80):
        _strength_session(
            session,
            index + 1,
            100 + index,
            "Full Body",
            ["SQUAT", "BENCH_PRESS", "BENT_OVER_ROW"],
        )
    for index, (name, exercises) in enumerate([
        ("Upper", ["BENCH_PRESS", "BENT_OVER_ROW", "OVERHEAD_PRESS"]),
        ("Lower", ["SQUAT", "LUNGE", "LEG_CURL"]),
    ] * 4):
        _strength_session(session, 200 + index, 13 - index * 2, name, exercises)
    session.commit()

    recommendation = analyze_user_history(session)["plan_recommendation"]

    assert recommendation["key"] == "advanced_upper_lower_4"
    assert "inferred more than 2 years training background" in recommendation["reason"]
