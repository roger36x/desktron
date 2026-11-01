"""Failed/interrupted process rows stay neutral; failure detail remains expandable."""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend/style.css").read_text(encoding='utf-8')
CHAT = "\n".join(
    (ROOT / "frontend" / name).read_text(encoding='utf-8')
    for name in (
        "app-chat-core.js", "app-chat-model.js", "app-chat-reconcile.js",
        "app-chat-composer.js", "app-chat-input.js", "app-chat-page.js",
    )
)


def test_failed_process_status_uses_the_same_neutral_color_as_other_terminal_states():
    assert ".hs-page .hs-status.st-done,\n.hs-page .hs-status.st-error { color: var(--ink-44); }" in CSS
    assert "html[data-theme=\"light\"] .hs-page .hs-status.st-done,\nhtml[data-theme=\"light\"] .hs-page .hs-status.st-error { color: var(--ink-50); }" in CSS
    assert ".hs-page .hs-status.st-error { color: #e08585; }" not in CSS


def test_failed_process_steps_keep_the_normal_step_label_color():
    assert ".hs-page .hs-step.run .hs-step-n,\n.hs-page .hs-step.err .hs-step-n { color: var(--ink-80); }" in CSS
    assert "html[data-theme=\"light\"] .hs-page .hs-step .hs-step-n { color: var(--ink-80); }" in CSS
    assert ".hs-page .hs-step.err .hs-step-n { color: #e08585; }" not in CSS


def test_process_status_stays_bold_while_action_copy_is_only_grayer():
    assert ".hs-page .hs-step-n { color: var(--ink-80); font-weight: 550;" in CSS
    assert ".hs-page .hs-proc-body { line-height: 1.6; color: var(--ink-70); }" in CSS
    assert ".hs-page .hs-step-h .hs-step-a { font-weight: inherit; }" in CSS
    assert ".hs-page .hs-step-a.txt, .hs-page .hs-step-ret { font-family: inherit; font-size: inherit; color: var(--ink-56); }" in CSS
    step_head = CSS[CSS.index(".hs-page .hs-step-h {"):CSS.index(".hs-page .hs-step-h:hover")]
    assert "font-weight: 300" not in step_head.split(".hs-step-h .hs-step-a", 1)[0]


def test_narrative_and_process_long_text_share_compact_line_height():
    assert re.search(r"\.hs-page \.hs-stream \{[^}]*font-size: 14\.5px; line-height: 1\.6", CSS)
    assert ".hs-page .hs-proc-body { line-height: 1.6; color: var(--ink-70); }" in CSS
    assert ".hs-page .hs-think-body { line-height: 1.6; }" in CSS
    assert ".hs-page .hs-text.md p { margin: 0 0 14px; }" in CSS
    assert ".hs-page .hs-text.md ul, .hs-page .hs-text.md ol { margin: 0 0 14px; }" in CSS
    assert ".hs-page .hs-proc-body .md p { margin: 0 0 8px; }" in CSS


def test_process_typography_is_half_a_pixel_larger_on_desktop_and_mobile():
    """过程区共用基础规则；没有媒体查询覆盖时，两端会一起生效。"""
    assert re.search(r"\.hs-page \.hs-proc-bar \{[^}]*font-size: 14\.5px", CSS)
    assert re.search(r"\.hs-page \.hs-status \{[^}]*font-size: 14\.5px", CSS)
    assert re.search(r"\.hs-page \.hs-step-h \{[^}]*font-size: 14px", CSS)
    assert re.search(r"\.hs-page \.hs-think-body \{[^}]*font-size: 14px", CSS)
    assert re.search(r"\.hs-page \.hs-text\.hs-lead \{[^}]*font-size: 13px", CSS)


def test_actual_error_surfaces_still_keep_error_emphasis():
    assert ".hs-page .hs-env.error .hs-env-k { color: #e08585; }" in CSS
    assert ".hs-page .hs-note { margin: 14px 0; color: #e08585;" in CSS


def test_process_error_status_stays_neutral_and_attaches_structured_detail():
    calls = re.findall(r'hsStatus\([^\n;]*"error"[^\n;]*\)', CHAT)
    assert calls
    assert all('"已停止"' in call for call in calls), calls
    tools = (ROOT / "frontend" / "app-chat-tools.js").read_text(encoding='utf-8')
    reconcile = (ROOT / "frontend" / "app-chat-reconcile.js").read_text(encoding='utf-8')
    assert "function hsProcFailure(t, error)" in tools
    assert "hsProcFailure(tail, p.error);" in CHAT
    assert "hsProcFailure(t, info.error);" in reconcile
    assert re.search(r"function hsErrorNotice\(v, message, stage, details(?:, identity)?\)", CHAT)
    assert re.search(r'hsEnvironment\(v, \{ (?:\.\.\.identity, )?state: "error"', CHAT)


def test_persistent_warning_and_error_bars_remain_expandable():
    assert 'if (["warning", "error"].includes(state) && !details.length) details.push(label);' in CHAT
    assert "function hsEnvironmentToggle(v)" in CHAT
    assert 'el.setAttribute("aria-expanded", String(open))' in CHAT
