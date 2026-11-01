"""claude 引擎的自动会话名(workspace-9c 这种)不该顶掉按内容派生的标题。

2026-08-27 用户实见:久睡的会话一开工,侧栏标题变成 workspace-9c,而窗口标题还是
自动识别的那份。引擎给每个进程往 ~/.claude/sessions/<pid>.json 注册一个
「目录名-短哈希」的名,Desk 把它当用户 /rename 收下。泄露有两条路:
_claude_pick_name 的过滤在 wd 未知(按 id 兜底找到的记录)时整个跳过;
engines/stores 的 list_by_cwd 生用注册表名、从来没有过滤——
/workbench/sessions 里实见 title="workspace-42"。

修法:共享判据 _claude_auto_named(nm, wd),锚定目录名(wd 未知退到全部工作区
目录名),不用裸的「-两位十六进制结尾」——用户真给会话起名 release-2a 是合法的。
"""
import os
import sys
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
sys.argv = ["test", "/tmp/tcanf-frontend", "0", "0", "/tmp/tcanf-repo"]   # deskcfg 读 argv
import term  # noqa: E402


class Predicate(unittest.TestCase):
    def test_锚全部工作区目录名而非落盘目录(self):
        """第一版的盲区:名字里那截来自引擎进程自己的 cwd,与 transcript 的落盘目录
        未必是同一个。实见 domains/incident 下的会话叫 workspace-9c(引擎 cwd 是
        工作区根),只锚落盘目录时判据返回 False,自动名照样漏进侧栏。"""
        with mock.patch.object(term, "_workspace_dirs",
                               lambda: ["/h/workspace", "/h/workspace/domains/incident"]):
            self.assertTrue(term._claude_auto_named("workspace-9c", "/h/workspace/domains/incident"),
                            "落盘在 incident、名字来自 workspace,这一类必须认出来")
            self.assertTrue(term._claude_auto_named("incident-b2", "/h/workspace"))
            self.assertFalse(term._claude_auto_named("elsewhere-9c", "/h/workspace"),
                             "不在工作区目录名集合里的前缀,不是我们这儿长出来的")

    def test_用户起的名不误伤(self):
        with mock.patch.object(term, "_workspace_dirs", lambda: ["/a/b/workspace", "/a/b/rogerplayer"]):
            self.assertFalse(term._claude_auto_named("release-2a", "/a/b/workspace"))
            self.assertFalse(term._claude_auto_named("rogerplayer dev src", "/a/b/rogerplayer"))
            self.assertFalse(term._claude_auto_named("desktron refactor", "/a/b/workspace"))

    def test_wd未知时锚全部工作区目录名(self):
        with mock.patch.object(term, "_workspace_dirs", lambda: ["/h/workspace", "/h/incident"]):
            self.assertTrue(term._claude_auto_named("workspace-9c", ""))
            self.assertTrue(term._claude_auto_named("incident-4f", ""))
            self.assertFalse(term._claude_auto_named("release-2a", ""))

    def test_空名不算(self):
        self.assertFalse(term._claude_auto_named("", "/a/workspace"))


class PickName(unittest.TestCase):
    """_claude_pick_name:注册表自动名让位,用户改名保留。"""

    def _pick(self, reg, wd, custom="", title="摘要标题"):
        with mock.patch.object(term, "_claude_named", lambda: {"s1": reg} if reg else {}), \
             mock.patch.object(term, "_claude_custom_title", lambda p: custom):
            return term._claude_pick_name("/tmp/x.jsonl", "s1", wd, title)

    def test_自动名让位给摘要(self):
        with mock.patch.object(term, "_workspace_dirs", lambda: ["/h/workspace"]):
            self.assertEqual(self._pick("workspace-9c", "/h/workspace"), "摘要标题")

    def test_落盘目录与名字来源不同也让位(self):
        """回归:侧栏休眠列表走 _claude_session_by_id,传的是落盘目录(incident),
        而名字来自引擎 cwd(workspace)——2026-08-27 第一版漏的正是这条路。"""
        with mock.patch.object(term, "_workspace_dirs",
                               lambda: ["/h/workspace", "/h/workspace/domains/incident"]):
            self.assertEqual(self._pick("workspace-9c", "/h/workspace/domains/incident"), "摘要标题")

    def test_wd未知也拦得住(self):
        """原来这条路(按 id 兜底找到的记录)整个跳过滤,自动名直接漏出去。"""
        with mock.patch.object(term, "_workspace_dirs", lambda: ["/h/workspace"]):
            self.assertEqual(self._pick("workspace-9c", ""), "摘要标题")

    def test_用户改名仍然最优先于摘要(self):
        with mock.patch.object(term, "_workspace_dirs", lambda: ["/h/workspace"]):
            self.assertEqual(self._pick("我的调查", "/h/workspace"), "我的调查")

    def test_手动自定义标题压过一切(self):
        with mock.patch.object(term, "_workspace_dirs", lambda: ["/h/workspace"]):
            self.assertEqual(self._pick("workspace-9c", "/h/workspace", custom="钉住的名字"), "钉住的名字")


class StoreListByCwd(unittest.TestCase):
    """engines/stores 的 list_by_cwd:原来生用注册表名,workspace-42 直接进 resume 候选。"""

    def test_源码接了判据(self):
        src = open(os.path.join(ROOT, "desktron", "lib", "engines", "stores.py"),
                   encoding="utf-8").read()
        self.assertIn("_claude_auto_named(reg, wd)", src)
        at = src.index("_claude_auto_named(reg, wd)")
        self.assertIn('reg = ""', src[at:at + 200], "命中自动名要清空退回内容派生")


if __name__ == "__main__":
    unittest.main(argv=["test_claude_auto_name_filter"])   # sys.argv 已被 deskcfg 借道
