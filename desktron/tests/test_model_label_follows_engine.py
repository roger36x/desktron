"""档位钮显示「引擎实际在跑哪个」,不是「Desk 让它跑哪个」(2026-09-02 用户指出)。

两者会分开,三种情形:
  · 选「默认」——opts 里没有 model,本来就只能靠引擎自报;
  · 用 /model 换模型——那是 CLI 原生命令,改的是引擎自己的会话状态,Desk 的 opts
    一个字没动,也不会发 session.settings。只认 opts 就永远停在旧值;
  · 别名跟着上游滚版本——传的是族名 fable,实测已从 claude-fable-5 滚到 -5-1,
    静态标签追不上。

后端早就在记(claude.py 的 last_model ← 引擎每条消息自报的 model),经 config_label
进 /config;缺的只是前端优先用它,以及回合结束后去取一次。

同一天的后半段又查出上面这套还漏了一层:last_model 挂在适配器实例上,而适配器每引擎
只有一个(engine.py 按名缓存),记的是「这个引擎下任意一段会话最近报过的模型」——A 段选了
fable,B 段刚跑完一轮 opus,A 的档位钮就显示 opus。且前端一旦优先用它,人刚选好的标签
反而被这个跨会话的旧值盖住。现在按会话记(index[sid]["model"]),/config 带 sid 取,
换模型时把该会话的旧值清掉;详见 test_model_label_per_session.py。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


OPTS = _read("frontend/app-chat-opts.js")
CORE = _read("frontend/app-chat-core.js")


class 真实模型优先(unittest.TestCase):
    def test_档位钮先用引擎自报的(self):
        """写成 `md ? md.label : realModel` 的话,只要 opts 里有 model 就用静态标签,
        /model 换过之后界面不会跟——那正是用户报的现象。"""
        m = re.search(r"const modelText = ([^;]+);", OPTS)
        self.assertIsNotNone(m, "档位钮那一行不见了")
        expr = m.group(1)
        # 要守的是「不能退回 opts 优先」,不是「realModel 必须写在第一个」——
        # 2026-09-02 前面合理地多了一档 modelPick(刚选完还没跑时以人的选择为准)。
        # 判据:realModel 要排在静态标签 md.label 之前。
        self.assertIn("v.realModel", expr)
        self.assertLess(expr.index("v.realModel"), expr.index("md.label"),
                        "realModel 要排在静态标签之前,实际是:" + expr)

    def test_拿不到时仍退回静态标签(self):
        """/config 没回来、或引擎还没报过 model 时,不能显示空白。"""
        m = re.search(r"const modelText = ([^;]+);", OPTS)
        self.assertIn("md.label", m.group(1))

    def test_realModel_来自引擎自报(self):
        """不是前端猜的:/config 的 model 由后端 config_label 给,值是 last_model。
        前端先清空 realModel,只有 cf.model 含模型尾(「· 」或「/」拆开后 != 原文)
        时才赋值;只含引擎名就保留人选,避免刚换完模型被旧自报盖住(2026-09-03)。"""
        self.assertRegex(OPTS, r"v\.realModel\s*=\s*\"\";[\s\S]{0,400}hsPrettyModel\(cf\.model\)")
        contract = _read("desktron/lib/ui_contract.py")
        self.assertIn('"model": ad.config_label(session_id)', contract)
        store = _read("desktron/lib/engine_adapters/cli_store.py")
        self.assertIn("last_model", store)


class 换模型后要跟上(unittest.TestCase):
    def test_回合收尾核一次档位(self):
        """/model 不经 Desk,没有任何事件通知。回合收尾是最早能知道真实模型的时刻——
        引擎在这一轮的消息里报过 model。不核就得等人切一次页。"""
        m = re.search(r"hsGoalSync\(v\); hsAgentsSync\(v\); hsTasksSync\(v\);(.*?)\n\}",
                      CORE, re.S)
        self.assertIsNotNone(m)
        self.assertIn("hsEngineSync(v)", m.group(1))


class 刚选的模型不被陈旧值盖掉(unittest.TestCase):
    """引擎自报的是「上一次跑时用的」。刚在菜单里选完还没发消息,自报值仍是旧的
    (session_model 的 docstring 写着「刚换过模型还没跑」那一档),直接显示它就盖掉了
    人刚做的选择——菜单圆点在新模型,钮上却写着旧模型(2026-09-02 用户实见,
    会话实测 opts.model 已是新值而后端仍报旧模型)。

    判据不是「谁优先」,是「谁更新」:选的那一刻记下当时的自报值(modelPickBase),
    在它变化之前以选择为准;引擎一报出新的就说明这一档真跑起来了,标记作废。
    这样 /model 那条路不受影响——它不经菜单,没有 modelPick。
    """

    def test_菜单点选时记下选择与基线(self):
        seg = OPTS[OPTS.index("function hsModelItem("):]
        seg = seg[:seg.index("\n}\n")]
        self.assertIn("v.modelPick = m.label", seg)
        self.assertIn("v.modelPickBase = v.realModel", seg, "不记基线就不知道何时该作废")

    def test_显示时选择优先于陈旧自报(self):
        m = re.search(r"const modelText = ([^;]+);", OPTS)
        self.assertTrue(m.group(1).strip().startswith("v.modelPick"),
                        "modelPick 要排最前,实际:" + m.group(1))

    def test_引擎报出新值后标记作废(self):
        """不作废的话,之后用 /model 换模型就再也跟不上了——一次选择永久钉住显示。
        判据在 2026-09-03 收紧:只有 realModel 非空且与基线真的不等时才作废,
        否则换模型后端清 realModel 这一步会把人选也误清。"""
        self.assertRegex(OPTS,
            r"if \(v\.modelPick && v\.realModel && v\.realModel !== v\.modelPickBase\)")
        at = OPTS.index("v.realModel !== v.modelPickBase")
        tail = OPTS[at:at + 240]
        self.assertIn('v.modelPick = ""', tail)

    def test_作废判定排在显示之前(self):
        """先判后显示,否则作废要等下一次重绘才生效,中间那一帧仍是旧的。"""
        self.assertLess(OPTS.index("v.realModel !== v.modelPickBase"),
                        OPTS.index("const modelText = v.modelPick"))


class 别名标签(unittest.TestCase):
    def test_fable_标到_5_1(self):
        m = re.search(r'\{ id: "fable", label: "([^"]+)" \}', OPTS)
        self.assertIsNotNone(m)
        self.assertEqual(m.group(1), "Fable 5.1")

    def test_传的仍是族名别名(self):
        """别名跟着上游滚版本,写死全名的话每次换代都要改这里。
        标签是给人选之前看的,传参是给引擎的,两者不必一致。"""
        self.assertIn('{ id: "fable"', OPTS)
        self.assertNotIn('id: "claude-fable', OPTS)


if __name__ == "__main__":
    unittest.main()
