"""终端标签的标题跟着 shell 走,而不是靠后端探测进程。

2026-09-03 用户实见:标签一直写着 `workspace`,ssh 到别的机器、换了目录都不变。
根因是我们只有一条轮询:后端 lsof 查本地 PTY 进程的 cwd(/workbench/term/cwd)。
一 ssh 出去,那个本地进程的 cwd 就再也不动,远端主机名更无从得知。

macOS 的终端从不探测进程。标题是 shell 自己在输出流里夹带的转义序列报上来的:

    OSC 0 / 1 / 2  设标题(0=窗口与标签、1=标签、2=窗口)
    OSC 7          报工作目录,载荷是 file://主机名/路径 —— 带主机名

Apple 在 /etc/zshrc_Apple_Terminal 里挂了 precmd/preexec,空闲时报目录、执行时报命令名。
关键在于 ssh 之后发这些字节的是远端的 shell:它写进远端 pty,顺着 ssh 流原样回到本地
终端,本地不必做任何事,标题就跟着主机和目录走。

口径:shell 报了就用 shell 的,没报才落回轮询。

一条要记的现实:本机这一路多半报不上来。Apple 那份 rc 里的 update_terminal_cwd 只在
TERM_PROGRAM=Apple_Terminal 时才装,我们的 PTY 不是,所以本机 zsh 不发 OSC 7。真正吃到
红利的是 ssh 到 Linux——Debian/Ubuntu 的 bash 默认 PROMPT_COMMAND 就带
`\\e]0;\\u@\\h:\\w\\a`,一连上标题就活了。要让本机也报,得在我们起 shell 时补一段 precmd,
那是后端改动,不在这一轮。

实测(对活着的 PTY 直接打转义序列,读标签上的文字):

    起始                                        workspace
    本机 OSC 7  file://mymac/…/desktron      desktron        (主机=基线,不带前缀)
    远端 OSC 0  alice@gpu-node-01:~/vllm     gpu-node-01:vllm
    远端跑命令  vim benchmark.py                vim benchmark.py
    回到本机    file://mymac/…/Downloads        Downloads
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
TERM = open(os.path.join(ROOT, "frontend", "app-terminal.js"), encoding="utf-8").read()


def fn(name):
    m = re.search(r"\nfunction %s\(.*?\n}" % re.escape(name), TERM, re.S)
    assert m, name
    return m.group(0)


class 两类转义序列都接(unittest.TestCase):
    def test_OSC_0_2_走xterm自带事件(self):
        self.assertIn("term.onTitleChange(", fn("_termAppWireTitle"))

    def test_OSC_1_和_7_单独注册(self):
        """OSC 1(图标名=标签名)不在 onTitleChange 里,OSC 7 更没有内建处理。"""
        seg = fn("_termAppWireTitle")
        self.assertIn("term.parser.registerOscHandler(1,", seg)
        self.assertIn("term.parser.registerOscHandler(7,", seg)

    def test_处理器不截断后续处理(self):
        """返回 false = 只看一眼,继续交给后面的处理器,不抢 xterm 自己的活。"""
        seg = fn("_termAppWireTitle")
        self.assertEqual(seg.count("return false;"), 2)

    def test_解析器缺失时安全退出(self):
        """老 build 没有 parser.registerOscHandler,不能因此炸掉整个挂载。"""
        self.assertIn("if (!term.parser || !term.parser.registerOscHandler) return;",
                      fn("_termAppWireTitle"))


class 标题取最近报上来的那一条(unittest.TestCase):
    def test_按时间戳选而不是固定优先OSC7(self):
        """固定优先 OSC 7 试过,不行:本机报过一次 OSC 7 之后 ssh 出去,远端多半只发
        OSC 0(Debian/Ubuntu 的 bash 就只发这个),旧的本机 cwd 会一直压着远端标题,
        标签卡在本机那个目录上(实测复现:远端换了两次目录,标签纹丝不动)。"""
        seg = fn("_termTabLabel")
        self.assertIn("(b.at || 0) >= (a.at || 0)", seg)
        wire = fn("_termAppWireTitle")
        self.assertIn("o.at = performance.now()", wire)


class 主机名只在不是本机时才显示(unittest.TestCase):
    def test_基线只由OSC7建立(self):
        """不拿 OSC 0/2 的标题立基线:本机 zsh 不发 OSC 7,ssh 到 Linux 之后第一个报上来的
        就是远端主机——拿它当本机的话,恰恰在最需要显示主机名的那一路上把主机名抹掉了。"""
        seg = fn("_termAppWireTitle")
        self.assertIn("seenLocal(o); tab.oscCwd = o;", seg)
        self.assertNotIn("seenLocal(o); tab.oscTitle", seg)

    def test_与基线相同的主机不显示(self):
        self.assertIn("src.host !== tab.localHost", fn("_termTabLabel"))

    def test_去掉域名后缀(self):
        self.assertIn('src.host.split(".")[0]', fn("_termTabLabel"))


class 解析两种常见写法(unittest.TestCase):
    def test_标题里的user_at_host冒号路径(self):
        """Debian/Ubuntu 的 bash 默认写法。认不出这一种,ssh 场景就整个不工作。"""
        seg = fn("_termTitleParse")
        self.assertIn(r"/^[^\s@]+@([^\s:]+)\s*:\s*(.+)$/", seg)

    def test_认不出格式时原样当内容(self):
        """`vim foo.c` 这类「正在跑的命令」不该被丢掉——macOS 终端也是这么显示的。"""
        self.assertIn('{ host: "", path: s }', fn("_termTitleParse"))

    def test_OSC7是file_url且主机段可空(self):
        seg = fn("_termOsc7Parse")
        self.assertIn(r"/^file:\/\/([^/]*)(\/.*)$/", seg)
        self.assertIn("decodeURIComponent(path)", seg)


class 截断按语义分方向(unittest.TestCase):
    def test_目录从左截主机从右截(self):
        """目录的末段才是人认的那一截,主机名的前缀才是它的身份。"""
        seg = fn("_termTabLabel")
        self.assertIn('"…" + x.slice(x.length - n + 1)', seg)   # cutDir
        self.assertIn('x.slice(0, n - 1) + "…"', seg)           # cutHost

    def test_位置不够先削主机(self):
        """目录是一直在变的那一半,优先保住它。"""
        self.assertIn("Math.min(base.length, 12)", fn("_termTabLabel"))


class 轮询退居兜底(unittest.TestCase):
    def test_shell报过之后轮询让路(self):
        """连 lsof 都不必再打:早退要排在取 cwd 之前。"""
        seg = fn("termAppCwdPoll")
        gate = seg.index("if (tab.oscOwned) return;")
        self.assertLess(gate, seg.index("/workbench/term/cwd"))

    def test_挂载时接上(self):
        self.assertIn("_termAppWireTitle(tab, h);", fn("_termAppMount"))
