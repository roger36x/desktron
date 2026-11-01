"""工具卡的摘要要说「这一步想干什么」,而不是把命令原文摆上去(2026-08-20 用户实见)。

codex 家族的 commandExecution 只带命令本体,没有 claude 的 Bash 那样的 description 字段。
没有意图时后端会去猜(_command_summary_one 的词表),而检索式与长管道猜不出来——
一行 `rg -n "goal|create_goal|update_goal|…"` 截断在屏幕右边,读的人得自己解码。
通道只有命令文本本身:直接 shell 用 `# 意图`,Code Mode 外层用 `// @intent: 意图`,
这里把它提到 description 上。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea
finally:
    sys.argv = _argv


class Bus:
    def publish(self, e):
        pass


class IntentComment(unittest.TestCase):
    def _desc(self, cmd):
        return ea.CodexAdapter._command_input(cmd)["description"]

    def test_首行注释就是摘要(self):
        self.assertEqual(
            self._desc('# 查 slash 命令与 Codex 能力位\nrg -n "goal|create_goal|/goal" lib'),
            "查 slash 命令与 Codex 能力位")

    def test_包在_bash_lc_里也认(self):
        """codex 常把命令包成 `bash -lc "…"`,注释在壳里面。先拆壳再取首行。"""
        self.assertEqual(self._desc('bash -lc "# 看看发送路径\nrg -n prompt_async lib"'),
                         "看看发送路径")

    def test_Code_Mode外层意图优先于嵌套动作(self):
        """真实 functions.exec 载荷的首行是 JS,不能再按 shell 的 `#` 规则处理。"""
        raw = '''// @intent: 确认发布流程不会携带临时文件
const results = await Promise.all([
  tools.exec_command({cmd: "sed -n '1,80p' deploy-desk.sh"}),
  tools.exec_command({cmd: "sed -n '1,80p' install-remote.sh"})
]);'''
        got = ea.CodexAdapter._command_input(raw)
        self.assertEqual(got["description"], "确认发布流程不会携带临时文件")
        self.assertNotIn("// @intent:", got["command"])

    def test_functions_exec的JSON参数会先解开input(self):
        source = '''// @intent: 确认远端generation实际位置
const r = await tools.exec_command({cmd: `# 确认远端generation实际位置
find $HOME/.local/share/desktron/generations -name .payload-rev`});'''
        raw = json.dumps({"input": source}, ensure_ascii=False)
        got = ea.CodexAdapter._command_input(raw)
        self.assertEqual(got["description"], "确认远端generation实际位置")
        self.assertNotIn('{"input"', got["command"])
        self.assertNotIn("// @intent:", got["command"])
        tool, projected = ea.CodexAdapter._rollout_tool("exec", raw)
        self.assertEqual(tool, "bash")
        self.assertEqual(projected["description"], "确认远端generation实际位置")

    def test_Code_Mode反引号命令能被展开(self):
        source = '''const r = await tools.exec_command({cmd: `# 核对远端目录
find $HOME/generations -name .payload-rev`});'''
        got = ea.CodexAdapter._command_input(source)
        self.assertEqual(got["description"], "核对远端目录")

    def test_exec_pragma可以放在意图前面(self):
        raw = '''// @exec: {"yield_time_ms": 10000}
// @intent：核对协议是否已有意图字段
const r = await tools.exec_command({cmd: "codex app-server --help"});'''
        got = ea.CodexAdapter._command_input(raw)
        self.assertEqual(got["description"], "核对协议是否已有意图字段")
        self.assertNotIn("// @exec:", got["command"])
        self.assertNotIn("// @intent", got["command"])

    def test_兼容嵌套_shell_里的旧意图(self):
        raw = '''const r = await tools.exec_command({
  cmd: "# 查找发送路径\\nrg -n prompt_async lib"
});'''
        self.assertEqual(self._desc(raw), "查找发送路径")

    def test_单引号嵌套命令保留中文意图(self):
        """真实 Code Mode 载荷常用单引号，中文不能被当成字节再解码一次。"""
        raw = """const r = await tools.exec_command({
  cmd: '# 核对会话状态\\nrg -n status frontend'
});"""
        got = ea.CodexAdapter._command_input(raw)
        self.assertEqual(got["description"], "核对会话状态")
        self.assertNotIn("æ", got["description"])

    def test_清理_appserver_留下的显示引号(self):
        got = ea.CodexAdapter._command_input(
            "'# 统计最近六轮实际渲染负载\nfor sid in a b; do\n  echo $sid\ndone\"")
        self.assertEqual(got["description"], "统计最近六轮实际渲染负载")
        self.assertEqual(got["command"], "for sid in a b; do\n  echo $sid\ndone")

    def test_普通_JS_注释不冒充意图(self):
        raw = '''// 这是实现说明
const r = await tools.exec_command({cmd: "git status"});'''
        self.assertEqual(self._desc(raw), "检查仓库状态")

    def test_shebang_不算意图(self):
        """`#!/usr/bin/env bash` 说的是拿什么跑,不是想干什么。"""
        self.assertEqual(self._desc("#!/usr/bin/env bash\necho hi"), "")

    def test_没有注释时词表照旧(self):
        self.assertEqual(self._desc("git status"), "检查仓库状态")

    def test_检索式仍然不硬翻(self):
        """没有注释、检索式又带正则元字符:倒出来是一串谁也读不懂的转义,
        留空让界面摆命令原文——这是既有的判断,不该被这次改动带偏。"""
        self.assertEqual(self._desc('rg -n "goal|create_goal" lib'), "")

    def test_命令原文照旧完整保留(self):
        """展开区保留真正执行的命令,但不再重复已经提成摘要的注释。"""
        cmd = "# 找一下发送路径\nrg -n prompt_async lib"
        shown = ea.CodexAdapter._command_input(cmd)["command"]
        self.assertEqual(shown, "rg -n prompt_async lib")

    def test_意图占位词不冒充摘要(self):
        got = ea.CodexAdapter._command_input("# 意图\ngit status")
        self.assertEqual(got, {"description": "检查仓库状态", "command": "git status"})

    def test_Code_Mode意图占位词也会降级(self):
        raw = '''// @intent: 本步目的
const r = await tools.exec_command({cmd: "git status"});'''
        got = ea.CodexAdapter._command_input(raw)
        self.assertEqual(got["description"], "检查仓库状态")
        self.assertNotIn("@intent", got["command"])


class GitGlobalOptions(unittest.TestCase):
    """`git -C <abs path> <子命令>` 要能认出子命令。

    codex 的 cwd 未必是仓根,它惯用 -C 而不是先 cd。判子命令的正则中间夹着 -C 与一长串路径
    就一条都不命中,于是一屏 git 卡片全退回命令原文(2026-08-20 用户实见:status / diff /
    log 六条全是 `git -C /Users/…/desktron diff…`)。
    """

    def _desc(self, cmd):
        return ea.CodexAdapter._command_input(cmd)["description"]

    def test_剥掉_C_与路径(self):
        self.assertEqual(self._desc("git -C /Users/x/work/desktron status --short"),
                         "检查仓库状态")

    def test_剥完仍取得到宾语(self):
        self.assertEqual(
            self._desc("git -C /Users/x/work/desktron diff -- desktron/lib/engine_adapters.py"),
            "检查代码差异 engine_adapters.py")

    def test_不取值的全局选项也剥(self):
        self.assertEqual(self._desc("git --no-pager -C /x log --oneline -5"), "查看提交历史")

    def test_带值的_c_连值一起剥(self):
        self.assertEqual(self._desc("git -c core.pager=cat -C /x branch"), "查看分支")

    def test_不是_git_的不动(self):
        """`gitk` 不是 git,剥词不能按前缀匹配。"""
        self.assertEqual(self._desc("gitk --all"), "")


class RuleIsInjected(unittest.TestCase):
    """约定要真的进系统提示,否则模型无从知道该写这一行。"""

    def _body(self, cls):
        return cls(tempfile.mkdtemp(), ROOT, Bus()).system_body({})

    def test_codex_家族带这条约定(self):
        for cls in (ea.CodexAdapter,):
            body = self._body(cls)
            self.assertIn("// @intent: <十来个中文字>", body, cls.__name__)
            self.assertIn("第一行写 `# <十来个中文字>`", body, cls.__name__)
            self.assertIn("只说明目的，不写占位词，不复述命令", body, cls.__name__)

    def test_claude_不带(self):
        """claude 的 Bash 自带 description 字段,界面优先用它,不必再要一行注释。"""
        self.assertNotIn("第一行先写一行", self._body(ea.ClaudeAdapter))


if __name__ == "__main__":
    unittest.main()
