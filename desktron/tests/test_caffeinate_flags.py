"""ui_server 长驻期间不许让这台 Mac 睡过去(2026-08-25 实见:睡了 26 秒,隧道跟着断)。

那次 caffeinate 的断言是生效的,pmset -g assertions 里查得到 PreventUserIdleSystemSleep 1,
而 pmset -g log 仍然记着 `Entering Sleep state due to 'Idle Sleep' ... Using AC`。
-i 只持有 PreventUserIdleSystemSleep 这一档;2026-08-26 查同一份 assertions,
PreventSystemSleep 是 0——那一档从来没人持有。补 -s 之后两档都在。

这里钉的是启动命令本身。它容易在别的改动里被顺手改回去,而改回去之后不报错、
只是某天夜里睡了一次。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BIN = open(os.path.join(ROOT, "desktron", "bin", "desktron"), encoding="utf-8").read()


def _line():
    """启动 ui_server 的那一行 caffeinate 命令。"""
    m = re.search(r"^\s*exec caffeinate (\S+) python3 .*ui_server\.py.*$", BIN, re.M)
    assert m, "caffeinate 那一行不见了"
    return m.group(1)


class Flags(unittest.TestCase):
    def test_防空闲休眠(self):
        """-i:PreventUserIdleSystemSleep。原来只有它。"""
        self.assertIn("i", _line().lstrip("-"))

    def test_防系统休眠(self):
        """-s:PreventSystemSleep,比 -i 强的那一档,2026-08-26 之前一直空着。"""
        self.assertIn("s", _line().lstrip("-"))

    def test_不锁着屏幕(self):
        """-d 会让显示器一直亮。要的是机器别睡,不是屏幕别关。"""
        self.assertNotIn("d", _line().lstrip("-"))

    def test_caffeinate仍是父进程(self):
        """它替 python3 持有断言,ui_server 退出即一并释放。反过来写(python3 起 caffeinate)
        会留下一个没人收的断言,Desk 关掉之后这台机器还一直醒着。"""
        m = re.search(r"exec caffeinate \S+ python3 ", BIN)
        self.assertIsNotNone(m, "caffeinate 不再是 python3 的父进程")

    def test_没有caffeinate也能起(self):
        """Linux 上没有这个命令(服务器常驻那份走这一支),不能因此起不来。"""
        self.assertIn("command -v caffeinate", BIN)
        at = BIN.index("command -v caffeinate")
        tail = BIN[at:at + 900]
        self.assertIn("fi\n", tail)
        # if 块之后还有一条不带 caffeinate 的 exec
        after = tail[tail.index("fi\n"):]
        self.assertRegex(after, r"exec python3 .*ui_server\.py")


if __name__ == "__main__":
    unittest.main()
