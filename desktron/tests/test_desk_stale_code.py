"""Desk 后端在跑旧代码时,自己说出来。

lark_sync 那侧早有这一条(它的进程跨 Desk 重启存活,能跑上几周旧代码)。而 Desk 自己同样会:
改完后端不重启,新加的路由就是不存在的,界面上只看到一个 404——2026-08-21 一天里两次撞上
(引擎一键更新那个端点)。判据与那边同源:进程起于何时,对比它实际加载的那份代码有多新。

只看 lib 下的 .py:前端是从磁盘现读的,改完刷新即生效,算进来会常亮。
"""
import os
import re
import sys
import time
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
SRC = open(os.path.join(LIB, "agents_status.py"), encoding="utf-8").read()
INIT = open(os.path.join(ROOT, "frontend", "app-init.js"), encoding="utf-8").read()


class SelfCheck(unittest.TestCase):
    def test_按进程起始时刻比(self):
        seg = re.search(r"def desk_self_status\(\)[\s\S]+?\n\n\n", SRC).group(0)
        self.assertIn("oscompat.process_started_at(os.getpid())", seg)
        compat = open(os.path.join(LIB, "oscompat.py"), encoding="utf-8").read()
        started = re.search(r"def process_started_at\(pid\)[\s\S]+?\n\n\n", compat).group(0)
        self.assertIn('"ps", "-o", "lstart=", "-p", str(int(pid))', started)   # POSIX 仍取 ps 起始时刻
        self.assertIn("process_start_token(pid)", started)                    # Windows 取进程创建时间

    def test_只看后端的_py(self):
        seg = re.search(r"def desk_self_status\(\)[\s\S]+?\n\n\n", SRC).group(0)
        self.assertIn('if not f.endswith(".py")', seg)
        self.assertIn("os.path.dirname(os.path.abspath(__file__))", seg)   # 就是它加载的那个目录

    def test_留一点余量(self):
        """装机/同步那一刻文件与进程几乎同时,差零点几秒不算旧。"""
        seg = re.search(r"def desk_self_status\(\)[\s\S]+?\n\n\n", SRC).group(0)
        self.assertIn("newest > started + 2", seg)

    def test_并进状态聚合(self):
        self.assertIn('"desk": desk_self_status()', SRC)

    def test_红点认这一条(self):
        self.assertIn('if ((st.desk || {}).stale_code) why.push("Desk 后端在跑旧代码,需重启");', INIT)

    def test_刚起的进程不算旧(self):
        """本测试自己就是一个刚起的解释器,它加载的正是最新代码。"""
        sys.path.insert(0, LIB)
        import agents_status
        r = agents_status.desk_self_status()
        self.assertIn("stale_code", r)
        self.assertFalse(r["stale_code"], r)


if __name__ == "__main__":
    unittest.main()
