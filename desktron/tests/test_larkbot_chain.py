"""larkbot 的存储、上下文组装与出站(docs/agent-hosting-design.md)。

钉住的核心是两道红线闸:
  第一道在取的时候——_sealed 层从不进入上下文组装。白名单不是黑名单,
  漏写一层白名单顶多少些上下文,漏写一层黑名单就是泄露。
  第二道在发的时候——出站必经 outbound.send,那里面有闸;别处不该直接调 larkapi。
"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from larkbot import context, handler, outbound, store  # noqa: E402
from curator.note import write_note  # noqa: E402


class Store(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()

    def test_存读一轮字段不丢(self):
        a, err = store.save(self.d, "bot1", {"name": "小助手", "lark_app_id": "cli_1"})
        self.assertEqual(err, "")
        got = store.load(self.d, "bot1")
        self.assertEqual(got["name"], "小助手")
        self.assertEqual(got["id"], "bot1")

    def test_代号与_app_id_填反了会被拦下(self):
        """2026-08-25 实测填反过一次:代号那栏填了 cli_ 开头的 App ID,App ID 那栏填了
        App Secret。两栏挨着问、名字又都叫 id,填反不算意外。"""
        a, err = store.save(self.d, "cli_a1b2c3d4e5f60718", {"name": "x"})
        self.assertIsNone(a)
        self.assertIn("App ID", err)

    def test_app_id_那栏收到的不像_app_id_就拒收(self):
        """拦的是「把 Secret 填进了这一栏」。Secret 一旦落进声明,就随 .workbench
        被打包同步——而它本该只在 ~/.config/desktron/env。"""
        a, err = store.save(self.d, "bot1", {"lark_app_id": "rXQjMK3j7reEC7zfJWd5PcYQQabcdefg"})
        self.assertIsNone(a)
        self.assertIn("cli_", err)
        self.assertIn("env", err)          # 得说清 Secret 该去哪儿,否则人只能瞎试
        self.assertIsNone(store.load(self.d, "bot1"))   # 真的没落盘

    def test_app_id_留空是允许的(self):
        """先建个空壳、之后再补 App ID 是正常路径,不该被这道校验挡住。"""
        a, err = store.save(self.d, "bot1", {"name": "x"})
        self.assertEqual(err, "")

    def test_引擎名单包含_pi(self):
        """当前支持 claude / codex / pi，旧自配模型引擎已退役。"""
        from larkbot import runner
        self.assertEqual(set(runner.ENGINES), {"claude", "codex", "pi"})
        self.assertIn("read,grep,find,ls", runner.ENGINES["pi"][1])
        self.assertIn("--no-session", runner.ENGINES["pi"][1])
        self.assertIn("--no-skills", runner.ENGINES["pi"][1])
        self.assertIn("--no-context-files", runner.ENGINES["pi"][1])
        self.assertNotIn("--skill", runner.ENGINES["pi"][1])
        self.assertNotIn("pi_reasoning_summary", " ".join(runner.ENGINES["pi"][1]))
        # 每个引擎都有模型旗标:模型是引擎之外的第二根轴,少一个旗标那个引擎就配不了模型
        for eng, spec in runner.ENGINES.items():
            self.assertTrue(spec[3], eng)

    def test_列模型_codex_家族的_json_解析(self):
        """形状取自 codex debug models 实测:models[] 里有 slug 与 visibility,
        只留 visibility=="list" 的;单条坏数据跳过,不拖垮整张清单(同 botmux)。"""
        from larkbot import runner
        blob = json.dumps({"models": [
            {"slug": "gpt-x", "visibility": "list"},
            {"slug": "hidden-one", "visibility": "internal"},
            {"slug": "", "visibility": "list"},
            {"no_slug": True},
            {"slug": "gpt-y", "visibility": "list"},
        ]})
        fake = mock.Mock(stdout=blob, stderr="")
        with mock.patch.object(runner.subprocess, "run", return_value=fake), \
             mock.patch.object(runner, "available", return_value=("/bin/codex", "")):
            got, err = runner.list_models("codex")
        self.assertEqual(got, ["gpt-x", "gpt-y"])
        self.assertEqual(err, "")

    def test_列模型_claude_用官方别名不猜全名(self):
        """claude 没有列表命令;全名随版本变,猜错还不如给稳定别名。"""
        from larkbot import runner
        got, err = runner.list_models("claude")
        self.assertEqual(got, ["opus", "sonnet", "haiku"])

    def test_列模型_pi_压平_provider(self):
        from larkbot import runner
        fake = mock.Mock(stdout=("provider  model  context\n"
                                 "deepseek  deepseek-v4-pro  1M\n"
                                 "openai  gpt-5  400K\n"), stderr="")
        with mock.patch.object(runner.subprocess, "run", return_value=fake) as run, \
             mock.patch.object(runner, "available", return_value=("/bin/pi", "")):
            got, err = runner.list_models("pi")
        command = run.call_args.args[0]
        self.assertIn("--no-skills", command)
        self.assertIn("--no-context-files", command)
        self.assertEqual(got, ["deepseek/deepseek-v4-pro", "openai/gpt-5"])
        self.assertEqual(err, "")

    def test_换引擎时清掉模型(self):
        """模型名是引擎专属的(claude 的 opus 对 codex 毫无意义)。带着旧值切过去,
        表现是引擎报参数错、一条消息都答不了——所以强制清,并在回话里说一声。
        参考 botmux 的同款语义(bot-config-editor.ts:切换 CLI 时 model 设 null)。"""
        store.save(self.d, "b1", {"name": "x", "engine": "claude", "model": "opus"})
        a = store.load(self.d, "b1")
        r, _ = handler._slash(self.d, a, "chat:A", "/engine codex")
        self.assertIn("已清掉", r)
        got = store.load(self.d, "b1")
        self.assertEqual(got["engine"], "codex")
        self.assertEqual(got["model"], "")

    def test_model_命令能设能清(self):
        store.save(self.d, "b1", {"name": "x"})
        a = store.load(self.d, "b1")
        handler._slash(self.d, a, "chat:A", "/model opus")
        self.assertEqual(store.load(self.d, "b1")["model"], "opus")
        a = store.load(self.d, "b1")
        r, _ = handler._slash(self.d, a, "chat:A", "/model -")
        self.assertIn("引擎默认", r)
        self.assertEqual(store.load(self.d, "b1")["model"], "")

    def test_没写的字段用默认值补齐(self):
        store.save(self.d, "bot1", {"name": "x"})
        got = store.load(self.d, "bot1")
        self.assertEqual(got["engine"], "claude")
        self.assertTrue(got["context"]["memory"])
        self.assertEqual(got["runtime"]["performance"], "fast")
        self.assertTrue(got["outbound"]["compliance"])

    def test_快速模式隔离交互配置且仍可选模型(self):
        from larkbot import runner
        codex = runner._argv("codex", "/bin/codex", "gpt-5.5", "fast")
        self.assertIn("--ignore-user-config", codex)
        self.assertIn("--ephemeral", codex)
        self.assertIn("project_doc_max_bytes=0", codex)
        self.assertEqual(codex[-2:], ["-m", "gpt-5.5"])
        full = runner._argv("codex", "/bin/codex", "gpt-5.5", "balanced")
        self.assertNotIn("--ignore-user-config", full)
        self.assertNotIn("project_doc_max_bytes=0", full)

    def test_登录失败快速返回且不暴露命令原文(self):
        from larkbot import runner
        runner._READY.clear()
        failed = mock.Mock(returncode=1, stdout="user@example.com", stderr="token=secret")
        with mock.patch.object(runner, "available", return_value=("/bin/codex", "")), \
             mock.patch.object(runner.subprocess, "run", return_value=failed) as run:
            ok, why = runner.ready("codex", force=True)
            ok2, why2 = runner.ready("codex")
        self.assertFalse(ok)
        self.assertEqual((ok2, why2), (ok, why))
        self.assertEqual(run.call_count, 1)
        self.assertIn("尚未登录", why)
        self.assertNotIn("secret", why)

    def test_默认值改了老_agent_跟着变(self):
        """默认值是读的时候补的,不是创建时冻进文件的——否则改一次默认值,
        存量 agent 全留在旧行为上,而人以为改了。"""
        store.save(self.d, "bot1", {"name": "x"})
        with open(os.path.join(store.dir_of(self.d), "bot1.json"), encoding="utf-8") as f:
            on_disk = json.load(f)
        with mock.patch.dict(store.DEFAULTS, {"engine": "codex"}):
            self.assertEqual(store.load(self.d, "bot1")["engine"], "codex")
        self.assertNotIn("engine", set(on_disk) - set(store.DEFAULTS))

    def test_声明里不许出现凭证(self):
        """.workbench 会随工作区被打包同步,凭证写进去就出不来了。"""
        for bad in ({"app_secret": "x"}, {"lark_token": "y"}, {"API_SECRET": "z"}):
            _, err = store.save(self.d, "bot1", bad)
            self.assertTrue(err, bad)
            self.assertIn("secret", err.lower())

    def test_id_不合规被挡(self):
        for bad in ("", "A", "../x", "有中文", "x" * 41):
            _, err = store.save(self.d, bad, {})
            self.assertTrue(err, bad)

    def test_allowed_列表整体覆盖不与默认值合并(self):
        """配成空表的意思是「不限制」。若与默认值合并,人就再也删不掉加过的条目。"""
        store.save(self.d, "bot1", {"allowed_users": ["ou_a"]})
        store.save(self.d, "bot1", {"allowed_users": []})
        self.assertEqual(store.load(self.d, "bot1")["allowed_users"], [])

    def test_不同锚点的会话互不串(self):
        store.session_append(self.d, "bot1", "chat:A", "user", "甲说的")
        store.session_append(self.d, "bot1", "thread:B", "user", "乙说的")
        a = [t["text"] for t in store.session(self.d, "bot1", "chat:A")["turns"]]
        b = [t["text"] for t in store.session(self.d, "bot1", "thread:B")["turns"]]
        self.assertEqual((a, b), (["甲说的"], ["乙说的"]))

    def test_不同_agent_同锚点也不串(self):
        store.session_append(self.d, "bot1", "chat:A", "user", "给一号的")
        store.session_append(self.d, "bot2", "chat:A", "user", "给二号的")
        self.assertEqual(len(store.session(self.d, "bot1", "chat:A")["turns"]), 1)

    def test_历史超上限只留最近的(self):
        for i in range(store.KEEP_TURNS + 10):
            store.session_append(self.d, "bot1", "chat:A", "user", f"第{i}条")
        t = store.session(self.d, "bot1", "chat:A")["turns"]
        self.assertEqual(len(t), store.KEEP_TURNS)
        self.assertEqual(t[-1]["text"], f"第{store.KEEP_TURNS + 9}条")

    def test_clear_只清这一条会话(self):
        store.session_append(self.d, "bot1", "chat:A", "user", "甲")
        store.session_append(self.d, "bot1", "chat:B", "user", "乙")
        store.session_clear(self.d, "bot1", "chat:A")
        self.assertEqual(store.session(self.d, "bot1", "chat:A")["turns"], [])
        self.assertEqual(len(store.session(self.d, "bot1", "chat:B")["turns"]), 1)

    def test_记下每轮实际读到了什么(self):
        """这是「配置调得对不对」的唯一依据:配置说记忆是开的,不等于真读到了东西。"""
        store.used_append(self.d, "bot1", "chat:A",
                          {"memory": ["m1", "m2"], "knowledge": ["k1"], "history": 3}, "问了啥")
        got = store.used_recent(self.d, "bot1", "chat:A")
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["memory"], ["m1", "m2"])
        self.assertEqual(got[0]["q"], "问了啥")

    def test_用量只留最近几轮(self):
        for i in range(store.KEEP_USED + 4):
            store.used_append(self.d, "bot1", "chat:A", {"history": i}, f"第{i}问")
        got = store.used_recent(self.d, "bot1", "chat:A")
        self.assertEqual(len(got), store.KEEP_USED)
        self.assertEqual(got[0]["q"], f"第{store.KEEP_USED + 3}问")   # 最新的排在前

    def test_不给锚点时跨会话取最近的(self):
        """人在 Agents 页看的是这个 bot 最近怎么样,不分哪个群。"""
        store.used_append(self.d, "bot1", "chat:A", {}, "甲群问的")
        store.used_append(self.d, "bot1", "thread:B", {}, "乙话题问的")
        qs = [u["q"] for u in store.used_recent(self.d, "bot1")]
        self.assertIn("甲群问的", qs)
        self.assertIn("乙话题问的", qs)

    def test_别的_agent_的用量不混进来(self):
        store.used_append(self.d, "bot1", "chat:A", {}, "一号的")
        store.used_append(self.d, "bot2", "chat:A", {}, "二号的")
        self.assertEqual([u["q"] for u in store.used_recent(self.d, "bot1")], ["一号的"])

    def test_被拦的内容留得住且只留最近_50_条(self):
        for i in range(60):
            store.hold(self.d, "bot1", "chat:A", f"第{i}条被拦的", [{"kind": "x"}])
        with open(store.held_path(self.d), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertEqual(len(items), 50)
        self.assertEqual(items[-1]["text"], "第59条被拦的")

    def test_记下这份声明建在哪台机器上(self):
        """两侧的 workspace 各自独立,声明本来就不跨侧。记这个不是为了过滤,
        是为了认出「被人整个拷过来的那一份」——那时同一个 app 可能有两条长连接。"""
        with mock.patch("remote_auth.desk_id", return_value="desk_abc"):
            store.save(self.d, "bot1", {"name": "x"})
        self.assertEqual(store.load(self.d, "bot1")["created_on"], "desk_abc")

    def test_下划线开头的文件不算_agent(self):
        """_state.json / _held.json 与声明放在同一个目录里。"""
        store.session_append(self.d, "bot1", "chat:A", "user", "x")
        store.hold(self.d, "bot1", "chat:A", "y", [])
        store.save(self.d, "bot1", {"name": "真的"})
        self.assertEqual([a["id"] for a in store.list_all(self.d)], ["bot1"])


class Sealed(unittest.TestCase):
    """红线第一道闸:_sealed 从不进入上下文组装。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        for tier in ("sot", "notes", "journal"):
            os.makedirs(os.path.join(self.d, ".memory", "wf", tier), exist_ok=True)
        os.makedirs(os.path.join(self.d, ".memory", "_sealed"), exist_ok=True)
        with open(os.path.join(self.d, ".memory", "_sealed", "x.md"), "w", encoding="utf-8") as f:
            f.write("这是封存内容,任何情况下都不该出现在给模型的上下文里\n")

    def test_白名单只有_sot_与_notes(self):
        self.assertEqual(set(context.TIERS), {"sot", "notes"})
        self.assertNotIn("_sealed", context.TIERS)
        self.assertNotIn("journal", context.TIERS)

    def test_取正文时拒绝任何指向_sealed_的路径(self):
        self.assertEqual(context._body(self.d, "_sealed/x.md"), "")
        self.assertEqual(context._body(self.d, "wf/../_sealed/x.md"), "")

    def test_取正文时拒绝跳出_memory(self):
        with open(os.path.join(self.d, "outside.md"), "w", encoding="utf-8") as f:
            f.write("不该读到")
        self.assertEqual(context._body(self.d, "../outside.md"), "")

    def test_白名单之外的层取不到(self):
        """journal 不在白名单里,search 根本不会去查它。"""
        with mock.patch.object(context, "_search", return_value=[]) as m:
            context._pick(self.d, "随便什么词", context.TIERS, 5)
        for call in m.call_args_list:
            self.assertIn(call.args[2], context.TIERS)

    def test_组装出来的_prompt_不含封存内容(self):
        agent = {"id": "b", "context": {"memory": True, "knowledge": True,
                                        "role": "你是助手", "history": 5}}
        # 让检索假装把封存那条也返回了——闸不该指望上游永远不出错
        fake = [{"id": "leak", "where": "body", "path": "_sealed/x.md", "desc": "描述", "tier": "sot"}]
        with mock.patch.object(context, "_search", return_value=fake):
            prompt, used = context.build(self.d, agent, "问一句", "chat:A")
        self.assertNotIn("封存内容", prompt)
        self.assertEqual(used["memory"], [])

    def test_sealed_dir_指的是那个目录(self):
        self.assertTrue(context.sealed_dir(self.d).endswith(os.path.join(".memory", "_sealed")))


