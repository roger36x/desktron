"""档位的形状由引擎自报,前端不按引擎名分流。

同一个「模式」在各引擎里叫的键不同:claude 的权限档存 mode,codex 家族的协作档存
collaboration_mode。前端原来靠 hsIsCodexLike 判断,于是每接一家新引擎都要回 app-chat.js
改三处(模式清单、模式字样、切档菜单),再加一处 slash 过滤——2026-08-17 接 pi 时正是这样:
它自报了模型清单却用不上,因为那条取值路径也挂在同一个家族判断上。

改法是把这几件事变成引擎自报的一份声明(options_catalog().ui),前端只读声明。
这里守两侧的口径:后端每个适配器都带上契约,前端不再有按引擎名走的档位分支。
"""
import os
import re
import sys
import tempfile
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "desktron", "lib")
CHAT = _srcmap.chat_src()


def _adapters():
    sys.path.insert(0, LIB)
    # deskcfg 在导入时读 argv(前端目录与两个端口),照 test_pi_engine 的口径临时换掉再还原:
    # pytest 自己的 argv 里第三项是测试文件路径,不还原就会被当成端口去 int()
    argv = sys.argv
    sys.argv = ["ui_server.py", os.path.join(ROOT, "frontend"), "7999", "7998", ROOT]
    try:
        import engine_adapters as ea
        # Pi 的目录按需导入 settings；也必须在这段受控 argv 内预载，不能等还原后再导。
        import settings  # noqa: F401
    finally:
        sys.argv = argv
    d = tempfile.mkdtemp()
    return [cls(d, ROOT, ea.EventBus())
            for cls in (ea.ClaudeAdapter, ea.CodexAdapter, ea.PiAdapter)]


class BackendDeclares(unittest.TestCase):
    def test_每个引擎都实现会话全文搜索契约(self):
        for ad in _adapters():
            self.assertTrue(ad.search_supported, "%s 未声明搜索能力" % ad.name)
            self.assertTrue(callable(ad.search_source_path))
            self.assertTrue(callable(ad.search_documents))

    def test_每个引擎的目录里都有契约(self):
        for ad in _adapters():
            ui = (ad.options_catalog() or {}).get("ui")
            self.assertIsInstance(ui, dict, "%s 的 options_catalog 没带 ui" % ad.name)
            self.assertTrue(ui.get("modeKey"), "%s 没声明模式键" % ad.name)

    def test_codex家族的模式键与claude不同(self):
        keys = {ad.name: ad.options_catalog()["ui"]["modeKey"] for ad in _adapters()}
        self.assertEqual(keys["claude"], "mode")
        self.assertEqual(keys["codex"], "collaboration_mode")
        self.assertEqual(keys["pi"], "mode")

    def test_codex家族声明压缩窗口菜单(self):
        ui = {ad.name: ad.options_catalog()["ui"] for ad in _adapters()}
        self.assertEqual([x["id"] for x in ui["codex"]["autocompacts"]],
                         ["", "200000", "400000", "600000"])

    def test_pi不声明人工压缩窗口(self):
        ui = {ad.name: ad.options_catalog()["ui"] for ad in _adapters()}
        self.assertNotIn("autocompacts", ui["pi"])

    def test_app_server不可用时契约仍在(self):
        """目录取不到会退到一份静态回落值。契约在那一支上一并丢掉的话,界面读到的模式键
        就是错的——codex 会话上切模式会写进 mode,引擎收不到。"""
        src = _srcmap.engine_adapters_src()
        seg = re.search(r"class CodexAdapter[\s\S]+?def normalize_options", src).group(0)
        body = seg[seg.index("def options_catalog"):]
        self.assertEqual(body.count("self.ui_contract()"), 2, "成功与回落两条路都要带上契约")


class FrontendReadsDeclaration(unittest.TestCase):
    def test_档位那几处不再按引擎名分流(self):
        for name in ("hsModes", "hsEffortDefault", "hsModeLabel", "hsPickMode", "hsSlashAll"):
            seg = re.search(r"function %s\(v(?:, anchor)?\)[\s\S]+?\n}" % name, CHAT)
            self.assertTrue(seg, "%s 不见了" % name)
            self.assertNotIn("hsIsCodexLike", seg.group(0))
            self.assertNotIn("collaboration_mode", seg.group(0))

    def test_模式键从声明里取_取不到按静态表再按mode(self):
        seg = re.search(r"function hsModeKey\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn('((v.catalog || {}).ui || {}).modeKey || hsUI(v).modeKey || "mode"', seg)

    def test_静态表留着codex家族的键做回落(self):
        """后端改了、进程还没重启的那一小段时间里,/config 带回的 catalog 里没有 ui。
        少了这一条,那期间打开的 codex 会话会按 mode 读写协作档,切了没反应。"""
        seg = re.search(r"const HS_UI_CODEX = \{[\s\S]+?\n\};", CHAT).group(0)
        self.assertIn('modeKey: "collaboration_mode"', seg)
        self.assertIn("autocompacts: HS_AUTOCOMPACTS", seg)

    def test_pi静态回退不显示人工压缩窗口(self):
        seg = re.search(r"\n  pi: \{[\s\S]+?\n  \},", CHAT).group(0)
        self.assertIn("autocompacts: null", seg)
        self.assertNotIn("autocompacts: HS_AUTOCOMPACTS", seg)

    def test_压缩菜单优先读后端声明(self):
        seg = re.search(r"function hsAutocompacts\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("(v.catalog || {}).ui", seg)
        self.assertIn("hsUI(v).autocompacts", seg)

    def test_模型菜单的圆点与输入框上那一枚同解(self):
        """两处都该指同一个模型。菜单原来读 opts.model 的原值,输入框读的是解析后的那一个
        (没选过时取清单里标 default 的);pi 的清单里每个模型都有真 id,于是菜单一个圆点
        都没有,而旁边明明显示着模型名。"""
        seg = re.search(r"function hsShowModelMenu\(v, anchor,[\s\S]+?\n  const models", CHAT).group(0)
        self.assertIn("const selected = hsModel(v);", seg)
        self.assertIn("const cur = selected && selected.id;", seg)
        self.assertNotIn('const cur = o.model || ""', seg)
        resolver = re.search(r"function hsModel\(v\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn("(x.variants || []).some((route) => route.id === id)", resolver)

    def test_当前档位只有一处判定(self):
        # 三处用它:字样、切档菜单的选中项、模式自带的默认 effort。各写一遍就会各错一遍
        self.assertIn("function hsModeId(v)", CHAT)
        self.assertGreaterEqual(CHAT.count("hsModeId(v)"), 3)


if __name__ == "__main__":
    unittest.main()
