"""“安排”入口、页面边界与手机布局的结构回归。"""
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[2]
INDEX = (ROOT / "frontend/index.html").read_text(encoding="utf-8")
NAV = (ROOT / "frontend/app-rail-nav.js").read_text(encoding="utf-8")
TOP = (ROOT / "frontend/app-rail-topnav.js").read_text(encoding="utf-8")
PAGE = (ROOT / "frontend/app-panel-arrangements.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
API = (ROOT / "desktron/lib/arrangement/api.py").read_text(encoding="utf-8")
SCHEDULER = (ROOT / "desktron/lib/arrangement/scheduler.py").read_text(encoding="utf-8")
BUNDLE = (ROOT / "desktron/lib/frontend_bundle.py").read_text(encoding="utf-8")
BOOT = (ROOT / "desktron/lib/ui_boot.py").read_text(encoding="utf-8")
PEERS = (ROOT / "desktron/lib/larkbot/peers.py").read_text(encoding="utf-8")


def test_sidebar_is_arrangements_not_workflow_task_aggregation():
    assert 'id="fx-tasks"' in INDEX
    assert '<span data-desk-key="nav.arrangements">安排</span></button>' in INDEX
    assert 'id="arrangement-cnt"' not in INDEX
    assert '$("#arrangement-cnt")' not in PAGE
    assert 'id="task-cnt"' not in INDEX
    assert "openArrangementsPage();" in NAV
    assert '_resourceIsOpen("arrangements")' in NAV


def test_navigation_replays_arrangements_and_keeps_legacy_tasks_internal():
    assert 'e.kind === "arrangements"' in TOP
    assert "openArrangementsPage()" in TOP
    assert 'e.kind === "tasks"' in TOP  # 老历史仍可回放，不再是侧栏入口
    assert 'entry.kind === "arrangements"' in TOP


def test_page_unifies_user_schedules_and_workshop_system_agents():
    assert 'api("/workbench/arrangements"' in PAGE
    assert 'api("/workbench/lark-bot/agents"' in PAGE
    assert 'api("/workbench/agents/status"' in PAGE
    assert 'row_kind: "schedule"' in PAGE
    assert '_arrSysCards(_arrCache.system || {})' in PAGE
    assert "matchingSystem.map(_arrSysCard)" in PAGE
    assert "_arrSysRenderResource" in PAGE
    assert 'c.kind === "memory"' in PAGE
    assert 'c.kind === "lark"' not in PAGE
    assert 'backLabel: "系统安排"' in PAGE
    assert '<span class="agp-sec-title">系统安排</span>' not in PAGE
    assert '<div class="arr-list-head"><b>随 Desk 运行</b>' in PAGE
    assert '<span class="agp-sec-title">系统 Agents</span>' not in PAGE
    assert 'row_kind: "system"' not in PAGE
    assert "被 @ 或话题消息" not in PAGE
    assert "新建安排" in PAGE
    assert "data-new" in PAGE


def test_system_arrangement_detail_keeps_the_home_page_frame():
    assert 'body.className = "md wf-page arrangements-page";' in PAGE
    assert 'bodyClass: "md wf-page arrangements-page arr-sys-resource-page"' in PAGE
    assert 'bodyClass: "md arrangements-page agents-page agp-resource-page"' not in PAGE
    assert ".arrangements-page.agp-resource-page { max-width: 680px !important; }" not in CSS


def test_search_waits_for_ime_composition():
    assert "oncompositionstart" in PAGE
    assert "oncompositionend" in PAGE
    assert "!e.isComposing" in PAGE


def test_user_agent_avatar_is_used_in_arrangement_rows():
    assert "avatars[key] = a.avatar || \"\"" in PAGE
    assert "agent_avatar: avatars" in PAGE
    assert "function _arrIconHtml(x)" in PAGE
    assert "const avatar = x.agent_avatar" in PAGE
    assert "has-avatar" in PAGE
    assert ".arr-icon img" in CSS
    assert ".arr-icon.has-avatar svg { display: none; }" in CSS


def test_existing_schedule_form_is_three_things_and_save_is_not_gated_by_a_test_run():
    """参照 Claude 桌面版 scheduled task 收敛(2026-09-05 用户反馈:要填的太多、保存是灰的不知为何)。
    做什么、谁来做、多久一次;试运行是次要按钮,结果就地显示,不再解锁保存。"""
    form = PAGE[PAGE.index("function _arrEditSheet("):PAGE.index("async function _arrWaitRun(")]
    assert '<button class="arr-primary" type="submit">保存</button>' in form
    assert "保存前先试一次" not in form
    assert "arr-fixed" not in form                                            # 不可改的假字段不再占一行
    assert "先试一次，确认 Agent、目录与权限都符合预期" not in form
    assert 'class="arr-compose-foot"' in form and 'name="agent"' in form      # 执行者内嵌在指令框脚条
    assert '<label for="arr-kind">频率</label>' in form
    assert 'const editing = !!item;' in form
    assert 'const trig = (item && item.trigger) || { kind: "manual" };' in form
    assert '<div class="arr-test" hidden>' in form                            # 结果卡默认收起
    assert ">试运行</button>" in form and "/workbench/arrangements/test" in form
    assert "结果回到安排页，不会自动外发。" in form
    # 保存前仍要有指令,但由提交时校验,不靠灰按钮让人猜
    assert form.count('_deskNotice("先写清楚每次要做什么")') == 2


def test_schedule_form_creates_and_configures_arrangements():
    form = PAGE[PAGE.index("function _arrEditSheet("):PAGE.index("async function _arrWaitRun(")]
    assert "为此安排创建 Agent" not in PAGE
    assert 'name="agent_name"' not in form
    assert "new_agent" not in form
    assert 'editing ? "配置安排" : "新建安排"' in form
    assert 'editing ? "PUT" : "POST"' in form
    assert '先在代理页创建一个 Agent' not in form
    assert 'const defaultBackend = _ARR_ENVIRONMENTS[_arrSection]?.backend || _arrCache.backend || "desktop"' in form
    assert ': `${defaultBackend}::${_ARR_DEFAULT_AGENT}`' in form
    assert '_arrEditSheet(item)' in PAGE
    assert '_arrEditSheet()' in PAGE


def test_new_schedule_has_desk_assisted_and_manual_entries():
    assert 'label: "用 Desk 创建"' in PAGE
    assert 'label: "手动设置"' in PAGE
    assert 'newHtmlSessionWithPrompt({ domain: DESKTOP }, `${_ARR_CREATE_PROMPT}' in PAGE
    assert 'task: true' not in PAGE[PAGE.index("async function _arrCreateWithDesk"):
                                   PAGE.index("function _arrAgentOptions")]
    assert 'make-arrangement skill' in PAGE


def test_new_schedule_offers_existing_bots_alongside_desk_and_agents():
    assert 'const _ARR_DEFAULT_AGENT = "_desk"' in PAGE
    assert "Desktron · ${escapeHtml(backend)}" in PAGE
    assert '<optgroup label="Desk（默认）">' in PAGE
    assert '<optgroup label="我的 Agent（可选）">' in PAGE
    assert '<optgroup label="已有飞书 Bot">' in PAGE
    assert '.filter((a) => a.kind === "agent")' in PAGE
    assert "被 @ 或话题消息" not in PAGE
    assert 'DEFAULT_AGENT_ID = "_desk"' in SCHEDULER
    assert "return bool(agent)" in API


def test_runtime_actions_are_wired_without_popup_windows():
    for action in ('/run${at}', '/toggle${at}', "_arrResultSheet", 'method: "DELETE"'):
        assert action in PAGE
    assert "window.open" not in PAGE
    assert 'aria-label="关闭"' in PAGE
    assert 'e.key === "Escape"' in PAGE


def test_mobile_has_twenty_pixel_visual_gutter_and_bottom_sheet():
    assert "margin-left: -8px; margin-right: 6px" in CSS
    assert "得到物理上左右各 20px" in CSS
    assert ".arr-freq { flex-wrap: wrap; }" in CSS
    assert "border-radius: 16px 16px 0 0" in CSS


def test_editor_sheet_uses_single_physical_hairline_for_all_colored_edges():
    assert ".arr-sheet {" in CSS
    assert "border: var(--menu-hairline) solid var(--lift-18)" in CSS
    assert "border: var(--menu-hairline) solid var(--border-base)" in CSS
    assert ".arr-test.testing { border-width: var(--menu-hairline)" in CSS
    assert ".arr-test.passed { border-width: var(--menu-hairline)" in CSS
    assert ".arr-test.failed { border-width: var(--menu-hairline)" in CSS
    assert ".arr-run-error { margin-top: 15px; padding: 11px 12px; border: var(--menu-hairline)" in CSS
    assert "border: var(--menu-hairline) solid var(--border-base); border-radius: 10px; background: var(--ink-10)" in CSS
    assert ".arr-field select { appearance: none; -webkit-appearance: none" in CSS
    assert "box-shadow: none" in CSS


def test_custom_repeat_form_serialization_and_summary():
    script = PAGE + r'''
const assert = require("node:assert/strict");
const values = {kind: "custom", repeat_unit: "week", repeat_every: "2", time: "10:35",
  start_date: "2030-01-02", ending: "date", end_date: "2030-12-31"};
const form = {elements: Object.fromEntries(Object.entries(values).map(([k,v]) => [k,{value:v}])),
  querySelectorAll: () => [{value:"4"}, {value:"0"}]};
const trigger = _arrFormTrigger(form);
assert.deepEqual(trigger, {kind:"custom", unit:"week", every:2, time:"10:35",
  start_date:"2030-01-02", end_date:"2030-12-31", weekdays:[0,4]});
assert.match(_arrTrigger(trigger), /每 2 周.*周一.*周五.*10:35/);
assert.match(_arrTrigger(trigger), /2030-12-31（含当天）/);
form.elements.repeat_unit.value = "month";
form.elements.ending.value = "never";
const monthly = _arrFormTrigger(form);
assert.equal(monthly.weekdays, undefined);
assert.equal(monthly.end_date, undefined);
assert.match(_arrTrigger(monthly), /永不截止/);
assert.match(_arrTrigger(monthly), /月末/);
form.elements.kind.value = "manual";
assert.deepEqual(_arrFormTrigger(form), {kind:"manual"});
'''
    subprocess.run(["node"], input=script, check=True, capture_output=True, text=True, encoding="utf-8")


def test_custom_repeat_hidden_fields_do_not_block_other_frequencies():
    assert 'repeat.hidden = !custom; repeat.disabled = !custom;' in PAGE
    assert 'el.disabled = !custom || !weekly' in PAGE
    assert '请至少选择一个星期' in PAGE
    assert 'until.disabled = !custom || until.hidden' in PAGE
    assert 'until.min = form.elements.start_date.value' in PAGE
    assert 'form.elements.repeat_unit.value = trig.unit || "week"' in PAGE
    assert 'aria-live="polite"' in PAGE
    assert 'repeat(7, minmax(0, 1fr))' in CSS
    assert 'input:checked + span { background: var(--ink-85); color: var(--ink-10); }' in CSS


def test_frontend_bundle_and_backend_router_include_arrangements():
    assert '"app-panel-arrangements.js"' in BUNDLE
    assert "arrangement_api" in BOOT
    assert 'ALLOW_PATHS = {"/workbench/arrangements"}' in PEERS
    assert '"/workbench/arrangements/"' in PEERS
