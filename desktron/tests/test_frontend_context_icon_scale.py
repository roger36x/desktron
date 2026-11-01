"""Context rail glyphs share visible bounds, not just transparent image boxes."""
from pathlib import Path
import re

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')


def test_visible_longest_edges_match_for_all_context_status_states():
    selectors = {
        'task': '.hs-page .hs-context-item > .hs-context-task-icon',
        'arrangement': '.hs-page .hs-context-item > .hs-context-arrangements-icon',
        'status': '.hs-page .hs-context-item > .hs-context-status-icon',
        'status-alert': '.hs-page .hs-context-status:is(.error, .warning) .hs-context-item > .hs-context-status-icon',
        'status-login': '.hs-page .hs-context-status.login .hs-context-item > .hs-context-status-icon',
    }
    visible = []
    for name, selector in selectors.items():
        rule = re.search(re.escape(selector) + r' \{([^}]+)\}', CSS).group(1)
        size = float(re.search(r'--hs-context-glyph-size: ([\d.]+)px', rule).group(1))
        image = Image.open(ROOT / f'frontend/icons/{name}.png').convert('RGBA')
        box = image.getchannel('A').point(lambda alpha: 255 if alpha >= 128 else 0).getbbox()
        visible.append(max(box[2] - box[0], box[3] - box[1]) / image.width * size)
    assert max(visible) - min(visible) < .03, visible
    assert all(14.7 < edge < 14.8 for edge in visible)


def test_scale_is_scoped_to_buttons_and_does_not_change_layout_or_popovers():
    rule = re.search(r'\.hs-page \.hs-context-item > \.hs-context-icon::before \{([^}]+)\}', CSS).group(1)
    for prop in ('width', 'height'):
        assert f'{prop}: 17px' in rule
    assert 'flex: 0 0 17px' in rule
    for prop in ('-webkit-mask-size', 'mask-size'):
        assert f'{prop}: var(--hs-context-glyph-size) var(--hs-context-glyph-size)' in rule
    # Fractional flex child sizes alter the inline button baseline in WebKit.
    assert 'flex: 0 0 var(--hs-context-glyph-size)' not in rule
    assert '.hs-page .hs-context-icon { width: 17px; height: 17px; flex: 0 0 17px;' in CSS
    assert 'html:not(.os-windows) .hs-page .hs-context-item > .hs-context-icon { transform: translateY(.5px); }' in CSS
    assert '.hs-context-status-popover-icon { width: 30px; height: 30px;' in CSS
    assert CSS.count('--hs-context-glyph-size:') == 5


def test_status_variants_do_not_reset_optical_size_via_higher_specificity_shorthand():
    # These selectors outrank the shared sizing rule. Only change mask-image.
    for state in ('error', 'warning', 'login'):
        selector = f'.hs-page .hs-context-status.{state} .hs-context-status-icon::before'
        match = re.search(re.escape(selector) + r'\s*(?:,[^{]+)?\s*\{([^}]+)\}', CSS)
        assert match, state
        declarations = re.sub(r'/\*.*?\*/', '', match.group(1), flags=re.S)
        assert 'mask-image:' in declarations
        assert not re.search(r'(?:^|;)\s*(?:-webkit-)?mask\s*:', declarations)
        assert 'contain' not in declarations
