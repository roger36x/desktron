"""窄条上的「更新」可以一键执行,箭头与文字同中线。

**箭头。** 三枚钮都按首行摆(align-self: flex-start)——摊开之后正文有四五行,居中会让它们
浮在整块的腰上。但只写 flex-start 的话,16px 的钮贴着 21px 那一行的顶,箭头中线比文字高
2px(2026-08-20 实测)。改成高度取一行、图标在里面居中;line-height 必须先继承下来,
否则 1lh 按钮自己的 normal 算成 15px,高度取错,照旧贴顶。

**一键更新。** 原来只把命令摆进详情让人自己抄,理由是「装引擎是往 PATH 上写东西」。
现在给一枚钮,但命令仍由后端从 manifest 取(与 `desktron install` 同一份声明),
前端只报「更新哪家」——把命令行传回去等于开一个任意执行的口子。详情里仍摆着那条命令:
一键执行不该是个盲盒。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
CHAT = _srcmap.chat_src()
UPD = open(os.path.join(ROOT, "desktron", "lib", "engine_updates.py"), encoding="utf-8").read()
SYS = open(os.path.join(ROOT, "desktron", "lib", "system", "api.py"), encoding="utf-8").read()
RUN = UPD[UPD.index("def run_update("):]   # 它是文件里最后一个函数


def rule(sel):
    m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", CSS)
    assert m, "没有这条规则:" + sel
    return m.group(1)



def _narrow_media_blocks():
    """取出所有窄屏媒体查询的块体。括号要配平地找块尾——按下一个 `}` 截会在第一条规则处断开。"""
    out, i = [], 0
    while True:
        i = CSS.find("@media", i)
        if i < 0:
            return out
        head_end = CSS.index("{", i)
        if "max-width: 820px" not in CSS[i:head_end]:
            i = head_end + 1
            continue
        depth, j = 1, head_end + 1
        while depth and j < len(CSS):
            if CSS[j] == "{":
                depth += 1
            elif CSS[j] == "}":
                depth -= 1
            j += 1
        out.append(CSS[head_end + 1:j - 1])
        i = j


class ChevronOnTheLine(unittest.TestCase):
    def test_高度取一行且图标居中(self):
        r = rule(".hs-page .hs-env-chev")
        self.assertIn("height: 1lh", r)
        self.assertIn("align-items: center", r)

    def test_行高要先继承(self):
        """钮自己的 line-height 是 normal,1lh 会按它算成 15px,而这一行是 21px。"""
        self.assertIn("line-height: inherit", rule(".hs-page .hs-env-chev"))

    def test_仍按首行摆(self):
        self.assertIn("align-self: flex-start", rule(".hs-page .hs-env-chev"))

    def test_老的光学补偿撤掉(self):
        self.assertNotIn("transform: translateY(0.5px)", rule(".hs-page .hs-env-chev"))


class ActionButtonOnTheLine(unittest.TestCase):
    """「更新」那枚钮也要与文字同中线。

    这一行上有三枚钮,箭头与关闭钮当初各自处理过中线,唯独动作钮漏了:它是
    padding 2px + line-height 1.6,钮高 24,而这一行行高 21.75(14.5px × 150%),
    贴着行顶摆时中心落在 12,比文字中线的 10.9 低 1.1px(2026-08-25 用户实见)。
    """

    def test_行高继承那一行(self):
        """不继承的话钮的文字盒按自己那份算,高度对不上一行,中线也就对不齐。"""
        self.assertIn("line-height: inherit", rule(".hs-page .hs-env-a"))

    def test_不再有纵向内边距(self):
        """高度要正好等于一行。留着纵向 padding 就会高出去,又得回头补位移。"""
        r = rule(".hs-page .hs-env-a")
        m = re.search(r"padding:\s*([^;]+);", r)
        self.assertIsNotNone(m, "没有 padding 声明")
        parts = m.group(1).split()
        self.assertEqual(parts[0], "0", "纵向 padding 要为 0,当前:" + m.group(1))

    def test_仍按首行摆(self):
        """摊开之后正文有四五行,居中会让它浮在整块的腰上——与另外两枚同一个理由。"""
        self.assertIn("align-self: flex-start", rule(".hs-page .hs-env-a"))

    def test_三枚钮都处理过中线(self):
        """漏掉任何一枚,这一行看上去就是歪的。以后新增钮也补进这条。"""
        for sel in (".hs-page .hs-env-chev", ".hs-page .hs-env-a", ".hs-page .hs-env-x"):
            self.assertIn("line-height: inherit", rule(sel), sel + " 没继承行高")

    def test_不是靠窄屏单独修的(self):
        """这条与设备无关:同一份 CSS 在 PC 上偏一样多,不该只在手机的媒体查询里补一刀。"""
        for block in _narrow_media_blocks():
            self.assertNotIn(".hs-env-a", block, "窄屏媒体查询里另开了一份动作钮的规则")


class OneClickUpdate(unittest.TestCase):
    def test_命令由后端从_manifest_取(self):
        seg = RUN
        self.assertIn("cmd = install_cmd(repo_root, engine)", seg)
        self.assertNotIn("body", seg)   # 不收调用方的命令行

    def test_只认登记过的引擎(self):
        seg = RUN
        self.assertIn("if engine not in NPM:", seg)

    def test_跑完作废缓存(self):
        """不作废的话,界面下一次问版本会拿到 TTL 内的旧答案,升完还提示有新版本。"""
        seg = RUN
        self.assertIn("_cache.pop(engine, None)", seg)

    def test_端点接上了(self):
        self.assertIn('"/workbench/engine-update"', SYS)
        self.assertIn("engine_updates.run_update(_root,", SYS)

    def test_钮只报引擎名(self):
        self.assertIn('action: up.how ? { label: "更新", kind: "engine-update", engine: up.engine } : null,', CHAT)
        self.assertIn('details: up.how ? [up.how] : [],', CHAT)   # 命令仍摆在详情里

    def test_跑的期间禁掉钮(self):
        seg = re.search(r"async function hsEngineUpdate\(v, engine, btn\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn('state: "working"', seg)
        self.assertIn('hsContextStatusSync(v)', seg)
        self.assertIn('label: DeskI18n.t("ctx.updating")', seg)
        self.assertIn('action.disabled = pending;', CHAT)
        self.assertIn('action.setAttribute("aria-busy", String(pending));', CHAT)

    def test_成了要说重开才生效(self):
        seg = re.search(r"async function hsEngineUpdate\(v, engine, btn\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn('"ctx.updateSuccessHint"', seg)
        zh = open(os.path.join(ROOT, "frontend", "app-i18n-zh.js"), encoding="utf-8").read()
        self.assertIn('新版本将在下次启动引擎时生效', zh)
        self.assertIn("_hsUpdateMuted.add", seg)

    def test_败了把原话摊出来(self):
        seg = re.search(r"async function hsEngineUpdate\(v, engine, btn\)[\s\S]+?\n}", CHAT).group(0)
        self.assertIn('unknown ? "warning" : "error"', seg)
        self.assertIn('"ctx.updateFailed"', seg)
        self.assertIn('r?.out ||', seg)


if __name__ == "__main__":
    unittest.main()
