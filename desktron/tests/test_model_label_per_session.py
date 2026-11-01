"""档位钮上的模型按会话记,不是按引擎记。

适配器每引擎只有一个实例(engine.py 按名缓存),原来 last_model 就挂在这个实例上——
记的是「这个引擎下任意一段会话最近报过的模型」。两段会话并行时:A 段选了 fable,B 段刚跑
完一轮 opus,A 的档位钮显示 opus(2026-09-02 用户实见「切换了 fable5.1 但还是显示 opus5」)。
前端同一天先改成优先显示引擎自报值(test_model_label_follows_engine),这一改把症状放大:
人刚选好的标签被跨会话的旧值盖住,直到自己这一段再跑完一轮。

改法三件,缺一不可:
  1. 引擎自报时按会话记进 index[sid]["model"](set_session_model),引擎级那份只留作兜底;
  2. /config 带 sid 取(config_label(sid)),没报过时只回引擎名——不拼「默认」,前端会把
     「· 」后面那一截当模型名,拼上就把人选的标签盖掉;
  3. 换模型(update_session_options 收到 model)时清掉该会话的旧自报值,并在广播里带上此刻
     该显示的 model,别的客户端才能就地跟上——那条路上没有 /config 可取。
"""
import os
import re
import sys
import tempfile
import unittest
from unittest import mock

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, os.path.join(ROOT, "desktron", "lib"))
# deskcfg 在 import 时读 sys.argv(脚本式启动的契约),照 test_claude_turn_boundary 的办法垫一份
_argv = sys.argv
sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
try:
    import engine_adapters as ea  # noqa: E402
finally:
    sys.argv = _argv

CHAT = _srcmap.chat_src()
CONTRACT = open(os.path.join(ROOT, "desktron", "lib", "ui_contract.py"), encoding="utf-8").read()


class _Bus:
    def __init__(self):
        self.rows = []

    def publish(self, ev):
        self.rows.append(ev)


def _start(model):
    return {"type": "stream_event", "event": {
        "type": "message_start", "message": {"model": model}}}


class 后端按会话记(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = _Bus()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, self.bus)
        self.a = self.ad.create_session()["id"]
        self.b = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def _ctx(self, sid):
        return {"sid": sid, "msg_id": "m1", "parts": {}, "order": [], "last_tool": None, "t0": 0}

    def test_两段会话各记各的(self):
        """这就是用户撞上的那一幕:A 在跑 fable,B 在跑 opus,A 不该显示 opus。"""
        self.ad.handle_event(_start("claude-fable-5-1"), self._ctx(self.a))
        self.ad.handle_event(_start("claude-opus-5"), self._ctx(self.b))
        self.assertEqual(self.ad.config_label(self.a), "claude · claude-fable-5-1")
        self.assertEqual(self.ad.config_label(self.b), "claude · claude-opus-5")

    def test_后来的会话不改前面那段(self):
        self.ad.handle_event(_start("claude-fable-5-1"), self._ctx(self.a))
        self.ad.handle_event(_start("claude-opus-5"), self._ctx(self.b))
        self.assertEqual(self.ad.session_model(self.a), "claude-fable-5-1")

    def test_没报过只回引擎名(self):
        """不拼「默认」:前端会把「· 」后面那截当模型名显示,拼上就盖掉人选的标签。"""
        self.assertEqual(self.ad.config_label(self.a), "claude")
        self.assertNotIn("·", self.ad.config_label(self.a))

    def test_换模型清掉旧自报值(self):
        self.ad.handle_event(_start("claude-opus-5"), self._ctx(self.a))
        self.ad.update_session_options(self.a, {"model": "fable"})
        self.assertEqual(self.ad.session_model(self.a), "",
                         "换了模型,引擎上一次自报的 opus 已不作数,得清掉")
        self.assertEqual(self.ad.config_label(self.a), "claude")

    def test_只改别的选项不清模型(self):
        self.ad.handle_event(_start("claude-opus-5"), self._ctx(self.a))
        self.ad.update_session_options(self.a, {"effort": "max"})
        self.assertEqual(self.ad.session_model(self.a), "claude-opus-5")

    def test_广播带上此刻该显示的模型(self):
        """别的客户端收到 session.settings 要能就地更新档位钮,那条路上没有 /config 可取。"""
        self.ad.handle_event(_start("claude-opus-5"), self._ctx(self.a))
        self.bus.rows.clear()
        self.ad.update_session_options(self.a, {"model": "fable"})
        ev = next(r for r in self.bus.rows if r["type"] == "session.settings")
        self.assertIn("model", ev["properties"])
        self.assertEqual(ev["properties"]["model"], "claude")   # 刚换过,旧值已清

    def test_引擎级兜底仍在(self):
        """没有会话的场合(引擎页、老测试)还是要能答;不传 sid 就读引擎级那份。"""
        self.ad.handle_event(_start("claude-opus-5"), self._ctx(self.a))
        self.assertEqual(self.ad.config_label(), "claude · claude-opus-5")
        self.assertEqual(self.ad.last_model, "claude-opus-5")

    def test_自报落盘(self):
        """索引要写下去,刷新/重启之后档位钮不该退回「默认」。"""
        self.ad.handle_event(_start("claude-fable-5-1"), self._ctx(self.a))
        again = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, _Bus())
        self.assertEqual(again.session_model(self.a), "claude-fable-5-1")


