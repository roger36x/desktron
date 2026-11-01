"""工具步骤那一行的宾语。

一列步骤读起来是「动词 + 宾语」:已读取 AGENTS.md、已执行 ls -la /tmp/x。宾语取不到就只剩
动词,一列全是「已执行 ls」,谁也看不出那几步分别在干什么(2026-08-17 用户实见,pi 的 bash
不写 description)。

claude 的 Bash 入参带 description(模型发起调用时随手写的一句话),优先用它;别的引擎没有,
就得从命令本身取——取整条(剥掉 cd 铺垫、压掉多余空白、按同一个上限截断),不是只取首词。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
import _srcmap
from frontend_i18n_js import NODE_PRELUDE

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT_JS = os.path.join(ROOT, "frontend", "app-chat.js")


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class ToolObject(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        src = _srcmap.chat_src()
        cls.src = src
        start = src.find("function hsGlyphWide(")
        assert start >= 0, "hsGlyphWide 不见了"
        m = re.search(r"\nfunction hsToolObj\(part\)[\s\S]*?\n}\n", src)
        assert m, "hsToolObj 不见了"
        cls.fn = src[start:m.end()]

    def _obj(self, inp, tool=""):
        js = ("function hsToolGist(){ return '(说不清)'; }\n" + self.fn
              + "\nconsole.log(JSON.stringify(hsToolObj({tool:"
              + json.dumps(tool, ensure_ascii=False) + ",state:{input:"
              + json.dumps(inp, ensure_ascii=False) + "}})));")
        out = subprocess.run(["node", "-e", NODE_PRELUDE + js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def _detail_input(self, inp):
        js = (self.fn + "\nconst i = " + json.dumps(inp, ensure_ascii=False)
              + "; const d = hsToolDescription(i);"
              + " console.log(JSON.stringify({description:d,command:hsCommandView(i.command,d)}));")
        out = subprocess.run(["node", "-e", NODE_PRELUDE + js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_宾语要取到那个目录(self):
        """首词是动作,宾语在参数里:「ls」看不出列的是哪个目录。

        原来的做法是整条命令原样摆出来(至少带上了参数);词表接手之后由它翻成
        「列出 pi-gate」,同一件事说得更短。两种做法都不接受只剩动词的「已执行 ls」。
        """
        self.assertEqual(self._obj({"command": "ls -la /tmp/pi-gate"})["t"], "列出 pi-gate")

    def test_cd_铺垫仍然剥掉(self):
        self.assertEqual(self._obj({"command": "cd /x && ls -la /tmp/y"})["t"], "列出 y")

    def test_子命令不是宾语(self):
        """git 的 diff / log 是动词的一部分,跳过它再取宾语,否则成了「检查代码差异 diff」。"""
        self.assertEqual(self._obj({"command": "git diff"})["t"], "检查代码差异")
        self.assertEqual(self._obj({"command": "git diff -- frontend/app-chat.js"})["t"],
                         "检查代码差异 app-chat.js")

    def test_只有_cd_时退回原样(self):
        """剥完什么都不剩,空着一行「已执行」比多余的 cd 更难读。"""
        self.assertEqual(self._obj({"command": "cd /some/where"})["t"], "cd /some/where")

    def test_长命令按宽度截断且带省略号(self):
        """超长按显示宽度截断,末尾补 …,读得出是被截断而不是凭空断掉。"""
        got = self._obj({"command": "python3 -c " + "x" * 200})["t"]
        self.assertLessEqual(len(got), 46)
        self.assertTrue(got.endswith("…"))

    def test_宽字符按列宽截断(self):
        """汉字占两列:同样的列预算,汉字装得比 ASCII 少,按字符数硬切会算错宽度。"""
        got = self._obj({"command": "echo " + "汉" * 60})["t"]
        # "echo "(5 列) + 20 个汉字(40 列) = 45 列,再补省略号
        self.assertEqual(got, "echo " + "汉" * 20 + "…")

    def test_多行命令压成一行(self):
        """脚本按行写,标题这一行放不下也不该换行——展开区里才给原样的多行。"""
        got = self._obj({"command": "for f in a b; do\n  echo $f\ndone"})["t"]
        self.assertNotIn("\n", got)

    def test_脚本摘要保留等宽但使用普通灰色档(self):
        got = self._obj({"command": "for f in a b; do\n  echo $f\ndone"})
        self.assertTrue(got["code"])
        self.assertTrue(got["cmd"])

    def test_有_description_时仍优先用它(self):
        """claude 那侧走的是这条:模型自己写的一句话比命令说明得多。"""
        got = self._obj({"description": "查找记录里的压缩标记", "command": "grep -n x y"})
        self.assertEqual(got["t"], "查找记录里的压缩标记")
        self.assertFalse(got["code"])   # 散文不用等宽

    def test_文件类工具取文件名(self):
        self.assertEqual(self._obj({"file_path": "/a/b/AGENTS.md"})["t"], "AGENTS.md")

    def test_Subagent左侧统一标签右侧使用任务摘要(self):
        for tool in ("task", "agent", "subagent"):
            self.assertIn(f'{tool}: "Subagent"', self.src)
            got = self._obj({
                "agent": "desk-reviewer", "mode": "sync", "context": "fresh",
                "task": "只读审查 /Users/alice/work/desktron 当前未提交的 4 个文件",
            }, tool)
            self.assertEqual(got, {
                "t": "只读审查 desktron 当前未提交的 4 个文件", "code": False,
            })
            self.assertNotIn("desk-reviewer", got["t"])
            self.assertNotIn("sync", got["t"])

    def test_Subagent并行任务合并任务摘要(self):
        got = self._obj({"mode": "async", "tasks": [
            {"agent": "desk-scout", "task": "盘点未合入工作树"},
            {"agent": "desk-reviewer", "task": "检查前端回归风险"},
        ]}, "subagent")
        self.assertEqual(got, {
            "t": "盘点未合入工作树；检查前端回归风险", "code": False,
        })

    def test_Subagent旧入参可回退到描述或提示词(self):
        self.assertEqual(self._obj({"description": "检查布局"}, "agent"), {
            "t": "检查布局", "code": False,
        })
        self.assertEqual(self._obj({"prompt": "比较实现方案"}, "task"), {
            "t": "比较实现方案", "code": False,
        })

    def test_命令首行注释当作意图(self):
        """pi 的 bash 没有 description,意图靠命令首行的 # 注释自述,取它当摘要。"""
        got = self._obj({"command": "# 查找记录里的压缩标记\ngrep -n x y"})
        self.assertEqual(got["t"], "查找记录里的压缩标记")
        self.assertFalse(got["code"])   # 意图是散文,不用等宽

    def test_意图占位词退回命令摘要(self):
        got = self._obj({"description": "意图", "command": "git status"})
        self.assertEqual(got, {"t": "检查仓库状态", "code": False})

    def test_意图占位行不进入展开区(self):
        got = self._detail_input({"description": "意图", "command": "# 意图\ngit status"})
        self.assertEqual(got, {"description": "", "command": "git status"})

    def test_Code_Mode占位意图降级到内部命令(self):
        """真实回包把 shell 藏在 cmd 字符串里；不能把外层 JS 当成摘要或展开正文。"""
        inner = ("# 意图\n"
                 "sed -n '1,20p' frontend/app-chat.js\n"
                 "sed -n '21,40p' frontend/app-chat.js\n"
                 "sed -n '1,40p' desktron/tests/test_subagent_status.py")
        wrapper = ("const r = await tools.exec_command({\n"
                   "  cmd: " + json.dumps(inner, ensure_ascii=False) + ",\n"
                   "  workdir: '/repo'\n"
                   "});\ntext(r.output);")
        inp = {"description": "意图", "command": wrapper}
        self.assertEqual(self._obj(inp), {
            "t": "读取 app-chat.js 等 3 项", "code": False,
        })
        self.assertEqual(self._detail_input(inp), {
            "description": "",
            "command": ("sed -n '1,20p' frontend/app-chat.js\n"
                        "sed -n '21,40p' frontend/app-chat.js\n"
                        "sed -n '1,40p' desktron/tests/test_subagent_status.py"),
        })

    def test_Code_Mode兼容嵌套命令里的真实意图(self):
        inner = "# 核对会话接口字段\ncurl -fsS http://127.0.0.1/session"
        wrapper = ("const r = await tools.exec_command({cmd: "
                   + json.dumps(inner, ensure_ascii=False) + "});\ntext(r.output);")
        inp = {"command": wrapper}
        self.assertEqual(self._obj(inp), {"t": "核对会话接口字段", "code": False})
        self.assertEqual(self._detail_input(inp), {
            "description": "核对会话接口字段",
            "command": "curl -fsS http://127.0.0.1/session",
        })

    def test_Code_Mode显式意图盖过后端旧摘要(self):
        """前端热更新后应立即可见,不必等后端重启重建这张卡的 description。"""
        got = self._obj({
            "description": "读取 deploy-desk.sh",
            "command": "// @intent: 确认发布流程不会携带临时文件\n"
                       "const r = await tools.exec_command({cmd: 'sed …'});",
        })
        self.assertEqual(got["t"], "确认发布流程不会携带临时文件")
        self.assertFalse(got["code"])

    def test_functions_exec的JSON参数刷新后恢复意图与真实命令(self):
        source = ("// @intent: 确认远端generation实际位置\n"
                  "const r = await tools.exec_command({cmd: `# 确认远端generation实际位置\n"
                  "find $HOME/generations -name .payload-rev`});")
        inp = {"command": json.dumps({"input": source}, ensure_ascii=False)}
        self.assertEqual(self._obj(inp), {"t": "确认远端generation实际位置", "code": False})
        self.assertEqual(self._detail_input(inp), {
            "description": "确认远端generation实际位置",
            "command": "find $HOME/generations -name .payload-rev",
        })

    def test_Code_Mode意图允许_exec_pragma与全角冒号(self):
        got = self._obj({
            "command": "// @exec: {\"yield_time_ms\": 10000}\n"
                       "// @intent：核对协议字段\nconst r = 1;",
        })
        self.assertEqual(got["t"], "核对协议字段")

    def test_普通_JS_注释不盖过摘要(self):
        got = self._obj({"description": "检查仓库状态",
                         "command": "// 实现说明\nconst r = 1;"})
        self.assertEqual(got["t"], "检查仓库状态")

    def test_普通_shell_注释不盖过_claude_摘要(self):
        got = self._obj({"description": "运行部署检查",
                         "command": "# shellcheck disable=SC2086\n./deploy.sh"})
        self.assertEqual(got["t"], "运行部署检查")

    def test_展开区不重复已经显示的_shell_意图(self):
        got = self._detail_input({"description": "检查运行服务与会话接口",
                                  "command": "# 检查运行服务与会话接口\ncurl /desk-probe"})
        self.assertEqual(got, {"description": "检查运行服务与会话接口",
                               "command": "curl /desk-probe"})

    def test_引号残片不让整段命令冒充标题(self):
        inp = {"description": "", "command": "'# 统计最近六轮实际渲染负载\nfor sid in a b; do\n  echo $sid\ndone\""}
        self.assertEqual(self._obj(inp), {"t": "统计最近六轮实际渲染负载", "code": False})
        self.assertEqual(self._detail_input(inp), {
            "description": "统计最近六轮实际渲染负载",
            "command": "for sid in a b; do\n  echo $sid\ndone",
        })

    def test_Code_Mode展开区隐藏控制注释(self):
        got = self._detail_input({"description": "读取 deploy-desk.sh",
                                  "command": "// @exec: {\"yield_time_ms\":10000}\n"
                                             "// @intent: 检查发布链路\nconst r = 1;"})
        self.assertEqual(got, {"description": "检查发布链路", "command": "const r = 1;"})

    def test_claude_技术注释仍保留在命令正文(self):
        got = self._detail_input({"description": "运行部署检查",
                                  "command": "# shellcheck disable=SC2086\n./deploy.sh"})
        self.assertEqual(got["command"], "# shellcheck disable=SC2086\n./deploy.sh")

    def test_shebang_不算意图注释(self):
        """脚本的 #! 首行不是意图,退回命令本体(等宽)。"""
        got = self._obj({"command": "#!/bin/bash\necho hi"})
        self.assertTrue(got["code"])   # 走命令本体,不是散文意图

    def test_命令意图词表(self):
        """没写 # 注释也没有 description 时,按词表把常见命令翻成意图短语。"""
        self.assertEqual(self._obj({"command": "grep -n foo bar.py"})["t"], "查找 foo")
        self.assertEqual(self._obj({"command": "git status"})["t"], "检查仓库状态")
        self.assertEqual(self._obj({"command": "git diff"})["t"], "检查代码差异")
        self.assertEqual(self._obj({"command": "cat /tmp/a.py"})["t"], "读取 a.py")
        self.assertEqual(self._obj({"command": "python3 setup.py"})["t"], "运行 setup.py")
        self.assertEqual(self._obj({"command": "curl -s http://x"})["t"], "检查接口")
        self.assertEqual(self._obj({"command": "ls -la /tmp/x"})["t"], "列出 x")
        # 词表外:退回命令原文,不硬凑意图
        got = self._obj({"command": "wc -l a.txt"})
        self.assertEqual(got["t"], "wc -l a.txt")
        self.assertTrue(got["code"])

    def test_摘要只说第一个动作那一段(self):
        """后面的 | head、; echo 是配料,不是这一步的目的。

        原来在整条命令上找第一个命中的词,于是 `ls 长路径 | head` 被说成「读取 head 后面
        那个文件」、`pytest x.py | tail` 被说成「读取」、`ls …/frontend/ ; git status` 被
        说成「检查仓库状态」。拿本机 pi 会话实录的 71 条单行命令实测,命中的 62 条里
        29 条是这样张冠李戴的(2026-08-17)。
        """
        self.assertEqual(self._obj({"command": "ls -la /a/b/logs | head -30"})["t"], "列出 logs")
        self.assertEqual(self._obj({"command": "python3 -m pytest t/test_x.py -q | tail -20"})["t"],
                         "运行测试 test_x.py")
        self.assertEqual(self._obj({"command": 'ls -la /a/frontend/; echo "--"; git status'})["t"],
                         "列出 frontend")

    def test_cd_铺垫跳过后再认动作(self):
        self.assertEqual(self._obj({"command": "cd /repo && git log --oneline app.js"})["t"],
                         "查看提交历史 app.js")

    def test_正则检索式不当宾语倒出来(self):
        """`grep -aoE "function Il\\(e,t,r=!1\\)\\{"` 翻成「查找 function Il\\(e,t…」,
        一串转义谁也读不懂,还不如原样的命令。实录 71 条里有 22 条是这样的。
        判据用 HS_PATTERNISH:一句给人读的说明里不会出现正则元字符。"""
        got = self._obj({"command": 'grep -n "a\\|b" x.js'})
        self.assertEqual(got["t"], 'grep -n "a\\|b" x.js')
        self.assertTrue(got["code"])
        # 干净的检索式照旧翻,中文的也一样
        self.assertEqual(self._obj({"command": 'grep -rn "已执行" frontend/'})["t"], "查找 已执行")

    def test_引号里的分隔符不切段(self):
        """`grep -E "a|b"` 是一个阶段,不是两个——切错了首词就成了 b。"""
        self.assertEqual(self._obj({"command": 'grep -rn "已执行" a/ | wc -l'})["t"], "查找 已执行")

    def test_没有宾语的动词退回原文(self):
        """「读取 文件」「查找 代码」这种没有宾语的说法什么也没说,不如原样的命令。"""
        got = self._obj({"command": "cat"})
        self.assertEqual(got["t"], "cat")
        self.assertTrue(got["code"])

    def test_就地改写不说成读取(self):
        self.assertEqual(self._obj({"command": "sed -i '' 's/a/b/' x.py"})["t"], "改写 x.py")
        self.assertEqual(self._obj({"command": "sed -n '1,5p' x.py"})["t"], "读取 x.py")

    def test_引号里的内联脚本不当宾语(self):
        """python3 -c "…" 里装的是代码,不是这一步动的文件。"""
        self.assertEqual(self._obj({"command": "python3 -c \"import ast; ast.parse(open('a.py').read())\""})["t"],
                         "检查语法")



