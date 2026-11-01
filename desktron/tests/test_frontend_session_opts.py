"""档位是会话级的,引擎级那份只做新会话的起点。

2026-08-16 实见:claude 的 effort 像是全局的——在一段会话里调成 max,所有 claude 会话都成了
max。查下来是前端分了两支:codex 家族先写 `/session/<id>/settings` 再同步一份引擎级,
而 claude 只写引擎级,读的时候也直接读引擎级,于是每段会话读的都是同一个值。

后端本来就是会话级语义:发消息取的是 `session_options(sid, engine_opts.<引擎>)`——会话自己
存过就用自己的,没存过回落到引擎级那一份。「新会话按用户最后一次设置起」由这条回落天然成立,
不需要另做一套默认值。所以要改的只是前端那一支:一律两处都写、一律读会话级。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
UI_SERVER = _srcmap.ui_server_src()
ADAPTERS = _srcmap.engine_adapters_src()


def fn(src, name):
    m = re.search(r"\n(?:async )?function %s\(.*?\n}" % re.escape(name), src, re.S)
    assert m, "%s 不见了" % name
    return m.group(0)


class WriteBothLevels(unittest.TestCase):
    def test_不分引擎_两处都写(self):
        seg = fn(CHAT, "hsSetOpt")
        self.assertIn("/settings`", seg)
        self.assertIn("/workbench/engine", seg)
        # 分支没了:原来 claude 走 else,只写引擎级
        self.assertNotIn("hsIsCodexLike", seg)

    def test_引擎级按引擎名写键(self):
        # 不同引擎的模型清单、登录各是一套,混进同一个键会让在一侧选的模型名
        # 被另一侧拿去启动
        seg = fn(CHAT, "hsSetOpt")
        self.assertIn("{ [eng]: engineDefaults }", seg)
        self.assertNotIn('{ codex:', seg)

    def test_desk_role_不写进引擎全局默认(self):
        seg = fn(CHAT, "hsSetOpt")
        self.assertIn('for (const key of ["desk_role", "shepherd_mode", "shepherd_trial", "active_engine"])', seg)
        self.assertIn("delete engineDefaults[key]", seg)

    def test_会话级先写_引擎级拿合并后的结果(self):
        seg = fn(CHAT, "hsSetOpt")
        self.assertLess(seg.index("/settings`"), seg.index("/workbench/engine"))
        self.assertRegex(seg, r"if \(!v\.shepherdMode \|\| v\.engine === eng\) \{\s+v\.sessionOpts = out\.opts;\s+v\.opts = out\.opts;")
        self.assertIn("v.shepherdEngineState[eng]", seg,
                      "迟到响应只能更新请求所属引擎，不能覆盖当前选择")


class ReadSessionLevel(unittest.TestCase):
    def test_一律读会话级(self):
        """档位一律取会话自己的那份,不按引擎分流。

        原来断言的是 `v.sessionOpts !== null ? v.sessionOpts` 这个字面写法。
        2026-08-25 那一行改成了「非 null 且非 undefined」——只判 !== null 时,
        /config 失败留下的 undefined 会被当成「有会话档位」,v.opts 因此成了 undefined:
        人看到的是 auto mode 变 manual、Opus 5 变「默认」,像设置被清了;
        再往下一行 o.effort 直接抛,整段初始化断在半路。
        断言跟着改成「读的是 sessionOpts、且不按引擎分流」这个意图。
        """
        seg = fn(CHAT, "hsOptsPaint")   # 2026-08-25 从 hsEngineSync 抽出,行为不变
        self.assertIn("v.sessionOpts", seg)
        i = seg.index("? v.sessionOpts")
        self.assertNotIn("hsIsCodexLike", seg[i - 200:i + 200])
        self.assertIn("v.sessionOpts !== undefined", seg,
                      "只判 !== null 的话,没拿到档位那一路会被当成「有」")


class BackendAlreadySessionScoped(unittest.TestCase):
    def test_发消息按会话取_回落引擎级(self):
        self.assertIn("ad.execution_options(seg[2], legacy)", UI_SERVER)
        self.assertIn("else ad.session_options(seg[2], legacy)", UI_SERVER)
        self.assertIn('legacy = _wb_state_read().get("engine_opts", {}).get(', UI_SERVER)

    def test_回落语义写在适配器里(self):
        m = re.search(r"def session_options\(self, sid, fallback=None\):[\s\S]+?\n    def normalize_options",
                      ADAPTERS)
        self.assertIsNotNone(m)
        self.assertIn("if isinstance(saved, dict)", m.group(0))
        self.assertIn("inherited = dict(fallback or {})", m.group(0))
        self.assertIn("raw.update(saved)", m.group(0),
                      "会话已存键必须覆盖引擎默认，缺失键才允许回落")
        self.assertIn("out = self.normalize_options(raw)", m.group(0))
        # 会话没显式写 desk_role 才能继承工作流；显式空串必须保留,供无角色接续使用。
        self.assertIn('"desk_role" not in raw', m.group(0))


if __name__ == "__main__":
    unittest.main()
