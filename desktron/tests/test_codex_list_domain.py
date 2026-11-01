"""codex 家族的会话列在哪个工作流下:cwd 与 meta 谁说了算。

2026-08-11 实见:在「Desk 迭代」下开的一段 codex GUI 会话,侧栏把它列进了「聊天」,
会话视图的任务栏跟着报「查不到归属」。而后端两处存储里 domain 都是 desk-iter——
适配器的 sessions.json 是,term-meta.json 也是。错的是列举那一步:它让 cwd 推断
无条件盖过 meta,而 GUI 建的会话 cwd 恒为工作台根(那儿推不出任何工作流,只会得到
_desktop),显式归属就这么被抹掉了。

TUI 那条路必须保持原样:它在工作流目录里起,cwd 就是最可靠的来源——会话被移动过、
meta 落后于实际位置时,信 cwd 才对。所以判据是「cwd 指得出工作流时才盖过 meta」。

这里把 _domain_for_cwd 换成假的,只测 _codex_list_domain 自己的取舍。真去解析路径的话
就得依赖进程级的 REPO_ROOT,而那个值由「哪个测试文件先导入 deskcfg」决定——
单跑全过、同跑全崩(本文件第一版就栽在这上面)。
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
if LIB not in sys.path:
    sys.path.insert(0, LIB)
if "deskcfg" not in sys.modules:
    sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]

import term  # noqa: E402

WS = "/ws"
BY_CWD = {                       # 假的路径→工作流映射,替掉真去解析 REPO_ROOT 的那个
    WS: "_desktop",              # 工作台根:推不出任何工作流
    WS + "/domains/desk-iter": "desk-iter",
    WS + "/domains/incident": "incident",
}


class ListDomain(unittest.TestCase):
    def setUp(self):
        self.p = mock.patch.object(term, "_domain_for_cwd", lambda cwd: BY_CWD.get(cwd, ""))
        self.p.start()
        self.addCleanup(self.p.stop)

    def test_GUI_会话信_meta(self):
        """cwd 是工作台根:推不出工作流,不能据此把显式归属抹成聊天。"""
        self.assertEqual(term._codex_list_domain("desk-iter", WS, "desk-iter"), "desk-iter")

    def test_TUI_会话信_cwd(self):
        """在工作流目录里起的,cwd 就是归属;meta 没有也能列对。"""
        self.assertEqual(term._codex_list_domain("", WS + "/domains/desk-iter", ""), "desk-iter")

    def test_cwd_指向别的工作流时以_cwd_为准(self):
        """会话被移动过、meta 落后于实际位置——这时 cwd 才是实情。"""
        self.assertEqual(term._codex_list_domain("desk-iter", WS + "/domains/incident", ""),
                         "incident")

    def test_两边都指不出来才算聊天(self):
        self.assertEqual(term._codex_list_domain("", WS, ""), "_desktop")

    def test_隔离子区与_dw_仍然直接信_meta(self):
        """这两类只存在于 Desk 的元数据里,rollout 的 cwd 指向源仓,按 cwd 归会让它们消失。"""
        self.assertEqual(term._codex_list_domain("proj:sub", WS + "/domains/desk-iter", ""),
                         "proj:sub")
        self.assertEqual(term._codex_list_domain("dw-1234", WS + "/domains/desk-iter", ""),
                         "dw-1234")


if __name__ == "__main__":
    unittest.main()
