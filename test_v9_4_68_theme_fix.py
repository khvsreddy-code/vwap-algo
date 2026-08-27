
from pathlib import Path

ROOT = Path(__file__).resolve().parent

def test_theme_overrides_are_after_base_css():
    s = (ROOT / "app.py").read_text(encoding="utf-8")
    base = s.index('"""', s.index('[data-testid="stSidebar"] > div'))
    marker = s.index("# ---------- final theme overrides ----------")
    nav = s.index("# ---------- same-tab page navigation ----------")
    assert base < marker < nav
    assert 'if ui_theme == "Light":' in s
    assert 'elif ui_theme == "Black & White":' in s
    assert 'background: #ffffff !important' in s
    assert 'background: #000000 !important' in s
