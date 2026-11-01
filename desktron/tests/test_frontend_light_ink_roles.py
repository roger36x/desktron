from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "frontend" / "style.css").read_text(encoding="utf-8")


def test_light_theme_has_requested_ink_steps():
    for token, value in {
        "80": "#333333",
        "75": "#3c3c3c",
        "70": "#464646",
        "50": "#6b6b6b",
        "30": "#b1b1b1",
    }.items():
        assert f"--ink-{token}: {value};" in CSS


def test_light_theme_role_inks_are_scoped():
    expected = (
        'html[data-theme="light"] #panel { --sess-title: var(--ink-80); }',
        'html[data-theme="light"] #panel .term-item .ti-label { color: var(--ink-80); }',
        'html[data-theme="light"] #panel .term-item.active .ti-label { color: var(--ink-100); }',
        'html[data-theme="light"] .proj-fold .pf-name { color: var(--ink-70); }',
        'html[data-theme="light"] .side-sec { color: var(--ink-70); }',
        'html[data-theme="light"] .hs-page .hs-step .hs-step-n { color: var(--ink-80); }',
        'html[data-theme="light"] .hs-page .hs-step-a.cmd { color: var(--ink-75); }',
        'html[data-theme="light"] .hs-page .hs-proc-body .hs-think-body { color: var(--ink-70); }',
        'html[data-theme="light"] .hs-page .hs-status { color: var(--ink-60); }',
        'html[data-theme="light"] .hs-page .hs-status.st-done,\nhtml[data-theme="light"] .hs-page .hs-status.st-error { color: var(--ink-50); }',
        # 气泡底色不再单列一条浅色规则:基础规则走 --user-bubble，浅色主题把它映射到
        # --ink-23。主题前缀覆盖会抬高权重并挡掉跨会话气泡变体，不能重新加回。
        'html[data-theme="light"] .hs-page .hs-tk { background: var(--ink-30); }',
        'html[data-theme="light"] .hs-page .hs-tk.on { background: var(--ink-60); }',
    )
    for rule in expected:
        assert rule in CSS
    # 气泡:基础规则走角色变量；浅色主题再映射到 ink-23，深色可独立微调。
    base = CSS[CSS.index(".hs-page .hs-bubble {"):]
    base = base[:base.index("}")]
    assert "--bubble-bg: var(--user-bubble)" in base
    dark = CSS[:CSS.index('html[data-theme="light"] {')]
    assert "--user-bubble: #3b3b3b;" in dark
    light = CSS[CSS.index('html[data-theme="light"] {'):]
    light = light[:light.index("\n}")]
    assert "--ink-23: #f4f4f4;" in light
    assert "--user-bubble: var(--ink-23);" in light
