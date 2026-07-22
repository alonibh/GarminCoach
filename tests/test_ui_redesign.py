from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_dashboard_has_native_metric_hero_and_responsive_activities():
    dashboard = _read("templates/dashboard.html")

    assert 'class="hero-metrics"' in dashboard
    assert "hero_metrics.readiness.signals" in dashboard
    assert "hero_metrics.sleep.progress" in dashboard
    assert "hero_metrics.load.progress" in dashboard
    assert "Load ratio" in dashboard
    assert "Strain" not in dashboard
    assert 'class="activity-mobile-list"' in dashboard


def test_all_user_facing_pages_use_the_shared_shells():
    app_pages = [
        "account.html",
        "calendar.html",
        "dashboard.html",
        "onboarding.html",
        "program.html",
        "workout.html",
    ]
    focus_pages = [
        "app_login.html",
        "auth_login.html",
        "auth_message.html",
        "invitation.html",
        "login.html",
        "multi_onboarding.html",
    ]

    for name in app_pages:
        assert 'extends "base.html"' in _read(f"templates/{name}")
    for name in focus_pages:
        assert 'extends "focused_base.html"' in _read(f"templates/{name}")


def test_design_system_covers_accessibility_and_responsive_states():
    css = _read("static/ui.css")
    manifest = _read("static/manifest.json")
    service_worker = _read("static/sw.js")

    assert "prefers-reduced-motion: reduce" in css
    assert "min-height: 44px" in css
    assert ".skip-link" in css
    assert '"theme_color": "#0a0d10"' in manifest
    assert "garmincoach-cache-v4" in service_worker
    assert "'/static/ui.css'" in service_worker
