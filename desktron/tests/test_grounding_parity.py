"""多引擎系统注入的一致性回归(2026-08-24)。

盯这几件事,都是同一次盘点里查出、当场核实、当场修的:
1. Desk 业务角色只认可选的 desk_role；缺省不注入角色正文,旧 agent 字段不能污染
   Claude/Codex,但 style/channel/intent 约束仍须保留。
2. 意图注释约定曾经"说已经写进 common/orientation.md",核对后其实没有——这里锁住,
   防止同一句承诺再次落空。
3. codex 家族的系统约定同样没到:它只在 build_cmd 的 seed 里,而那条只服务 exec 退路,
   主路走 app-server。
4. 重建历史时 Desk 前置的两段(系统约定 seed、当前上下文)要摘掉,而不是把整条用户消息丢掉。
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea
    from grounding import builder as grounding_builder
finally:
    sys.argv = _argv


class _Bus:
    def publish(self, *a, **k):
        pass


def _adapter(cls):
    return cls(tempfile.mkdtemp(), ROOT, _Bus())


class DeskRoleDefinitions(unittest.TestCase):
    def test_退役角色文件保持删除(self):
        self.assertFalse(os.path.lexists(os.path.join(ROOT, ".desk", "agents", "diagnose.md")))

    def test_ops_写动作要人批准(self):
        with open(os.path.join(ROOT, ".desk", "agents", "ops.md"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("edit: ask", src)
        self.assertIn("mode: primary", src)


class ChannelRule(unittest.TestCase):
    """通道约定必须两侧都在(2026-09-09)。

    写进 final_answer 的那一句同时就是回合的收尾。这条约定原先只写了「该让用户处理的
    内容不能只放在 commentary」,把内容一路往 final_answer 推,却没说什么不该写在那里,
    于是反向的故障照发:模型把「我会先读取这两份文档」当成终答,回合三秒收尾、零工具调用,
    界面上是莫名停下来。两侧各锁一条,防止其中一半在后续改动里被抹掉。
    """

    def test_工作未完成时不收尾(self):
        rule = ea.CodexAdapter.CHANNEL_RULE
        self.assertIn("工作未完成时继续执行", rule)
        self.assertIn("不要用计划或进度收尾", rule)
        self.assertIn("`final_answer` 会结束本回合", rule)
        self.assertNotIn("说明进展和下一步", rule)

    def test_需要用户处理的内容进入正文(self):
        rule = ea.CodexAdapter.CHANNEL_RULE
        for word in ("执行", "确认", "补充"):
            self.assertIn(word, rule)
        self.assertIn("不得只写在 `commentary`", rule)

class DeskRoleInjection(unittest.TestCase):
    def test_缺省无角色但_codex_通用约束仍在(self):
        ad = _adapter(ea.CodexAdapter)
        body = ad.system_body({})
        self.assertNotIn("运维工单处理主 agent", body)
        self.assertIn(ad.style_body()[:12], body)
        self.assertIn(ad.CHANNEL_RULE[:24], body)
        self.assertIn(ad.INTENT_RULE[:24], body)

    def test_显式_desk_role_ops_才给订阅引擎注入(self):
        for cls in (ea.ClaudeAdapter, ea.CodexAdapter):
            ad = _adapter(cls)
            self.assertNotIn("运维工单处理主 agent", ad.system_body({}), cls.__name__)
            self.assertNotIn("运维工单处理主 agent",
                             ad.system_body({"agent": "ops"}), cls.__name__)
            self.assertIn("运维工单处理主 agent",
                          ad.system_body({"desk_role": "ops"}), cls.__name__)

    def test_退役角色名安全降级为空(self):
        ad = _adapter(ea.ClaudeAdapter)
        self.assertEqual(ad.system_body({"desk_role": "diagnose"}), ad.style_body())

    def test_desktop_新会话不继承旧_agent(self):
        ad = _adapter(ea.ClaudeAdapter)
        sid = ad.create_session("_desktop")["id"]
        # 引擎全局默认里即使残留过 agent / desk_role,desktop 也必须是通用主 agent。
        opts = ad.session_options(sid, {"agent": "diagnose", "desk_role": "ops"})
        self.assertNotIn("agent", opts)
        self.assertNotIn("desk_role", opts)
        self.assertNotIn("运维工单处理主 agent", ad.system_body(opts))

    def test_工作流角色可继承且显式空值可关闭(self):
        ad = _adapter(ea.ClaudeAdapter)
        # session_options 在调用期才导入 domains；放入最小替身可避免测试进程的 argv 与
        # 其他模块级桩影响本用例,同时保留对这条延迟查询的真实覆盖。
        domains_mod = types.ModuleType("domains")
        domains_mod._domain_entry = lambda _did: {"desk_role": "ops"}
        with mock.patch.dict(sys.modules, {"domains": domains_mod}):
            inherited = ad.create_session("incident")["id"]
            neutral = ad.create_session("incident", opts={"desk_role": ""})["id"]
            self.assertEqual(ad.session_options(inherited).get("desk_role"), "ops")
            self.assertEqual(ad.session_options(neutral).get("desk_role"), "")


class OrientationCarriesIntentRule(unittest.TestCase):
    """公共说明要讲清命令目的写在哪里。"""

    def test_命令目的写法在文件里(self):
        with open(os.path.join(ROOT, "common", "orientation.md"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn("说明目的", src)
        self.assertIn("第一行", src)
        self.assertRegex(src, r"description[^\n]*(字段|写入)")


class EngineSkillIndexInjection(unittest.TestCase):
    def _sections(self, engine):
        calls = []
        fake_domains = types.SimpleNamespace(
            _grounding_index=lambda *_a: "",
            _skill_index=lambda sections=None: calls.append(sections) or "技能索引",
        )
        with mock.patch.object(grounding_builder, "_domains", return_value=fake_domains), \
             mock.patch.object(grounding_builder, "_read", return_value=""):
            grounding_builder.constant(ROOT, "desk-iter", engine)
        return calls

    def test_all_engines_use_on_demand_skill_discovery(self):
        for engine in ("claude", "shepherd:claude", "pi", "codex", "shepherd", ""):
            self.assertEqual(self._sections(engine), [], engine)

    def test_shepherd_grounding_follows_producer_engine(self):
        cases = {"claude": "shepherd:claude", "pi": "pi", "codex": "codex", "": ""}
        for producer, expected in cases.items():
            facade = types.SimpleNamespace(session_engine=lambda _sid, p=producer: p)
            self.assertEqual(ea.ShepherdAdapter.grounding_engine(facade, "s"), expected, producer)

    def test_plain_adapter_grounding_uses_its_own_name(self):
        for cls in (ea.ClaudeAdapter, ea.CodexAdapter, ea.PiAdapter):
            self.assertEqual(_adapter(cls).grounding_engine("s"), cls.name, cls.__name__)


class CodexFamilySeed(unittest.TestCase):
    """系统约定要真的送到 codex 家族,且只在首轮、不与 system_body 重复。"""

    OPTS = {"_grounding": "【系统约定标记】红线与判断原则"}

    def test_系统约定进首轮_seed(self):
        for cls in (ea.CodexAdapter,):
            ad = _adapter(cls)
            extra = ad._seed_extra(self.OPTS)
            self.assertIn("系统约定标记", extra, cls.__name__)

    def test_不与_system_body_重复(self):
        """app-server 那条路 system_body 每轮都发,seed 里再放一份就是同一段说两遍。"""
        ad = _adapter(ea.CodexAdapter)
        extra = ad._seed_extra(self.OPTS)
        self.assertNotIn(ad.style_body()[:12], extra)

    def test_exec_退路仍然自带_system_body(self):
        """退路没有独立的系统提示通道,那一条要完整。"""
        ad = _adapter(ea.CodexAdapter)
        cmd = ad.build_cmd("用户原话", "", self.OPTS)
        config = next(value for value in cmd if value.startswith('developer_instructions='))
        self.assertIn(ad.style_body()[:12].replace('\n', '\\n'), config)
        self.assertIn("系统约定标记", config)
        self.assertEqual(cmd[-1], "用户原话")

    def test_续轮不再重发(self):
        ad = _adapter(ea.CodexAdapter)
        self.assertEqual(ad.build_cmd("继续", "thr_1", self.OPTS)[-1], "继续")

    def test_codex_delta_指向按需查找而非已删除的清单(self):
        self.assertIn("查找操作说明", ea.CODEX_DELTA)
        self.assertNotIn("可用技能", ea.CODEX_DELTA)

class RebuildStripsInjections(unittest.TestCase):
    """重建历史时摘掉 Desk 前置的两段,别把用户那句话一起丢了。"""

    def _item(self, text):
        return {"type": "message", "role": "user", "content": [{"type": "text", "text": text}]}

    def setUp(self):
        self.ad = _adapter(ea.CodexAdapter)
        self.seed = ea.CodexAdapter.SEED_HEAD + "种子" + ea.CodexAdapter.SEED_SEP
        self.scene = "【当前上下文】" + ea.SCENE_SEP

    def test_两段都摘掉后剩用户原话(self):
        got = self.ad._rollout_user_text(self._item(self.seed + self.scene + "帮我看看"))
        self.assertEqual(got, "帮我看看")

    def test_只有当前上下文也摘(self):
        self.assertEqual(self.ad._rollout_user_text(self._item(self.scene + "帮我看看")), "帮我看看")

    def test_旧版上下文也摘(self):
        old_scene = "【旧版上下文】" + ea.SCENE_SEPS[1]
        self.assertEqual(self.ad._rollout_user_text(self._item(old_scene + "帮我看看")), "帮我看看")

    def test_只有_seed_也不丢原话(self):
        """原来命中 SEED_HEAD 直接返回空串,首次提问在重建出来的历史里凭空消失。"""
        self.assertEqual(self.ad._rollout_user_text(self._item(self.seed + "帮我看看")), "帮我看看")

    def test_没有前置的原样返回(self):
        self.assertEqual(self.ad._rollout_user_text(self._item("帮我看看")), "帮我看看")


if __name__ == "__main__":
    unittest.main()
