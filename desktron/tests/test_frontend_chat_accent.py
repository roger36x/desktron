from pathlib import Path


CSS = (Path(__file__).parents[2] / "frontend" / "style.css").read_text(encoding='utf-8')


def test_chat_accent_is_scoped_blue():
    start = CSS.index(".overlay-content.hs-page {")
    end = CSS.index("\n}", start)
    rule = CSS[start:end]
    assert "--hs-accent: #8fb5ee" in rule
    assert "--hs-accent-soft: rgba(143,181,238,.35)" in rule
    assert "--accent: var(--hs-accent)" in rule
    assert "--accent-soft: var(--hs-accent-soft)" in rule


def test_light_chat_accent_has_matching_readable_blue():
    selector = 'html[data-theme="light"] .overlay-content.hs-page {'
    start = CSS.index(selector)
    end = CSS.index("\n}", start)
    rule = CSS[start:end]
    assert "--hs-accent: #3968a8" in rule
    assert "--hs-accent-soft: rgba(57,104,168,.25)" in rule


def test_diff_file_paths_follow_chat_accent():
    assert ".hs-page .hs-dfile { color: var(--accent);" in CSS
    assert ".hs-rv-body .hs-dfile { color: var(--accent);" in CSS
    assert "#aeb8ff" not in CSS
