"""Removed generic application artwork must not be selected or shipped inline."""
import json
from pathlib import Path
import subprocess

import pytest

FRONT = Path(__file__).resolve().parents[2] / "frontend"
REMOVED = {
    "learn": "学习 每日 微课 learn study daily 知识库 知识",
    "alert": "告警 报警 alert",
    "cal": "会议 日程 纪要 meeting calendar minutes",
    "chat": "群 消息 聊天 速览 chat lark digest 飞书",
    "shield": "治理 风险 安全 governance risk guard",
    "task": "审批 任务 跟催 清单 task todo approv",
}


def render(cases):
    source = (FRONT / "app-assets-apps.js").read_text(encoding="utf-8")
    snippet = source[source.index("const _APP_COLORS"):source.index("const _APP_RECENT_MAX")]
    code = (
        "const escapeHtml=s=>String(s);\n" + snippet
        + "\nconsole.log(JSON.stringify("
        + json.dumps(cases, ensure_ascii=False)
        + ".map(args=>_appIconHTML(...args))));"
    )
    return json.loads(subprocess.check_output(["node", "-e", code], text=True, encoding="utf-8"))


@pytest.mark.parametrize("category,keywords", REMOVED.items())
def test_removed_categories_fall_back_for_name_key_and_description(category, keywords):
    source = (FRONT / "app-assets-apps.js").read_text(encoding="utf-8")
    assert f'_appTile("{category}",' not in source
    cases = []
    for keyword in keywords.split():
        cases.extend([
            [keyword, "custom", "sm", "", ""],
            ["示例", keyword, "", "", ""],
            ["示例", "custom", "", keyword, ""],
        ])
    for args, html in zip(cases, render(cases), strict=True):
        assert "<svg" not in html and "<img" not in html
        assert "background-color:" in html
        assert f'>{args[0][0].upper()}</div>' in html


def test_retained_application_artwork_still_matches():
    cases = [
        ["TaskNote", "custom", "", "", ""],
        ["便笺", "custom", "", "", ""],
        ["浏览器", "custom", "", "", ""],
        ["终端", "custom", "", "", ""],
        ["Monaco IDE", "custom", "", "", ""],
        ["Excalidraw", "custom", "", "", ""],
        ["指标", "custom", "", "", ""],
    ]
    assert all('class="ap-ic tile"' in html for html in render(cases))


def test_explicit_application_images_keep_priority():
    cases = [[keyword, "custom", "sm", "", "/icons/custom.png"]
             for keywords in REMOVED.values() for keyword in keywords.split()]
    for html in render(cases):
        assert 'class="ap-ic img sm"' in html
        assert '<img src="/icons/custom.png"' in html
        assert "<svg" not in html
