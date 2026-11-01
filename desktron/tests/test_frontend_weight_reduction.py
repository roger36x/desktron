"""Non-Windows UI -50 weights; Windows keeps explicit weights with lighter reading."""
from pathlib import Path
import re

import pytest

CSS = (Path(__file__).resolve().parents[2] / 'frontend/style.css').read_text(encoding='utf-8')
CLEAN = re.sub(r'/\*.*?\*/', '', CSS, flags=re.S)


def block(selector):
    found = [body for selectors, body in re.findall(r'([^{}]+)\{([^{}]*)\}', CLEAN)
             if selector in [s.strip() for s in selectors.split(',')]]
    assert found, selector
    return '\n'.join(found)


def test_regular_and_medium_tokens_are_reduced_once():
    assert '--font-weight-regular: 350;' in block(':root')
    assert '--font-weight-medium: 450;' in block(':root')
    assert 'font-weight: var(--font-weight-regular);' in block('body')
    assert '--windows-ui-weight: 350;' in block('html.os-windows')
    assert '--chat-read-weight: 350;' in block('html.os-windows')
    assert '--font-weight-regular: 400;' in block('html.os-windows')
    assert '--font-weight-medium: 500;' in block('html.os-windows')
    assert 'font-weight: var(--font-weight-medium);' in block('.hs-page .hs-text.md p')


@pytest.mark.parametrize(('selector', 'weight'), [
    ('.side-brand .brand', 550),
    ('#term-head', 550),
    ('.ss-hit-at', 550),
    ('.rp-ctx .rp-ctx-head .rp-value', 350),
    ('.hs-page .hs-step-n', 550),
    ('.hs-page .hs-current-task-title', 510),
    ('.auth-tui-kicker', 600),
    ('.auth-tui-terminal .auth-tui-title strong', 510),
    ('.sm-group-title', 650),
    ('.setup-score strong', 600),
    ('.settings-center-live .sm-section > .sm-title', 570),
    ('.arr-sys-status-intro h2', 530),
    ('.hs-page .hs-peer-label', 400),
    ('#sm-update-status', 350),
    ('.hs-activity', 350),
])
def test_explicit_component_weights_keep_their_hierarchy(selector, weight):
    values = re.findall(r'font-weight:\s*(\d+)\s*;', block(selector))
    assert values[-1] == str(weight)


def test_font_shorthand_does_not_reset_controls_to_old_normal_weight():
    values = re.findall(r'(?<![\w-])font:\s*([^;{}]+)', CLEAN)
    concrete = [value for value in values if value != 'inherit']
    assert len(concrete) >= 14
    assert all(re.match(r'(350|510)\s+\d', value) for value in concrete)
    assert 'font-weight: inherit;' in block('.hs-page .hs-step-h .hs-step-a')
    # No parent-relative subtraction, which would compound for nested elements.
    assert not re.search(r'font-weight:\s*calc\(', CLEAN)


def test_font_face_still_describes_the_real_regular_file():
    faces = re.findall(r'@font-face\s*\{([^}]+)\}', CLEAN)
    mono = next(face for face in faces if 'font-family: "Desk Mono";' in face)
    assert 'font-weight: 400;' in mono
    assert 'font-weight: 350;' not in mono
    assert 'font-synthesis-weight: none;' in block('html.os-windows')


def test_browser_defaults_and_highlight_bold_are_also_reduced():
    assert ':where(button, input, textarea, select) { font-weight: var(--font-weight-regular); }' in CLEAN
    assert ':where(h1, h2, h3, h4, h5, h6, strong, b, th) { font-weight: 650; }' in CLEAN
    assert 'font-weight: 650;' in block('.hljs-strong')