@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class UnknownShapes(unittest.TestCase):
    """认不出的入参形状:先用引擎自报的标题,再退到从输出里抠一句。

    某些 skill / webfetch 工具入参只有 {name} / {url},盘点时发现它们一律落到
    「从输出抠一句」那一档,而 state.title 里明摆着「Loaded skill: lark-doc」。
    """

    @classmethod
    def setUpClass(cls):
        src = _srcmap.chat_src()
        start = src.find("function hsGlyphWide(")
        m = re.search(r"\nfunction hsToolObj\(part\)[\s\S]*?\n}\n", src)
        cls.fn = src[start:m.end()]

    def _obj(self, state):
        js = ("function hsToolGist(){ return '(说不清)'; }\n" + self.fn
              + "\nconsole.log(JSON.stringify(hsToolObj({state:"
              + json.dumps(state, ensure_ascii=False) + "})));")
        out = subprocess.run(["node", "-e", NODE_PRELUDE + js], capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(out.returncode, 0, out.stderr)
        return json.loads(out.stdout)

    def test_skill_取名字(self):
        self.assertEqual(self._obj({"input": {"name": "lark-doc"},
                                    "title": "Loaded skill: lark-doc"})["t"], "lark-doc")

    def test_没有可认字段时用引擎标题(self):
        self.assertEqual(self._obj({"input": {}, "title": "编译 3 个文件"})["t"], "编译 3 个文件")

    def test_标题也没有才抠输出(self):
        self.assertEqual(self._obj({"input": {}, "output": "done"})["t"], "(说不清)")

    def test_命令仍然排在标题前面(self):
        """bash 的 title 就是命令本身,让它盖过词表翻译会倒退一步。"""
        self.assertEqual(self._obj({"input": {"command": "git status"},
                                    "title": "git status"})["t"], "检查仓库状态")


# 两侧同跑的用例。前端(hsCmdIntent)管 pi 这类只给命令原文的引擎,后端
# (CodexAdapter._command_summary_one)管 codex——它把摘要写进 description,前端见到
# description 就直接用,走不到自己那份。同一件事两份实现,只能靠一份共同的用例钉住:
# 原来靠注释写「两侧保持同步」,而两边的缺陷正是一模一样地各留了一份。
SAME_ON_BOTH_SIDES = [
    # 张冠李戴那一类:动词必须属于第一个真动作,不能是后面管道里的
    "ls -la /a/b/logs | head -30",
    "python3 -m pytest t/test_x.py -q | tail -20",
    'ls -la /a/frontend/; echo "--"; git status',
    "cd /repo && git log --oneline app.js",
    # 检索式:干净的翻,带正则元字符的整条退回原文
    'grep -rn "已执行" frontend/',
    'grep -n "a\\|b" x.js',
    'grep -aoE "function Il\\(e,t,r=!1\\)\\{" dist/cli.js',
    'grep -rn "已执行" a/ | wc -l',
    # git 的全局选项:codex 惯用 `git -C <abs path> <子命令>`,剥掉再判子命令
    "git -C /Users/x/work/desktron status --short",
    "git -C /Users/x/work/desktron diff -- desktron/lib/engine_adapters.py",
    "git --no-pager -C /x log --oneline -5",
    "git -c core.pager=cat -C /x branch",
    "gitk --all",
    # 宾语
    "git diff",
    "git diff -- frontend/app-chat.js",
    "sed -n '1,20p' frontend/app-chat.js",
    "sed -i '' 's/a/b/' x.py",
    "python3 .claude/skills/journal/scripts/journal_append.py --request",
    "cat",
    "wc -l a.jsonl b.jsonl",
    "ls -la /tmp/pi-gate",
    "curl -s http://x",
    "find /a/b -name cli.js",
    "which pi",
    'env | grep -i "^PI_"',
]


@unittest.skipUnless(shutil.which("node"), "需要 node 跑前端函数")
class BothSidesAgree(unittest.TestCase):
    """前后端两份实现在同一批命令上给同一个答案。"""

    @classmethod
    def setUpClass(cls):
        src = _srcmap.chat_src()
        parts = [re.search(r"^const HS_PATTERNISH = .*$", src, re.M).group(0),
                 re.search(r"^const HS_GIT_GLOBAL_VALUE = [\s\S]*?\];$", src, re.M).group(0)]
        for name in ("hsGlyphWide", "hsStrWidth", "hsCut", "hsGitNorm",
                     "hsCmdStages", "hsCmdArgs", "hsCmdArg", "hsCmdIntent"):
            m = re.search(r"\nfunction %s\(.*?\n}" % re.escape(name), src, re.S)
            assert m, "%s 不见了" % name
            parts.append(m.group(0))
        js = ("\n".join(parts) + "\nconst C = " + json.dumps(SAME_ON_BOTH_SIDES)
              + ";\nconsole.log(JSON.stringify(C.map(hsCmdIntent)));")
        out = subprocess.run(["node", "-e", NODE_PRELUDE + js], capture_output=True, text=True, timeout=60, encoding='utf-8')
        assert out.returncode == 0, out.stderr
        cls.front = json.loads(out.stdout)

        argv = sys.argv
        sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
        sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
        try:
            import engine_adapters as ea
        finally:
            sys.argv = argv
        cls.back = [ea.CodexAdapter._command_summary_one(c) for c in SAME_ON_BOTH_SIDES]

    def test_逐条一致(self):
        for cmd, f, b in zip(SAME_ON_BOTH_SIDES, self.front, self.back):
            with self.subTest(cmd=cmd):
                self.assertEqual(f, b)

    def test_翻不出的照实留空(self):
        """两侧都用空串表示「翻不出」:前端据此退回命令原文,后端据此把 description 留空,
        同样让前端拿 command 那一栏去显示。硬凑一个「执行 <首词>」等于把 ls、cat、python
        都说成同一句话。"""
        i = SAME_ON_BOTH_SIDES.index("which pi")
        self.assertEqual(self.front[i], "")
        self.assertEqual(self.back[i], "")


if __name__ == "__main__":
    unittest.main()
