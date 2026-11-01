"""会话上下文托盘的结构与产品契约回归。"""
from hashlib import sha256
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "frontend/app-chat-page.js").read_text(encoding="utf-8")
BAR = (ROOT / "frontend/app-chat-tasksbar.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
ICON_LICENSE = (ROOT / "frontend/vendor/ICONS-LICENSE.txt").read_text(encoding="utf-8")
SETTINGS = (ROOT / "frontend/app-settings-core.js").read_text(encoding="utf-8")
NAV = (ROOT / "frontend/app-rail-nav.js").read_text(encoding="utf-8")
CREATE = (ROOT / ".claude/skills/make-arrangement/scripts/create_arrangement.py").read_text(encoding="utf-8")


def test_rail_is_attached_without_changing_composer_box():
    assert 'class="hs-context hs-col"' in PAGE
    assert 'class="hs-box hs-col"' in PAGE
    assert "height: 34px" in CSS
    assert "margin: 0 16px -1px" in CSS
    assert "border-radius: 20px 20px 0 0" in CSS
    desktop = CSS[CSS.index("@container hs-chat (min-width: 901px)"):CSS.index("@container hs-chat (max-width: 900px)")]
    assert ".hs-page .hs-context-rail { height: 31px; border-radius: 17px 17px 0 0; }" in desktop
    assert ".hs-page .hs-context-item { height: 25px; }" in desktop
    assert ".hs-page .hs-box { --hs-box-bg: var(--ink-16);" in CSS
    assert "border-radius: 20px; padding: 16px 18px 10px" in CSS


def test_rail_typography_is_quiet_and_only_error_status_keeps_its_red_glyph():
    assert "font-weight: 400" in CSS
    assert "font-weight: 350" in CSS
    assert 'mask: url("/icons/status.png") center / contain no-repeat' in CSS
    assert 'mask-image: url("/icons/status-alert.png")' in CSS
    assert 'mask-image: url("/icons/status-login.png")' in CSS
    assert 'class="hs-context-status-signal"' not in PAGE
    assert 'class="hs-context-status-alert"' not in PAGE
    assert 'class="hs-context-status-login-icon"' not in PAGE
    assert PAGE.count('class="hs-context-icon hs-context-status-icon" aria-hidden="true"></span>') == 1
    assert PAGE.count('class="hs-context-status-icon"></span>') == 1
    assert "--hs-status-accent: var(--ink-66)" in CSS
    assert ".hs-context-status.error { --hs-status-accent: #ef6464; }" in CSS
    assert "#45c878" not in CSS
    assert "#e3b84f" not in CSS
    assert "p.stage === \"auth\" ? \"login\"" in BAR


def test_context_rail_uses_original_png_masks_and_keeps_historical_attribution():
    assert "Desktron 原创圆润界面图标" in ICON_LICENSE
    assert "当前会话任务使用V4方形外框" in ICON_LICENSE
    assert 'class="hs-context-icon hs-context-task-icon" aria-hidden="true"></span>' in PAGE
    assert 'class="hs-context-icon hs-context-arrangements-icon" aria-hidden="true"></span>' in PAGE
    assert 'mask: url("/icons/task.png") center / contain no-repeat' in CSS
    assert 'mask: url("/icons/current-session-task.png") center / contain no-repeat' in CSS
    assert 'mask: url("/icons/arrangement.png") center / contain no-repeat' in CSS
    for name in ("task.png", "current-session-task.png", "arrangement.png", "status.png", "status-alert.png", "status-login.png"):
        assert (ROOT / "frontend/icons" / name).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert sha256((ROOT / "frontend/icons/task.png").read_bytes()).hexdigest() == (
        "3018c5bcc2165c73c1e1fe25d896e8424eded929a81963179691d78d660527e9"
    )
    assert sha256((ROOT / "frontend/icons/current-session-task.png").read_bytes()).hexdigest() == (
        "13994f7d2322dddce020f4129e4085f7c155b93d76a0451c20d771422d0f12fb"
    )
    for source in ("check_9068247", "checklist_9741134", "calendar_9308104", "status_8917343", "warning_9068127", "key_8555135"):
        assert source in ICON_LICENSE
    assert ICON_LICENSE.count("作者 Graphics Plazza") >= 6


def test_status_popover_has_a_compact_visual_summary():
    assert 'class="hs-context-status-summary"' in PAGE
    assert 'class="hs-context-status-popover-icon"' in PAGE
    assert 'class="hs-context-status-action-icon"' in PAGE
    assert 'class="hs-context-status-action-label"' in PAGE
    assert "grid-template-columns: 30px minmax(0,1fr)" in CSS
    assert "width: min(306px, calc(100vw - 36px))" in CSS
    assert '.hs-context-status-action[data-kind="engine-update"]' in CSS
    assert "border-radius: 999px" in CSS


def test_reconcile_action_uses_a_small_reviewed_direction_arrow():
    selector = '.hs-page .hs-context-status-action[data-kind="shepherd-reconcile"] .hs-context-status-action-icon {'
    rule = CSS.split(selector, 1)[1].split('}', 1)[0]
    assert 'width: 12px; height: 12px; flex-basis: 12px;' in rule
    assert '-webkit-mask-image: url("/icons/reviewed-remaining/_BR_ICON_FWD.svg")' in rule
    assert 'mask-image: url("/icons/reviewed-remaining/_BR_ICON_FWD.svg")' in rule
    assert (ROOT / 'frontend/icons/reviewed-remaining/_BR_ICON_FWD.svg').is_file()
    assert '.hs-page .hs-context-status-action[data-kind="engine-update"]' in CSS


def test_rail_columns_do_not_change_when_status_copy_grows():
    assert "@container hs-chat (max-width: 900px)" in CSS
    assert "grid-template-columns: minmax(0, 5fr) minmax(0, 2fr) minmax(0, 2fr)" in CSS
    assert ".hs-context-item { width: 100%; min-width: 0" in CSS
    assert "overflow: hidden; padding: 0" in CSS
    assert "grid-template-columns: minmax(0, 1fr) auto auto" not in CSS
    assert ".hs-page .hs-context-meta { display: none; }" in CSS
    narrow = CSS[CSS.index("@container hs-chat (max-width: 900px)"):CSS.index("/* 子代理:")]
    assert "overflow: hidden; text-overflow: ellipsis; text-align: left" in narrow
    assert ".hs-page .hs-arrangements { position: static; min-width: 0; }" in narrow
    assert ".hs-page .hs-arrangements-popover { left: 0; right: 0; width: auto; }" in narrow
    assert "overflow-x: auto; scrollbar-width: none" not in CSS[CSS.index(".hs-page .hs-context-rail"):CSS.index(".hs-page .hs-context-item")]
    assert "background: #202020" in CSS
    assert 'background: #e9e9e9' in CSS


def test_task_popover_separates_session_tasks_from_the_remaining_backlog():
    assert 'class="hs-current-task-section"' in PAGE
    assert 'class="hs-current-tasks-title"' in PAGE
    assert '"ctx.currentTasks"' in PAGE
    assert 'class="hs-current-tasks"' in PAGE
    assert 'class="hs-tasks-sum"' in PAGE
    assert 'class="hs-tasks-sum-icon"' in PAGE
    assert 'placeholder="输入回车创建新任务"' in PAGE
    assert "hsTaskBelongsToSession(t, v.sid)" in BAR
    assert "const sessionCurrent = items.filter" in BAR
    assert "const currentItems = sessionCurrent" in BAR
    assert "const backlogItems = items.filter" in BAR
    assert 'DeskI18n.bind(box.querySelector(".hs-tasks-sum-title"), "ctx.allTodos", { count: backlogItems.length })' in BAR
    assert "for (const task of currentItems)" in BAR
    assert "for (const t of backlogItems)" in BAR
    assert 'row.innerHTML = \'<span class="hs-task-id"></span><span class="hs-task-t"></span>\'' in BAR
    assert 'const taskID = String(t.t || "").trim()' in BAR
    assert 'id.classList.toggle("empty", !taskID)' in BAR
    assert ".hs-page .hs-task-id { flex: 0 0 4ch; width: 4ch; overflow: hidden; color: var(--ink-80)" in CSS
    assert "font-family: var(--mono-code); font-weight: 550" in CSS
    assert ".hs-page .hs-task-id.empty { visibility: hidden; }" in CSS
    assert '`${task.t || DeskI18n.t("task.noId")} · ${DeskI18n.t(task.state === "doing" ? "task.doing" : "task.todo")}`' in BAR
    assert 'String(task.line || "").slice(0, 4)' not in BAR
    assert 'hs-task-current-meta' not in PAGE
    assert 'class="hs-current-tasks-title-icon"' in PAGE
    assert 'class="hs-current-tasks-title-icon" aria-hidden="true"></span>' in PAGE
    assert 'class="hs-tasks-sum-icon" aria-hidden="true">' in PAGE
    assert "HS_ICONS.check" in PAGE
    assert '.hs-current-tasks-title-icon::before' in CSS
    assert '.hs-tasks-sum-icon svg' in CSS
    assert '.hs-tasks-sum-icon::before' not in CSS
    assert ".hs-current-task-title { min-width: 0; font-size: 14px; font-weight: 510" in CSS
    assert ".hs-tasks-popover { width: min(330px, calc(100vw - 36px)); display: flex; flex-direction: column" in CSS
    assert "gap: 8px; padding: 14px; border-color: var(--lift-12); border-radius: 14px" in CSS
    assert ".hs-current-task-section { padding-bottom: 8px; border-bottom: 1px solid var(--lift-08); }" in CSS
    assert ".hs-current-tasks-title { min-height: 34px; display: flex; align-items: center" in CSS
    assert "color: var(--ink-70); font-size: 14.5px; font-weight: 550" in CSS
    assert ".hs-current-tasks { display: flex; flex-direction: column; gap: 0" in CSS
    assert "border: 0; border-radius: 0; background: transparent; padding: 11px 9px" in CSS
    assert ".hs-current-task + .hs-current-task" not in CSS
    assert ".hs-tasks-sum { min-height: 30px; display: flex; align-items: center" in CSS
    tasks_sum = CSS[CSS.index(".hs-page .hs-tasks-sum {"):CSS.index(".hs-page .hs-tasks-sum-icon {")]
    assert "color: var(--ink-70); font-size: 14.5px; font-weight: 550" in tasks_sum
    assert "max-height: 190px; overflow-y: auto" in CSS
    assert "max-height: 170px; overflow-y: auto" in CSS
    assert ".hs-current-tasks, .hs-page .hs-tasks-list { box-sizing: border-box; margin-right: -10px" in CSS
    assert "padding-right: 10px; }" in CSS
    assert ".hs-current-tasks, .hs-tasks-list { scrollbar-width: none; }" in CSS
    assert ".hs-current-tasks::-webkit-scrollbar, .hs-tasks-list::-webkit-scrollbar { display: none; }" in CSS
    assert "scrollbar-color: var(--ink-32) transparent" not in CSS
    assert 'list.dispatchEvent(new CustomEvent("desk-scrollbar-refresh", { bubbles: true }))' in BAR
    assert 'v.el.querySelector(".hs-context")?.contains(thumb._key)' in BAR
    assert "wfOpenTaskSheet(task, dom)" in BAR


def test_arrangements_are_scoped_to_the_current_session():
    assert "item.session_id === v.sid" in BAR
    assert "session_id: (item && item.session_id) || options.sessionID" in (ROOT / "frontend/app-panel-arrangements.js").read_text(encoding="utf-8")
    assert 'os.environ.get("DESKTRON_SESSION_ID"' in CREATE
    assert "hsArrangementsManage(v)" in PAGE
    assert 'class="hs-arrangements-empty"' in BAR
    assert 'class="hs-arrangements-empty-icon" aria-hidden="true"></span>' in BAR
    assert '"ctx.arrEmptyTitle"' in BAR and '"ctx.arrEmptyHint"' in BAR
    assert ".hs-arrangements-popover { left: -10px; width: min(306px" in CSS
    assert ".hs-arrangements-empty { display: grid; grid-template-columns: 34px minmax(0,1fr)" in CSS
    assert '.hs-arrangements-empty-icon::before' in CSS
    assert ".hs-context-manage { width: 100%; min-height: 34px; display: inline-flex" in CSS
    assert ".hs-context-manage svg { display: block; width: 14px; height: 14px; transform: translateY(.7px); }" in CSS
    narrow = CSS.split("@container hs-chat (max-width: 900px)", 1)[1]
    assert ".hs-context-manage svg { transform: none; }" in narrow


def test_system_arrangements_are_reachable_from_settings_not_the_rail_nav():
    assert '{ id: "system-arrangements", title: "系统安排" }' in SETTINGS
    assert "renderSystemArrangementsSettings" in SETTINGS
    assert 'api("/workbench/agents/status"' in SETTINGS
    assert '["system", "系统安排"]' not in NAV


def test_status_details_do_not_repeat_the_heading_and_clear_between_updates():
    from test_frontend_recall import RecallUI
    from frontend_i18n_js import NODE_PRELUDE
    RecallUI().run_js([('app-chat-core.js', 'hsEnvFull'),
                       ('app-chat-tasksbar.js', 'hsContextStatusSync')], NODE_PRELUDE + r"""
const assert=require('node:assert/strict');
const node=()=>({attrs:{},getAttribute(k){return this.attrs[k]||'';},setAttribute(k,v){this.attrs[k]=v;},
  removeAttribute(k){delete this.attrs[k];}});
const nodes={};const host={querySelector:s=>nodes[s]||(nodes[s]=node())};
nodes['.hs-context-status-action']={...node(),dataset:{},querySelector:s=>nodes[s]||(nodes[s]=node())};
const v={engine:'pi',el:{querySelector:()=>host}};
const title=()=>nodes['.hs-context-status-title'].textContent;
const detail=()=>nodes['.hs-context-status-detail'];
v.envCurrent={state:'error',label:'错误',details:['错误','错误']};
hsContextStatusSync(v);assert.equal(title(),'错误');assert.equal(detail().hidden,true);
v.envCurrent={state:'error',label:'完整错误…',details:['完整错误内容']};
hsContextStatusSync(v);assert.equal(title(),'完整错误内容');assert.equal(detail().hidden,true);
v.envCurrent={state:'error',label:'恢复失败',details:['恢复失败','HTTP 404','HTTP 404']};
hsContextStatusSync(v);assert.equal(title(),'恢复失败');assert.equal(detail().textContent,'HTTP 404');assert.equal(detail().hidden,false);
v.envCurrent={state:'warning',label:'单句警告'};
hsContextStatusSync(v);assert.equal(detail().hidden,true);assert.equal(detail().textContent,'');
v.envCurrent={stage:'auth',label:'需要登录',action:{label:'登录'}};
hsContextStatusSync(v);assert.equal(host.className,'hs-context-status login');assert.equal(nodes['.hs-context-status-action'].hidden,false);
assert.equal(nodes['.hs-context-status-action-label'].textContent,'登录');
v.envCurrent=null;hsContextStatusSync(v);assert.equal(title(),'引擎已就绪');assert.equal(detail().textContent,'pi 可正常使用');
""")
