from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_mobile_navigation_is_accessible_and_stateful():
    html = (PROJECT_ROOT / "templates" / "base.html").read_text(encoding="utf-8")

    assert 'aria-controls="primary-navigation"' in html
    assert 'aria-expanded="false"' in html
    assert 'aria-label="Primary navigation"' in html
    assert "setAttribute('aria-expanded', String(open))" in html
    assert "event.key === 'Escape'" in html
    assert "event.target.closest('a')" in html
    assert "onclick=\"document.querySelector('nav')" not in html


def test_dense_mobile_components_have_scoped_responsive_layouts():
    css = (PROJECT_ROOT / "static" / "style.css").read_text(encoding="utf-8")
    calendar = (PROJECT_ROOT / "templates" / "calendar.html").read_text(encoding="utf-8")

    assert "body.nav-open { overflow: hidden; }" in css
    assert "max-height: calc(100dvh - 60px);" in css
    assert ".calendar-scroll" in css
    assert "min-width: 620px;" in css
    assert 'class="calendar-scroll"' in calendar
    assert 'aria-label="Monthly workout calendar"' in calendar
