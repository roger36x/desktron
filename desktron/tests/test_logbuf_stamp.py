"""落盘的那份日志也要带时刻(2026-08-25 排查手机连不上时卡在这里)。

内存缓冲那一份一直带 t——日志悬浮窗按它排。而 _Tee 往真实 stdout 透传时是原样写的,
app 壳把它重定向到 /tmp/desktron-web.log,于是事后翻那个文件只看得见「发生过什么」,
看不见「什么时候」:那天 63 条 AttributeError 没法跟用户说的故障窗口对齐,
只能靠 ssh 进程的年龄反推时刻。

补时刻要按行首补,不能按 write() 补:print 会把一行分几次写进来,
整段前面加一个时刻会把时间戳插进句子中间。
"""
import io
import os
import re
import sys
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
import logbuf  # noqa: E402

STAMP = re.compile(r"^\[\d\d-\d\d \d\d:\d\d:\d\d\] ")


class Stamp(unittest.TestCase):
    def setUp(self):
        self.raw = io.StringIO()
        self.tee = logbuf._Tee(self.raw, "out")

    def lines(self):
        return [x for x in self.raw.getvalue().split("\n") if x]

    def test_整行带时刻(self):
        self.tee.write("一整行\n")
        self.assertTrue(STAMP.match(self.lines()[0]), self.lines())

    def test_多行每行都带(self):
        self.tee.write("甲\n乙\n丙\n")
        self.assertEqual(len(self.lines()), 3)
        for ln in self.lines():
            self.assertTrue(STAMP.match(ln), ln)

    def test_半行分次写不被切开(self):
        """print 常把一行分几次写进来。按 write 补的话,时刻会插进句子中间。"""
        self.tee.write("半")
        self.tee.write("行拼")
        self.tee.write("起来\n")
        self.assertEqual(len(self.lines()), 1, self.lines())
        self.assertIn("半行拼起来", self.lines()[0])
        self.assertEqual(len(STAMP.findall(self.lines()[0])), 1, "一行里补了不止一个时刻")

    def test_空写入不产生噪声(self):
        self.tee.write("")
        self.tee.write("\n")
        self.assertEqual(self.lines(), [], "空写入凭空造出了一行")

    def test_原样透传的内容不丢(self):
        """补时刻是加前缀,不能动正文——traceback 的缩进、制表都得原样留着。"""
        self.tee.write("    ~~~~~~^^^\n")
        self.assertTrue(self.lines()[0].endswith("    ~~~~~~^^^"), self.lines())

    def test_缓冲那一份不重复带时刻(self):
        """_BUF 的条目自带 t 字段,正文里再补一遍就是两份时刻。"""
        n0 = len(logbuf._BUF)
        self.tee.write("进缓冲的那一行\n")
        self.assertGreater(len(logbuf._BUF), n0, "没进缓冲")
        last = logbuf._BUF[-1]
        self.assertIn("t", last)
        self.assertFalse(STAMP.match(last["ln"]), "缓冲里的正文也被补了时刻:%r" % last["ln"])

    def test_底层流坏了不影响切行(self):
        """透传失败(管道断了之类)不该连带把缓冲那一路也废掉。"""
        class Bad:
            def write(self, s):
                raise OSError("broken pipe")

        tee = logbuf._Tee(Bad(), "out")
        n0 = len(logbuf._BUF)
        tee.write("照样要进缓冲\n")
        self.assertGreater(len(logbuf._BUF), n0)


if __name__ == "__main__":
    unittest.main()
