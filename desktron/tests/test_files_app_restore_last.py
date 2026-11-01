"""从会话交接过来的那个文件要真的成为当前项。

交接的做法是把「该开哪个文件」写进 IDE 自己的恢复态(app-files.js 的 openFileInIde:
tabs 末尾追加、last 指向它),再让那一页重走启动。启动时先开 tabs 里其余几个,最后开 last,
它就是当前项。

但 last 必须先抄下来:每开一个标签都会走 openTab,那里把 st.last 改成刚开的这个并落盘
(它记的是「当前在看哪个」)。循环之后再读 st.last,读到的已经是其余标签里最后那个——
交接来的文件根本没被打开,出来的是上一次看的那份(2026-08-19 用户实见:点 AGENTS.md
出来 domains.py)。

注:vendor/ 那份打包产物不入 git,这里钉的是源码 src/entry.js。
"""
import os
import re
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ENTRY = open(os.path.join(ROOT, "frontend", "app-assets", "files", "src", "entry.js"),
             encoding="utf-8").read()


class RestoresTheHandedOffFile(unittest.TestCase):
    def test_last_先抄下来再开其余(self):
        seg = ENTRY[ENTRY.index("// 恢复上次开着的那几个标签"):]
        self.assertIn("const last = st.last;", seg)
        self.assertLess(seg.index("const last = st.last;"), seg.index("const want ="))

    def test_循环与收尾都用抄下来的那个(self):
        seg = ENTRY[ENTRY.index("// 恢复上次开着的那几个标签"):]
        self.assertIn("st.tabs.filter((x) => x !== last)", seg)
        self.assertIn("if (last) await openFile(last,", seg)
        # 循环之后不能再读 st.last:那时它已经被 openTab 改过
        tail = seg[seg.index("for (const rel of want)"):]
        self.assertNotIn("st.last", tail)

    def test_切标签确实会写回_st_last(self):
        """这条是上面那个陷阱的前提,写下来免得日后有人把它当成无关的一行删掉。"""
        self.assertIn("st.last = rel; st.tabs = [...tabs.keys()]; save();", ENTRY)


if __name__ == "__main__":
    unittest.main()
