"""中文文档里不给每个全角括号套黄框。

Monaco 默认开着 `unicodeHighlight.ambiguousCharacters`:把「长得像 ASCII 但不是」的字符
逐个框出来。（ ） ，正是这一类(U+FF08 / U+FF09 / U+FF0C),于是一篇中文文档里每个括号、
每个逗号都带一个黄框,整屏都是噪声(2026-08-19 用户实见,common/orientation.md)。

不一刀切关掉:一个全角逗号混进 .py 是真会出错的,那时这个框恰恰是要看的东西。
判据按模型的语言走——散文类(markdown / 纯文本)关,代码类留,切标签时跟着换。

注:vendor/ 那份打包产物不入 git(见 src/README.md),换机器要照 README 重打一份,
所以这里钉的是源码 src/entry.js。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC = os.path.join(ROOT, "frontend", "app-assets", "files", "src", "entry.js")
ENTRY = open(SRC, encoding="utf-8").read()


class ProseSkipsAmbiguous(unittest.TestCase):
    def test_散文类关代码类留(self):
        self.assertIn('const PROSE_LANG = new Set(["markdown", "plaintext"]);', ENTRY)
        seg = re.search(r"function unicodeOpt\(lang\)[\s\S]+?\n}", ENTRY).group(0)
        self.assertIn("ambiguousCharacters: !PROSE_LANG.has(", seg)

    def test_切标签时跟着模型的语言换(self):
        """同一个编辑器实例来回换模型,选项不跟着换就会停在上一份的语言上。"""
        self.assertIn("unicodeHighlight: unicodeOpt(t.model.getLanguageId())", ENTRY)

    def test_建出来时先按散文(self):
        seg = re.search(r"monaco\.editor\.create\(\$\(\"#ed\"\)[\s\S]+?\n  \}\);", ENTRY).group(0)
        self.assertIn('unicodeHighlight: unicodeOpt("plaintext")', seg)

    def test_差异视图两侧同一口径(self):
        """diffEd.updateOptions 收的是差异自己那几项,编辑器选项要分别给两个内嵌实例。"""
        self.assertIn("diffEd.getOriginalEditor().updateOptions(uo)", ENTRY)
        self.assertIn("diffEd.getModifiedEditor().updateOptions(uo)", ENTRY)


if __name__ == "__main__":
    unittest.main()
