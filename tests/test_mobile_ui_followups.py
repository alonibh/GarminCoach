"""Source-level mobile regressions for the production screenshot follow-ups."""
from pathlib import Path

import app as app_module


ROOT = Path(__file__).resolve().parents[1]


def test_pager_is_not_a_site_navigation_and_has_stable_slots():
    dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "ui.css").read_text(encoding="utf-8")

    assert '<nav class="activity-pagination"' not in dashboard
    assert 'class="activity-pagination" role="navigation"' in dashboard
    assert 'aria-disabled="true">Previous' in dashboard
    assert 'aria-disabled="true">Next' in dashboard
    assert dashboard.index('class="activity-pagination"') > dashboard.index('class="activity-mobile-list"')
    assert "position: static" in css and "grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)" in css


def test_dashboard_activity_paging_is_progressively_enhanced():
    dashboard = (ROOT / "templates" / "dashboard.html").read_text(encoding="utf-8")
    app_source = (ROOT / "app.py").read_text(encoding="utf-8")

    assert 'id="recent-workouts"' in dashboard
    assert 'data-activity-page-link href="{{ activity_previous_url }}"' in dashboard
    assert 'data-activity-page-link href="{{ activity_next_url }}"' in dashboard
    assert "document.addEventListener('click', event =>" in dashboard
    assert "event.preventDefault();" in dashboard
    assert "fetch(url" in dashboard and "new DOMParser()" in dashboard
    assert "getBoundingClientRect().top" in dashboard and "current.replaceWith(replacement)" in dashboard
    assert "window.scrollBy(0, newTop - oldTop)" in dashboard
    assert "history.pushState" in dashboard and "popstate" in dashboard and "AbortController" in dashboard
    assert "window.location.assign(url)" in dashboard
    assert 'f"/?activity_page={activity_page - 1}#recent-workouts"' in app_source
    assert 'f"/?activity_page={activity_page + 1}#recent-workouts"' in app_source


def test_fitness_age_decimal_formatter_rejects_invalid_values():
    assert app_module._format_metric_decimal(19.7528413103418) == "19.8"
    assert app_module._format_metric_decimal(19.341608959802667) == "19.3"
    assert app_module._format_metric_decimal(20) == "20.0"
    assert app_module._format_metric_decimal(0) == "0.0"
    assert app_module._format_metric_decimal(float("nan")) is None
    assert app_module._format_metric_decimal(float("inf")) is None
    assert app_module._format_metric_decimal(True) is None


def test_mobile_exercise_and_program_state_contracts_are_explicit():
    workout = (ROOT / "templates" / "workout.html").read_text(encoding="utf-8")
    program = (ROOT / "templates" / "program.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "ui.css").read_text(encoding="utf-8")

    assert 'class="exercise-meta muted"' in workout
    assert workout.index('exercise-name') < workout.index('exercise-meta')
    assert 'sets-field' in program and 'rest-field' in program
    assert "is-timed" in program and "warmup-is-timed" in program
    assert "row.classList.toggle('is-timed',timed)" in program
    assert "row.classList.toggle('warmup-is-timed',timed)" in program
    assert ".exercise-meta { grid-row: 2" in css
    final_mobile = css[css.rfind("/* Keep these mobile layout contracts last") :]
    assert 'grid-template-areas: "identity identity" "sets rest" "target load" "actions actions" "warmup warmup"' in final_mobile
    assert 'grid-template-areas: "identity identity" "sets rest" "target target" "actions actions" "warmup warmup"' in final_mobile
    for selector, area in (
        (".exercise-identity", "identity"),
        (".sets-field", "sets"),
        (".rest-field", "rest"),
        (".target-field", "target"),
        (".load-field", "load"),
        (".exercise-actions", "actions"),
        (".exercise-warmup", "warmup"),
    ):
        assert f"{selector} {{ grid-area: {area}; }}" in final_mobile
    assert "grid-template-columns: minmax(0, 1.4fr) minmax(0, .8fr)" in final_mobile
    assert ".exercise-row.warmup-is-timed .warmup-weight { display: none; }" in css


def test_final_mobile_target_and_warmup_controls_are_flexible():
    css = (ROOT / "static" / "ui.css").read_text(encoding="utf-8")
    final_mobile = css[css.rfind("/* Keep target controls shrinkable") :]

    assert ".target-field .target-control { display: grid !important" in final_mobile
    assert "minmax(3.4rem, .72fr)" in final_mobile
    assert ".target-field .target-select, .exercise-row .target-field .ex-target { width: 100% !important; min-width: 0 !important" in final_mobile
    assert "grid-template-columns: minmax(0, 1.45fr) minmax(5.25rem, .75fr)" in final_mobile
    assert "column-gap: 1rem" in final_mobile
    assert "warmup-target .target-control { display: grid !important" in final_mobile
    assert "minmax(3.4rem, .7fr)" in final_mobile
