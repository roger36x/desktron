"""会话正文划选文字不脱钩(2026-11-01)。

鼠标按住移动是选字，正文不会因此滚动。原实现把它当成向上拖动，立即 hsUnstick：
贴底态被解除，已摘下的旧回合整批挂回，选字途中正文整体重排。修复后鼠标只记拖动
状态，真正向上滚动时由 scroll 监听脱钩；触屏与触控笔拖动仍立即脱钩。
"""
import json
import re
import subprocess
import unittest

import _srcmap

PAGE = open(_srcmap.os.path.join(_srcmap.FRONTEND, "app-chat-page.js"), encoding="utf-8").read()


def handler():
    m = re.search(r'v\.body\.addEventListener\("pointermove", (\(e\) => \{.*?\n  \}), \{ passive: true \}\);',
                  PAGE, re.S)
    assert m, "pointermove handler"
    return m.group(1)


def run(pointer_type):
    js = """
const calls=[];const hsUnstick=()=>calls.push('unstick');globalThis.hsVpdInput=()=>calls.push('vpd');
const v={pointerY:100,dragging:false};
const h=%s;
h({buttons:1,clientY:120,pointerType:%s});
h({buttons:1,clientY:140,pointerType:%s});
console.log(JSON.stringify({calls,dragging:v.dragging}));
""" % (handler(), json.dumps(pointer_type), json.dumps(pointer_type))
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class 划选不脱钩(unittest.TestCase):
    def test_鼠标划选只记拖动不脱钩(self):
        r = run("mouse")
        self.assertTrue(r["dragging"])
        self.assertEqual(r["calls"], [])

    def test_触屏拖动仍立即脱钩(self):
        for kind in ("touch", "pen"):
            r = run(kind)
            self.assertTrue(r["dragging"])
            self.assertEqual(r["calls"], ["vpd", "unstick"])

    def test_向上滚动仍由拖动状态脱钩(self):
        self.assertIn("const dragUp = v.dragging && scrollUp;", PAGE)
        self.assertIn("if (dragUp) { hsUnstick(v); pageBack(); }", PAGE)


if __name__ == "__main__":
    unittest.main()
