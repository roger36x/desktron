"""desktop 发的截图,手机上那条消息看不见(2026-08-26 用户实见)。

不是设计如此,是实时路的缺口:别的客户端发来的用户消息,手机端先补一个空回合,
再靠 message.part.updated 填内容——那个分支只认 text,图片块整个丢掉,
要刷新重建才看得到。数据与传输都验过是好的:后端消息里 image part 带着
/session/<sid>/asset/ 引用,经入口取回来是完好的 PNG。

三个坑一起钉:帧序(text 帧会把气泡清空重填,图片帧先到会被抹掉,靠 t.atts 补回);
去重(同一帧重放不重复画);发送端不收(本地建回合时已画 base64 那份,再收 SSE 的
/session 引用会同一张图两遍——src 不同,靠 src 去重认不出)。
"""
import os
import re
import unittest

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()


def user_branch():
    m = re.search(r'if \(hsIsUser\(v, q\.messageID\)\) \{[\s\S]+?\n      return;\n    \}', CHAT)
    assert m, "part.updated 的用户分支不见了"
    return m.group(0)


class LiveImage(unittest.TestCase):
    def test_用户分支认图片块(self):
        s = user_branch()
        self.assertIn('q.type === "image"', s, "原来只认 text,图片整个丢掉")
        self.assertIn("hsTurnImgs", s)

    def test_发送端不收(self):
        """本地发的回合 atts 里是 data_b64;SSE 回来的是 /session 引用,收了就是同图两遍。"""
        s = user_branch()
        self.assertIn("a.data_b64", s)
        at = s.index("a.data_b64")
        self.assertIn("!", s[at - 120:at], "有 data_b64 的回合该跳过,不是只收它")

    def test_按url去重(self):
        s = user_branch()
        self.assertIn("a.url === src", s, "同一帧重放不该画两遍")

    def test_text帧清空后图能补回(self):
        """hsSetTurnUser 先 textContent='' 再填字——图片帧先到的话会被抹掉,
        填完字必须凭 t.atts 把图补回来。"""
        m = re.search(r"function hsSetTurnUser[\s\S]+?\n\}", CHAT)
        s = m.group(0)
        self.assertIn("hsTurnImgs(v, t)", s)
        self.assertLess(s.index("hsUserBubbleText"), s.index("hsTurnImgs"), "先填字再补图")

    def test_补画按src幂等(self):
        m = re.search(r"function hsTurnImgs[\s\S]+?\n\}", CHAT)
        self.assertIsNotNone(m, "hsTurnImgs 不见了")
        s = m.group(0)
        self.assertIn('getAttribute("src") === src', s, "已在气泡里的不重画")
        self.assertIn("hs-u-img", s, "与发送时那条路同一个类名,样式不分家")


if __name__ == "__main__":
    unittest.main()
