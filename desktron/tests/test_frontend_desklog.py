"""前端能往日志悬浮窗写日志,且这条路不许拖慢主流程。

浏览器里 console.log 的东西进不了那个面板,排查前端状态只能开 Web 检查器——门槛高到
很多问题就这么放着了。deskLog 把前端诊断 POST 进 /workbench/logs(后端 logbuf.push_client),
前后端进同一条流。

这一层没有渲染环境可跑,只能钉源码上的几处特征。要钉住的是三件容易在后续改动里丢掉的事:
一、deskLog 存在,且没有任何调用点 await 它——一旦有人为了「确保写进去」加上 await,
    这条诊断路就成了渲染路径上的一次网络往返;
二、发送失败静默,不抛未捕获的 rejection 污染真正要看的现场;
三、hsPour 里那条重放诊断还在,且四个数一个不少(它是「切引擎后过程区展不开」的唯一取证)。
"""
import os
import re
import unittest

import _srcmap

FRONT = os.path.join(os.path.dirname(__file__), "..", "..", "frontend")
CORE = open(os.path.join(FRONT, "app-core.js"), encoding="utf-8").read()
CHAT = _srcmap.chat_src()
LOGDOCK = open(os.path.join(FRONT, "app-logdock.js"), encoding="utf-8").read()


class DeskLogExists(unittest.TestCase):
    def test_全局函数存在且挂在_window_上(self):
        self.assertIn("function deskLog(...args)", CORE)
        self.assertIn("window.deskLog = deskLog", CORE)

    def test_写进后端同一条流(self):
        m = re.search(r"function _deskLogFlush\(\)[\s\S]+?\n}", CORE)
        self.assertTrue(m)
        seg = m.group(0)
        self.assertIn('fetch("/workbench/logs"', seg)
        self.assertIn('method: "POST"', seg)
        self.assertIn("JSON.stringify({ line })", seg)

    def test_对象参数序列化失败退回String(self):
        m = re.search(r"function deskLog\(\.\.\.args\)[\s\S]+?\n}", CORE)
        self.assertTrue(m)
        self.assertIn("JSON.stringify(a)", m.group(0))
        self.assertIn("String(a)", m.group(0))

    def test_同时留一份在console(self):
        m = re.search(r"function deskLog\(\.\.\.args\)[\s\S]+?\n}", CORE)
        self.assertIn("console.log(line)", m.group(0))


class NeverBlocksTheCaller(unittest.TestCase):
    def test_不是async函数(self):
        self.assertNotIn("async function deskLog", CORE)

    def test_没有任何调用点await它(self):
        # 加 await 等于把一次网络往返塞进渲染路径,而这条路的全部价值就在于不影响主流程
        for name, src in (("app-core.js", CORE), ("app-chat.js", CHAT), ("app-logdock.js", LOGDOCK)):
            self.assertNotIn("await deskLog", src, name)
            self.assertNotIn("await window.deskLog", src, name)

    def test_失败静默吞掉(self):
        m = re.search(r"function _deskLogFlush\(\)[\s\S]+?\n}", CORE)
        seg = m.group(0)
        self.assertIn(".catch(() => {})", seg)   # 请求本身失败:不抛未捕获 rejection
        self.assertIn("try {", seg)              # fetch 不可用/被拦:整段也不许炸出来
        self.assertIn("} catch {}", seg)

    def test_按节流合批发送(self):
        # 流式场景下每帧一条,一条一个请求会把单线程的 ui_server 打满
        self.assertIn("const DESKLOG_MS = 200", CORE)
        m = re.search(r"function deskLog\(\.\.\.args\)[\s\S]+?\n}", CORE)
        seg = m.group(0)
        self.assertIn("_deskLogBuf.push(line)", seg)
        self.assertIn("setTimeout(_deskLogFlush, DESKLOG_MS)", seg)
        self.assertIn("if (!_deskLogTimer)", seg)          # 窗口内只排一次,不是每条一个定时器
        self.assertIn("const DESKLOG_MAX", CORE)           # 后端不可达时缓冲不无限涨

    def test_合批后仍是多行(self):
        # push_client 按行拆,所以拼接必须用换行,不能用空格把几条挤成一行
        self.assertIn('_deskLogBuf.join("\\n")', CORE)


class LogDockShowsUiStream(unittest.TestCase):
    def test_ui这一档单独配色(self):
        self.assertIn("#logdock-panel .ld-ui", LOGDOCK)
        m = re.search(r"function makeRow\(x\)[\s\S]+?\n  }", LOGDOCK)
        self.assertTrue(m)
        self.assertIn('x.s === "ui"', m.group(0))
        self.assertIn('"ld-ui"', m.group(0))

    def test_面板标题上看得出有前端这一路(self):
        self.assertIn("前端", LOGDOCK)
        self.assertIn('uiTag.className = "ld-ui"', LOGDOCK)

    def test_样式仍注入在本文件内(self):
        self.assertIn('st.id = "logdock-style"', LOGDOCK)


class ReplayDiagnostic(unittest.TestCase):
    """切引擎(整页重载)之后,还在跑的那一回合过程区展不开、也没有摘要。

    后端已排除:/session/{id}/message 里那条 running 消息带着 reasoning/tool/text 的 parts。
    断在前端重放,而重放这一段没有任何取证——这条诊断就是为了把三种病灶分开:
    parts=0 / parts>0 但步骤=0 且过程区脱离文档 / 两者都不为 0(CSS 藏了)。
    """

    def test_诊断落在hsPour的running那一支(self):
        m = re.search(r"function hsPour\(v, msgs\)[\s\S]+?\n}", CHAT)
        self.assertTrue(m)
        pour = m.group(0)
        run = re.search(r"if \(info\.running\) \{[\s\S]+?\n    \} else if", pour)
        self.assertTrue(run, "hsPour 里找不到 running 分支")
        self.assertIn("deskLog(", run.group(0))

    def test_诊断带齐四个字段(self):
        m = re.search(r"deskLog\(`\[hs\] 重放 [\s\S]+?\);", CHAT)
        self.assertTrue(m, "hsPour 的重放诊断没了")
        seg = m.group(0)
        self.assertIn("${v.sid}", seg)                        # 哪一段会话
        self.assertIn("回合 ${v.turns.indexOf(t) + 1}", seg)   # 第几个回合
        self.assertIn("parts=${t.parts.size}", seg)           # 收进来几个 part
        self.assertIn("类型=", seg)
        self.assertIn(".map((x) => x.type)", seg)             # 各 part 的 type
        self.assertIn("过程区在文档=${!!(t.procEl && t.procEl.isConnected)}", seg)
        self.assertIn("步骤=", seg)

    def test_步骤数取过程区盒子的子节点数(self):
        m = re.search(r'const body = t\.procEl && t\.procEl\.querySelector\("\.hs-proc-body"\);', CHAT)
        self.assertTrue(m)
        self.assertIn("body ? body.children.length : 0", CHAT)


if __name__ == "__main__":
    unittest.main()
