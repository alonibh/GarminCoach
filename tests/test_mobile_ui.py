from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_mobile_navigation_is_accessible_and_stateful():
    html = (PROJECT_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    script = (PROJECT_ROOT / "static" / "ui.js").read_text(encoding="utf-8")

    assert 'class="bottom-nav"' in html
    assert 'aria-label="Mobile navigation"' in html
    assert 'aria-controls="mobile-more"' in html
    assert 'aria-expanded="false"' in html
    assert 'role="dialog"' in html
    assert "setAttribute('aria-expanded', String(open))" in script
    assert "event.key === 'Escape'" in script
    assert "event.key !== 'Tab'" in script
    assert "lastFocus.focus()" in script


def test_dense_mobile_components_have_scoped_responsive_layouts():
    legacy_css = (PROJECT_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    ui_css = (PROJECT_ROOT / "static" / "ui.css").read_text(encoding="utf-8")
    calendar = (PROJECT_ROOT / "templates" / "calendar.html").read_text(encoding="utf-8")

    assert "body.sheet-open { overflow: hidden; }" in ui_css
    assert "env(safe-area-inset-bottom)" in ui_css
    assert ".calendar-scroll" in legacy_css
    assert 'class="calendar-scroll"' in calendar
    assert 'class="calendar-agenda"' in calendar
    assert 'aria-label="Monthly activity agenda"' in calendar


def test_desktop_sidebar_and_active_navigation_are_present():
    html = (PROJECT_ROOT / "templates" / "base.html").read_text(encoding="utf-8")
    css = (PROJECT_ROOT / "static" / "ui.css").read_text(encoding="utf-8")

    assert 'class="app-sidebar"' in html
    assert 'aria-current="page"' in html
    assert "current_path.startswith('/workout/')" in html
    assert "@media (max-width: 899px)" in css
    assert ".app-sidebar { display: none; }" in css
