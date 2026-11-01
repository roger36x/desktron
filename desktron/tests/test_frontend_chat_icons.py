"""聊天输入区仅模式与权限使用指定图标，信息卡标题保持纯文字。"""
from pathlib import Path
import hashlib
import struct
import subprocess


ROOT = Path(__file__).resolve().parents[2]
PAGE = (ROOT / "frontend/app-chat-page.js").read_text(encoding="utf-8")
SITUATION = (ROOT / "frontend/app-chat-situation.js").read_text(encoding="utf-8")
SIDEBAR = (ROOT / "frontend/app-panel-sidebar.js").read_text(encoding="utf-8")
TERMS = (ROOT / "frontend/app-panel-terms.js").read_text(encoding="utf-8")
CSS = (ROOT / "frontend/style.css").read_text(encoding="utf-8")
LICENSE = (ROOT / "frontend/vendor/ICONS-LICENSE.txt").read_text(encoding="utf-8")


def test_chat_composer_uses_original_mode_and_permission_icons():
    assert "Desktron 原创圆润界面图标" in LICENSE
    assert "Flaticon条目仅记录替换前素材" in LICENSE
    icons = {
        "composer-mode.png": "random_7313741",
        "composer-lock.png": "padlock_747305",
    }
    for filename, source in icons.items():
        assert (ROOT / "frontend/icons" / filename).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert f'/icons/{filename}' in CSS
        assert source in LICENSE
    assert 'hs-composer-icon is-lock' in PAGE


def test_permission_and_collaboration_icons_follow_engine_switches():
    script = r'''
const fs = require("fs"), assert = require("node:assert/strict");
eval(fs.readFileSync(process.argv[1], "utf8"));
let className = "hs-composer-icon is-lock", writes = 0;
const icon = { get className() { return className; },
  set className(value) { className = value; writes++; } };
const text = { textContent: "" };
const mode = { hidden: false, querySelector(s) {
  return s === ".hs-composer-icon" ? icon : text;
} };
const sandboxText = { textContent: "" };
const sandboxIcon = { className: "hs-composer-icon is-lock" };
const sandbox = { hidden: true, querySelector(s) {
  return s === ".hs-sandbox-t" ? sandboxText : sandboxIcon;
} };
const v = { engine: "claude", catalog: {}, sessionOpts: {},
  el: { querySelector(s) { return s === ".hs-mode" ? mode : s === ".hs-sandbox" ? sandbox : null; } } };
for (const [engine, opts, expected] of [
  ["claude", {mode: "bypassPermissions"}, "is-lock"],
  ["claude", {mode: "auto"}, "is-lock"],
  ["codex", {collaboration_mode: "plan"}, "is-mode"],
  ["codex", {collaboration_mode: "default"}, "is-mode"],
  ["pi", {mode: "bypassPermissions"}, "is-lock"],
  ["claude", {mode: "plan"}, "is-lock"],
]) {
  v.engine = engine; v.sessionOpts = opts;
  hsOptsPaint(v);
  assert.equal(icon.className, "hs-composer-icon " + expected, engine);
  assert.equal(sandboxIcon.className, "hs-composer-icon is-lock");
  writes = 0; hsOptsPaint(v);
  assert.equal(writes, 0, "相同选项不应重复写入图标");
}
// 后端声明优先于引擎静态回退，迟到的 catalog 也应刷新图标。
v.catalog = {ui: {modeKey: "collaboration_mode"}};
hsOptsPaint(v);
assert.equal(icon.className, "hs-composer-icon is-mode");
'''
    subprocess.run(["node", "-e", script, str(ROOT / "frontend/app-chat-opts.js")], check=True)


def test_chat_composer_keeps_original_attachment_send_and_text_model():
    assert "HS_ICONS.clip" in PAGE
    assert 'polyline points="6 12 12 6 18 12"' in PAGE
    assert "HS_ICONS.bolt" not in PAGE
    assert "HS_ICONS.lock" not in PAGE
    model = '<span class="hs-model"><span class="hs-m1">…</span><span class="hs-m2"></span></span>'
    assert model in PAGE
    assert '<span class="hs-m2"></span>\' + HS_ICONS.chev2' not in PAGE


def test_context_menus_use_requested_icons_without_session_leading_divider():
    icons = {
        "menu-rename.png": "draw_12377828",
        "menu-delete.png": "delete_3405244",
        "menu-move.png": "input-icon.png",
    }
    for filename, source in icons.items():
        assert (ROOT / "frontend/icons" / filename).read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
        assert f'/icons/{filename}' in CSS
        assert source in LICENSE
    assert 'rename: \'<span class="menu-icon-mask is-rename"' in SIDEBAR
    assert 'remove: \'<span class="menu-icon-mask is-delete"' in SIDEBAR
    assert 'RP_RENAME_ICO = \'<span class="menu-icon-mask is-rename"' in TERMS
    assert 'RP_TRASH_ICO = \'<span class="menu-icon-mask is-delete"' in TERMS
    assert 'RP_MOVE_ICO = \'<span class="menu-icon-mask is-move"' in TERMS
    assert '{ label: "重命名", sep: true, ico:' not in TERMS


def test_move_menu_uses_original_v4_square_and_half_length_arrow():
    import json
    data = (ROOT / "frontend/icons/menu-move.png").read_bytes()
    assert struct.unpack(">II", data[16:24]) == (512, 512)
    manifest = json.loads((ROOT / "frontend/icons/supplemental-original-manifest.json").read_text(encoding="utf-8"))
    icon = next(i for i in manifest['icons'] if i['name'] == 'move')
    assert hashlib.sha256(data).hexdigest() == icon['png_sha256']
    svg = (ROOT / "frontend/icons/menu-move.svg").read_text(encoding="utf-8")
    assert 'M3.5 16H14.5m-3.5-3.5 3.5 3.5-3.5 3.5' in svg
    assert 'A3.5 3.5 0 0 1 8.5 5h15' in svg
    assert 'Desktron 补充自绘图标' in LICENSE
    assert '历史移动到素材' in LICENSE


def test_visible_arrows_use_original_asset_with_historical_attribution():
    assert "right-arrow" in LICENSE
    assert "Desktron 原创圆润界面图标" in LICENSE
    icon = ROOT / "frontend/icons/right-arrow.png"
    assert icon.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    assert '/icons/right-arrow.png' in CSS
    assert 'next_130884' in LICENSE


def test_information_card_section_titles_are_text_only():
    assert "hs-sit-title-icon" not in SITUATION + CSS
    for title in ("待审批", "项目", "制品", "回顾"):
        assert f"<h2>{title}</h2>" in SITUATION
    assert '<span class="hs-sit-change-title">变更</span>' in SITUATION


def test_sidebar_workflow_uses_original_curved_four_point_star():
    assert 'M12 2c.6 5.5 4.5 9.4 10 10' in SIDEBAR
    assert "sidebar-workflow-icon" not in SIDEBAR + CSS