class Build(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, ".memory"), exist_ok=True)

    def _agent(self, **over):
        c = {"role": "", "memory": False, "knowledge": False, "history": 0}
        c.update(over)
        return {"id": "b", "context": c}

    def test_四段全关时只剩当前这句(self):
        prompt, used = context.build(self.d, self._agent(routing=False), "今天怎么样", "chat:A")
        self.assertIn("今天怎么样", prompt)
        self.assertFalse(used["role"])
        self.assertEqual(used["history"], 0)

    def test_第0段_默认注入且身份是飞书代理(self):
        """没有它时 bot 对「我是谁」唯一的信息来自引擎默认与 cwd 里的 AGENTS.md,
        把自己当 coding agent(2026-08-27 用户在飞书里实测,codex 自称编程代理)。"""
        prompt, _ = context.build(self.d, self._agent(), "你好", "chat:A")
        self.assertIn("Desktron", prompt)
        self.assertIn("飞书", prompt)
        self.assertIn("不是交付物", prompt)          # 对着 coding agent 的自我认知说的那句
        self.assertIn("300 字", prompt)              # 与 outbound.CARD_THRESHOLD 对齐
        self.assertIn("红线内容不要发到飞书", prompt)

    def test_第0段_排在角色之前(self):
        """role 是「你是谁」,它先得知道「你在哪儿」。"""
        prompt, _ = context.build(self.d, self._agent(role="你是运维助手"), "你好", "chat:A")
        self.assertLess(prompt.index("你在哪儿工作"), prompt.index("你是运维助手"))

    def test_第0段_能关掉(self):
        prompt, _ = context.build(self.d, self._agent(routing=False), "你好", "chat:A")
        self.assertNotIn("你在哪儿工作", prompt)

    def test_第0段_只列真的在的东西(self):
        """写了不存在的路径,模型会去读、读不到、然后开始猜,比不写更糟。
        空目录下:没有 mem_read.py 就没有记忆那一行。"""
        prompt, _ = context.build(self.d, self._agent(), "你好", "chat:A")
        self.assertNotIn("mem_read", prompt)
        os.makedirs(os.path.join(self.d, ".claude", "skills", "memory", "scripts"))
        with open(os.path.join(self.d, ".claude", "skills", "memory", "scripts",
                               "mem_read.py"), "w") as f:
            f.write("# stub")
        prompt, _ = context.build(self.d, self._agent(), "你好", "chat:A")
        self.assertIn("mem_read.py", prompt)
        self.assertIn("不要直接开文件", prompt)

    def test_不同工作流的记忆一视同仁(self):
        """这里曾有一段「本工作流的那条排前面」的重排,去掉了:bot 不属于任何工作流,
        顺序只由检索结果决定。"""
        fake = [{"id": "m-risk", "where": "body", "path": "risk/sot/a.md", "desc": ""},
                {"id": "m-inc", "where": "body", "path": "incident/sot/b.md", "desc": ""}]
        with mock.patch.object(context, "_search", return_value=fake):
            got = context._pick(self.d, "gpubox 扩容", ("sot",), 5)
        self.assertEqual([i["id"] for i in got], ["m-risk", "m-inc"])

    def test_记忆与知识共用同一批_sot_检索(self):
        """memory=sot+notes，knowledge=sot；共同开启时 sot 每个词只能搜一次。"""
        a = self._agent(memory=True, knowledge=True, memory_max=20, knowledge_max=10)
        with mock.patch.object(context, "_search", return_value=[]) as search:
            context.build(self.d, a, "desktron gpubox", "chat:A")
        terms = context._terms("desktron gpubox")
        self.assertEqual(search.call_count, len(terms) * len(context.TIERS))
        for term in terms:
            self.assertEqual(sum(1 for c in search.call_args_list
                                 if c.args[1] == term and c.args[2] == "sot"), 1)

    def test_mixed_sot_按_kind_与确认状态分流(self):
        base = os.path.join(self.d, ".memory", "wf")
        os.makedirs(os.path.join(base, "sot"), exist_ok=True)
        os.makedirs(os.path.join(base, "notes"), exist_ok=True)
        rows = [
            ("pinned-one", "sot", "judgment", True),
            ("legacy-one", "sot", "judgment", None),
            ("fact-one", "sot", "fact", None),
            ("note-one", "notes", "judgment", None),
        ]
        hits = {"sot": [], "notes": []}
        for nid, tier, kind, confirmed in rows:
            rel = f"wf/{tier}/{nid}.md"
            fm = {"id": nid, "tier": tier, "kind": kind, "summary": nid}
            if confirmed is not None:
                fm["user_confirmed"] = confirmed
            write_note(os.path.join(self.d, ".memory", rel), fm, nid + " body")
            hits[tier].append({"id": nid, "where": "body", "path": rel, "desc": nid})
        with mock.patch.object(context, "_search", side_effect=lambda _r, _t, tier: hits[tier]):
            prompt, used = context.build(
                self.d, self._agent(memory=True, knowledge=True), "specific-term", "chat:A")
        self.assertEqual(used["memory"], ["pinned-one", "legacy-one", "note-one"])
        self.assertEqual(used["knowledge"], ["fact-one"])
        self.assertIn("[pinned/pinned-one]", prompt)
        self.assertIn("[legacy-unconfirmed/legacy-one]", prompt)
        self.assertIn("## 领域事实", prompt)

    def test_角色排在第0段之后其余各段之前(self):
        """2026-08-27 起最前面是第 0 段(它在哪儿工作)——role 是「你是谁」,
        它先得知道「你在哪儿」。角色仍在记忆/知识/历史之前。"""
        prompt, _ = context.build(self.d, self._agent(role="你是 SRE 助手"), "问", "chat:A")
        self.assertLess(prompt.index("你在哪儿工作"), prompt.index("你是 SRE 助手"))
        self.assertLess(prompt.index("你是 SRE 助手"), prompt.index("## 现在"))
        # 关掉第 0 段时角色回到最前——退化路径与 botmux 的纯转发一致
        prompt, _ = context.build(self.d, self._agent(role="你是 SRE 助手", routing=False),
                                  "问", "chat:A")
        self.assertTrue(prompt.startswith("你是 SRE 助手"))

    def test_历史按轮次裁不做摘要(self):
        """摘要引入失真,而这一段的价值恰恰是原话。"""
        h = [{"role": "user", "text": f"第{i}句"} for i in range(10)]
        prompt, used = context.build(self.d, self._agent(history=3), "现在这句", "chat:A", history=h)
        self.assertEqual(used["history"], 3)
        self.assertIn("第9句", prompt)
        self.assertNotIn("第6句", prompt)

    def test_历史为_0_时一句不带(self):
        h = [{"role": "user", "text": "以前说的"}]
        prompt, _ = context.build(self.d, self._agent(history=0), "现在", "chat:A", history=h)
        self.assertNotIn("以前说的", prompt)

    def test_检索词能从中英文里抽出来(self):
        t = context._terms("帮我看看 gpubox 上的 kerberos 票据为什么过期了")
        self.assertIn("gpubox", [x.lower() for x in t])
        self.assertTrue(any("票据" in x or "过期" in x for x in t))

    def test_检索词去掉停用词(self):
        self.assertEqual(context._terms("的了是在和与"), [])

    def test_剥掉前后挂着的虚词(self):
        """「上的」这种切出来很常见,剥完只剩一个字就该丢——单字在 curator 里是子串匹配,
        会捞回一大片(实测「上的」命中 11 条,而「gpubox」1 条)。"""
        self.assertNotIn("上的", context._terms("看看 gpubox 上的配置"))

    def test_泛词整个丢掉而不是截前几条(self):
        """截出来的那几条也是随机的。上下文里塞十条不相关的记忆比少一条相关的更糟:
        模型会去解释它们。"""
        many = [{"id": f"n{i}", "where": "body", "path": f"wf/sot/{i}.md", "desc": ""}
                for i in range(context.TERM_TOO_BROAD + 1)]
        with mock.patch.object(context, "_search", return_value=many):
            self.assertEqual(context._pick(self.d, "某个泛词啊", ("sot",), 5), [])
        few = many[:2]
        with mock.patch.object(context, "_search", return_value=few):
            self.assertEqual(len(context._pick(self.d, "某个具体词", ("sot",), 5)), 2)

    def test_解析_curator_输出(self):
        out = ("devtools  (keywords/body)  _desktop/sot/common-kb/glossary/devtools.md\n"
               "    研发协作工具链速查\n"
               "bp-framework  (body)  _desktop/sot/sre-kb/governance/bp-framework.md\n"
               "    BP机制框架\n")
        items = context._parse(out)
        self.assertEqual([i["id"] for i in items], ["devtools", "bp-framework"])
        self.assertEqual(items[0]["desc"], "研发协作工具链速查")

    def test_解析时跳过格式不合的行(self):
        """curator 报错时 stdout 里可能是一句人话。把它当记忆塞进上下文比少几条更糟。"""
        self.assertEqual(context._parse("✗ 不像 curator 实例\n请先 init\n"), [])