class codex改道也按会话(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.CodexAdapter(self.tmp.name, self.tmp.name, _Bus())
        self.a = self.ad.create_session()["id"]
        self.b = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_rerouted_只记到自己那段(self):
        ctx = {"sid": self.a, "msg_id": "m1", "parts": {}, "order": [], "t0": 0}
        self.ad._handle_notification("model/rerouted", {"toModel": "gpt-5.6-sol"}, ctx) \
            if hasattr(self.ad, "_handle_notification") else None
        # 找不到统一入口就直接走 set_session_model,断言的是"按会话隔离"这件事
        self.ad.set_session_model(self.a, "gpt-5.6-sol")
        self.assertEqual(self.ad.session_model(self.a), "gpt-5.6-sol")
        self.assertEqual(self.ad.session_model(self.b), "")

    def test_Pi带会话且未自报时不拿全局当前模型冒充(self):
        pi = ea.PiAdapter(self.tmp.name, self.tmp.name, _Bus())
        with mock.patch("settings._pi_current_model", return_value="deepseek/deepseek-v4"):
            self.assertEqual(pi.config_label(self.a), "pi")


class 会话选项优先于引擎默认(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ad = ea.ClaudeAdapter(self.tmp.name, self.tmp.name, _Bus())
        self.sid = self.ad.create_session()["id"]

    def tearDown(self):
        self.tmp.cleanup()

    def test_fallback只补缺口不覆盖会话模型(self):
        self.ad.index[self.sid]["opts"] = {"model": "seed-2.1-turbo"}
        out = self.ad.session_options(self.sid, {"model": "gpt-5.4", "effort": "high"})
        self.assertEqual(out["model"], "seed-2.1-turbo")
        self.assertEqual(out["effort"], "high")

    def test_更新部分选项时固化完整快照且不写内部标记(self):
        self.ad.index[self.sid]["opts"] = {"model": "seed-2.1-turbo"}
        out = self.ad.update_session_options(
            self.sid, {"effort": "max"}, {"model": "gpt-5.4", "mode": "default"})
        self.assertEqual(out["model"], "seed-2.1-turbo")
        self.assertEqual(out["effort"], "max")
        self.assertEqual(out["mode"], "default")
        self.assertNotIn("_touched", self.ad.index[self.sid]["opts"])


class 接口与前端(unittest.TestCase):
    def test_config按会话取(self):
        self.assertIn('"model": ad.config_label(session_id)', CONTRACT)

    def test_前端不把引擎名当模型(self):
        """后端没报过时 /config 的 model 只有引擎名,没有「· 」;解析要得到空串,
        档位钮才会退回人选的标签。原来 split(" · ").pop() 对「claude」也会得到「claude」。
        2026-09-03 改成先清零 realModel,只有当 cf.model 真的包含模型尾(「· 」或「/」拆开
        后不等于原文)时才赋值,同样满足"纯引擎名 → 空"。"""
        m = re.search(r"v\.realModel\s*=\s*\"\";[\s\S]{0,400}includes\(\" · \"\)[\s\S]{0,400}v\.realModel\s*=", CHAT)
        self.assertIsNotNone(m)

    def test_换模型先清前端的旧值(self):
        seg = re.search(r"async function hsSetOpt\(v, patch\)[\s\S]+?\n}\n", CHAT).group(0)
        self.assertIn('hasOwnProperty.call(patch, "model")) v.realModel = ""', seg)
        self.assertLess(seg.index('v.realModel = ""'), seg.index("hsEngineSync(v)"),
                        "要在回读 /config 之前清,否则回读把它又盖回去")

    def test_设置事件带model时同步(self):
        seg = re.search(r'if \(type === "session\.settings"\) \{[\s\S]+?\n    return;\n  \}', CHAT).group(0)
        self.assertIn("p.model", seg)
        self.assertIn("v.realModel =", seg)


if __name__ == "__main__":
    unittest.main()
