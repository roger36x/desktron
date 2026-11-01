"""回到最新箭头使用已批准的32格独立重绘，保留flex居中与原17px尺寸。

历史版本曾复用发送箭头的24格镜像。用户已批准剩余25项全部独立重绘，
不再要求与发送箭头同路径或同描边；仍禁止叠加光学位移来凑居中。
"""
import json
import os
import re
import unittest
import xml.etree.ElementTree as ET

import _srcmap

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CHAT = _srcmap.chat_src()
CSS = open(os.path.join(ROOT, "frontend", "style.css"), encoding="utf-8").read()
NS = {'s': 'http://www.w3.org/2000/svg'}


def down_icon():
    match = re.search(r'^  down: (".*"),$', CHAT, re.M)
    assert match, '找不到down图标定义'
    return ET.fromstring(json.loads(match.group(1)))


class 回到最新箭头使用批准重绘(unittest.TestCase):
    def test_几何为批准的向下箭头(self):
        root = down_icon()
        self.assertEqual(root.attrib['viewBox'], '0 0 32 32')
        self.assertEqual(root.find('.//s:path', NS).attrib['d'], 'M16 5.5v20.7M7.6 18.3l8.4 8.4 8.4-8.4')
        self.assertEqual(root.attrib['width'], '17')
        self.assertEqual(root.attrib['height'], '17')
        self.assertFalse(root.findall('.//*[@transform]'))

    def test_子组固定批准的描边(self):
        paint = down_icon().find('s:g', NS)
        self.assertEqual(paint.attrib['stroke-width'], '2.25')
        self.assertEqual(paint.attrib['stroke-linecap'], 'round')
        self.assertEqual(paint.attrib['stroke-linejoin'], 'round')

    def test_没有额外的transform(self):
        m = re.search(r"\n\.hs-page \.hs-down svg \{[^}]*\}", CSS)
        self.assertIsNone(m, "不应再给箭头添加额外的光学位移")

    def test_居中靠flex不靠位移(self):
        m = re.search(r"\n\.hs-page \.hs-down \{[^}]*\}", CSS).group(0)
        self.assertIn("align-items: center", m)
        self.assertIn("justify-content: center", m)


if __name__ == "__main__":
    unittest.main()