class Outbound(unittest.TestCase):
    """红线第二道闸:出站必经 outbound.send。"""

    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.agent = {"id": "b", "lark_app_id": "cli_1", "domain": "feishu",
                      "outbound": {"strict": False, "compliance": True, "sealed": True, "card": True}}

    def test_干净内容照常发出(self):
        with mock.patch("larkbot.larkapi.send_text", return_value={"code": 0, "data": {"message_id": "om_1"}}) as m:
            r = outbound.send(self.d, self.agent, "一切正常", "oc_1")
        self.assertTrue(r["ok"])
        self.assertEqual(m.call_args.args[2], "一切正常")

    def test_命中凭证时原文不进飞书(self):
        with mock.patch("larkbot.larkapi.send_text", return_value={"code": 0, "data": {}}) as m:
            r = outbound.send(self.d, self.agent, "key=ghp_abcdefghijklmnopqrstuvwxyz0123", "oc_1")
        self.assertFalse(r["ok"])
        self.assertTrue(r["held"])
        self.assertNotIn("ghp_", m.call_args.args[2])

    def test_拦下的原文留在本机可取回(self):
        """拦下之后无处可查,人下次就会想办法关掉这道闸。"""
        with mock.patch("larkbot.larkapi.send_text", return_value={"code": 0, "data": {}}):
            outbound.send(self.d, self.agent, "ghp_abcdefghijklmnopqrstuvwxyz0123", "oc_1", anchor="chat:A")
        with open(store.held_path(self.d), encoding="utf-8") as f:
            items = json.load(f)["items"]
        self.assertIn("ghp_", items[-1]["text"])
        self.assertEqual(items[-1]["anchor"], "chat:A")

    def test_长内容走卡片(self):
        long = "字" * (outbound.CARD_THRESHOLD + 1)
        with mock.patch("larkbot.larkapi.send_card", return_value={"code": 0, "data": {"message_id": "om"}}) as c, \
             mock.patch("larkbot.larkapi.send_text") as t:
            outbound.send(self.d, self.agent, long, "oc_1")
        self.assertTrue(c.called)
        self.assertFalse(t.called)

    def test_关了卡片就一律走文本(self):
        a = dict(self.agent, outbound=dict(self.agent["outbound"], card=False))
        with mock.patch("larkbot.larkapi.send_text", return_value={"code": 0, "data": {}}) as t:
            outbound.send(self.d, a, "字" * 1000, "oc_1")
        self.assertTrue(t.called)

    def test_有占位卡片时改它而不是新发(self):
        with mock.patch("larkbot.larkapi.update_card", return_value={"code": 0}) as u, \
             mock.patch("larkbot.larkapi.send_card") as s:
            outbound.send(self.d, self.agent, "定稿内容", "oc_1", card_message_id="om_ph")
        self.assertTrue(u.called)
        self.assertFalse(s.called)

    def test_被拦时也要改掉占位卡片(self):
        """否则那张卡片永远停在「生成中…」。"""
        with mock.patch("larkbot.larkapi.update_card", return_value={"code": 0}) as u:
            r = outbound.send(self.d, self.agent, "ghp_abcdefghijklmnopqrstuvwxyz0123",
                              "oc_1", card_message_id="om_ph")
        self.assertTrue(u.called)
        self.assertFalse(r["ok"])

    def test_飞书返回错误码时如实报告(self):
        with mock.patch("larkbot.larkapi.send_text", return_value={"code": 230002, "msg": "bot not in chat"}):
            r = outbound.send(self.d, self.agent, "在吗", "oc_1")
        self.assertFalse(r["ok"])
        self.assertIn("230002", r["err"])

    def test_节流限制刷新频率(self):
        th = outbound.Throttle(gap=999)
        self.assertTrue(th.ready())
        self.assertFalse(th.ready())
        self.assertTrue(th.ready(force=True))

    def test_流式正文在定稿前不外发(self):
        th = outbound.Throttle(gap=0)
        with mock.patch("larkbot.larkapi.update_card") as update:
            outbound.stream_progress(self.agent, "om_ph", "ghp_abcdefghijklmnopqrstuvwxyz0123", th)
        update.assert_not_called()

    def test_占位卡片不过闸(self):
        """内容是我们自己写死的一句话,不含模型产出。"""
        with mock.patch("larkbot.larkapi.send_card", return_value={"code": 0, "data": {"message_id": "om"}}), \
             mock.patch("larkbot.redline.check") as chk:
            mid, err = outbound.open_placeholder(self.agent, "oc_1")
        self.assertEqual((mid, err), ("om", ""))
        self.assertFalse(chk.called)


class Slash(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        a, err = store.save(self.d, "b1", {"name": "小助手", "engine": "claude"})
        self.assertEqual(err, "")
        self.a = store.load(self.d, "b1")

    def test_非斜杠开头不当命令(self):
        r, done = handler._slash(self.d, self.a, "chat:A", "帮我看看 /tmp 下的文件")
        self.assertFalse(done)
        self.assertIsNone(r)

    def test_clear_清历史(self):
        store.session_append(self.d, "b1", "chat:A", "user", "旧的")
        r, done = handler._slash(self.d, self.a, "chat:A", "/clear")
        self.assertTrue(done)
        self.assertEqual(store.session(self.d, "b1", "chat:A")["turns"], [])

    def test_改配置落盘(self):
        r, done = handler._slash(self.d, self.a, "chat:A", "/history 3")
        self.assertTrue(done)
        self.assertEqual(store.load(self.d, "b1")["context"]["history"], 3)

    def test_引擎只能是认识的那几个(self):
        r, _ = handler._slash(self.d, self.a, "chat:A", "/engine 不存在的")
        self.assertIn("只能是", r)
        self.assertEqual(store.load(self.d, "b1")["engine"], "claude")

    def test_cd_到不存在的目录被挡(self):
        r, _ = handler._slash(self.d, self.a, "chat:A", "/cd /没有这个目录")
        self.assertIn("不存在", r)

    def test_不认识的命令给出提示而不是静默(self):
        r, done = handler._slash(self.d, self.a, "chat:A", "/gogogo")
        self.assertTrue(done)
        self.assertIn("/help", r)

    def test_没有绑工作流这回事(self):
        """bot 是工作台这一层的代理,不属于任何一个工作流:记忆与知识全局检索。

        留这一条是防回归。「只从当前工作流取」看着像个合理的收窄,很容易被重新加回来,
        而它的后果是检索不到本该检索到的东西——群里问什么取决于那个群在聊什么,
        与 Desk 怎么给人分域无关。"""
        r, done = handler._slash(self.d, self.a, "chat:A", "/workflow incident")
        self.assertTrue(done)
        self.assertIn("不认识的命令", r)
        self.assertNotIn("workflow", store.load(self.d, "b1"))

    def test_status_摆出实际用量不只是开关(self):
        """记忆开着而一条也没检索到时实际读到 0 条,但开关仍显示「开」——
        不把用量摆出来,人看不出问题在哪。"""
        store.used_append(self.d, "b1", "chat:A",
                          {"memory": ["mem-x"], "knowledge": [], "history": 2}, "问句")
        r, _ = handler._slash(self.d, self.a, "chat:A", "/status")
        self.assertIn("上一轮实际读到", r)
        self.assertIn("mem-x", r)

    def test_一条都没读到时点出可能的原因(self):
        store.used_append(self.d, "b1", "chat:A", {"memory": [], "knowledge": [], "history": 0}, "问句")
        r, _ = handler._slash(self.d, self.a, "chat:A", "/status")
        self.assertIn("一条都没读到", r)
        self.assertIn("没有能检索的词", r)

    def test_还没对话时说清楚是没有而不是读不到(self):
        r, _ = handler._slash(self.d, self.a, "chat:A", "/status")
        self.assertIn("还没有对话", r)

    def test_help_列得出主要命令(self):
        r, _ = handler._slash(self.d, self.a, "chat:A", "/help")
        for c in ("/clear", "/role", "/engine", "/strict", "/cd"):
            self.assertIn(c, r)


class Dedupe(unittest.TestCase):
    def setUp(self):
        handler._seen.clear()

    def test_同一个_event_id_只处理一次(self):
        """飞书在未收到 ack 或客户端重连时会重推。不去重,一句话会被回答两遍,
        而且因为每轮都重新组装上下文,两遍的答案还未必一样。"""
        self.assertFalse(handler._dedupe("evt_1"))
        self.assertTrue(handler._dedupe("evt_1"))

    def test_空_id_不参与去重(self):
        self.assertFalse(handler._dedupe(""))
        self.assertFalse(handler._dedupe(""))


class TextOf(unittest.TestCase):
    def test_纯文本(self):
        self.assertEqual(handler._text_of(
            {"message_type": "text", "content": '{"text":"你好"}'}), "你好")

    def test_非文本类型给占位描述(self):
        """模型至少该知道「这里有一张图」,而不是收到一段空白。"""
        self.assertEqual(handler._text_of({"message_type": "image", "content": "{}"}), "[图片]")
        self.assertIn("报告.pdf", handler._text_of(
            {"message_type": "file", "content": '{"file_name":"报告.pdf"}'}))

    def test_富文本拼成纯文本(self):
        c = json.dumps({"content": [[{"tag": "text", "text": "看这个"},
                                     {"tag": "a", "text": "链接", "href": "http://x"}]]})
        got = handler._text_of({"message_type": "post", "content": c})
        self.assertIn("看这个", got)
        self.assertIn("http://x", got)

    def test_内容不是合法_json_时不炸(self):
        self.assertEqual(handler._text_of({"message_type": "text", "content": "不是json"}), "")

    def test_去掉_at_占位符(self):
        """留着会让模型以为对方在提一个叫 _user_1 的人。"""
        self.assertEqual(handler._strip_mentions("@_user_1 帮我看看"), "帮我看看")


if __name__ == "__main__":
    unittest.main()


class Perf(unittest.TestCase):
    """延迟:我们这一侧贡献了多少,以及不许回退到串行。

    2026-08-27 实测账本(528 条记忆库):上下文组装 5.9 秒、引擎推理 6.8 秒、
    飞书往返几十毫秒。也就是说我们自己占了将近一半,而那一半几乎全是可省的——
    六次 mem_read 子进程串行排队,彼此无关。并发后 5.9 → 1.2 秒。
    """

    def setUp(self):
        self.d = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.d, ".memory"), exist_ok=True)

    def test_检索是并发的不是串行(self):
        """串行下墙钟是各次之和,并发下收敛到最慢的那次。用可测的睡眠证明,
        不靠读代码:回退成 for 循环时这条会红。"""
        import time as _t
        calls = []

        def slow(_root, term, tier, timeout=12):
            calls.append((term, tier))
            _t.sleep(0.25)
            return []

        with mock.patch.object(context, "_search", side_effect=slow):
            t0 = _t.time()
            context._pick(self.d, "desktron gpubox 事故 值班", context.TIERS, 5)
            spent = _t.time() - t0
        self.assertGreaterEqual(len(calls), 4, "词×层应当都发出去")
        # 串行至少是 0.25×次数;并发下接近单次
        self.assertLess(spent, 0.25 * len(calls) * 0.6, f"看着还是串行:{spent:.2f}s / {len(calls)} 次")

    def test_并发不改结果顺序(self):
        """顺序是相关性的粗排(长词优先),乱序会让 cap 截出来的那几条变随机。"""
        table = {
            ("长的那个检索词", "sot"): [{"id": "a1", "where": "", "path": "x/a.md", "desc": ""}],
            ("短词", "sot"): [{"id": "b1", "where": "", "path": "x/b.md", "desc": ""}],
        }

        def fake(_root, term, tier, timeout=12):
            return list(table.get((term, tier)) or [])

        with mock.patch.object(context, "_search", side_effect=fake):
            got = context._pick(self.d, "长的那个检索词 短词", ("sot",), 5)
        self.assertEqual([i["id"] for i in got], ["a1", "b1"])

    def test_状态文件一轮只读写一次(self):
        """此前 session_append / session / used_append 各自 load+save,一轮六次。
        合成一次不只省 IO——三次写之间若有别的线程插入,后写的会覆盖前面的。"""
        sess = store.turn_and_used(self.d, "b1", "chat:A", "user", "问一句",
                                   {"memory": ["m1"], "knowledge": [], "history": 0}, "问一句")
        self.assertEqual(len(sess["turns"]), 1)
        self.assertEqual(store.used_recent(self.d, "b1", "chat:A", 1)[0]["memory"], ["m1"])

    def test_状态缓存按_stat_校验不会读到陈旧的(self):
        """别的进程改过(跨侧转发、手工编辑)就要重读。"""
        store.turn_and_used(self.d, "b1", "chat:A", "user", "第一句")
        p = os.path.join(store.dir_of(self.d), "_state.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"sessions": {"b1|chat:A": {"turns": [{"role": "user", "text": "外部改的"}]}}}, f)
        got = store.session(self.d, "b1", "chat:A")
        self.assertEqual(got["turns"][0]["text"], "外部改的")
