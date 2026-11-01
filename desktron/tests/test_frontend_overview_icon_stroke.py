"""Reviewed V2 overview: one rounded panel divided into three, not filled tiles."""
import json
from pathlib import Path
import re
import unittest
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
CORE = (ROOT / 'frontend/app-core.js').read_text(encoding='utf-8')
NS = {'s': 'http://www.w3.org/2000/svg'}


def _icon(name):
    match = re.search(r'^  ' + re.escape(name) + r': (".*"),$', CORE, re.M)
    return ET.fromstring(json.loads(match.group(1))) if match else None


class OverviewIcon(unittest.TestCase):
    def test_uses_reviewed_32_grid_and_unfilled_paint(self):
        icon = _icon('overview')
        self.assertIsNotNone(icon)
        self.assertEqual(icon.attrib['viewBox'], '0 0 32 32')
        paint = icon.find('s:g', NS)
        self.assertEqual(paint.attrib['fill'], 'none')
        self.assertEqual(paint.attrib['stroke-width'], '2.1')
        self.assertFalse(any(node.attrib.get('fill') == 'currentColor' for node in icon.iter()))

    def test_keeps_one_panel_and_two_dividers(self):
        icon = _icon('overview')
        rects = icon.findall('.//s:rect', NS)
        self.assertEqual(len(rects), 1)
        self.assertEqual(rects[0].attrib, {'x': '3.5', 'y': '5', 'width': '25', 'height': '22', 'rx': '5'})
        paths = icon.findall('.//s:path', NS)
        self.assertEqual([node.attrib['d'] for node in paths], ['M13 5.8v20.4M13.5 16h14.2'])

    def test_solid_bell_is_an_explicit_reviewed_exception(self):
        """The user retained the filled bell; don't force it back to outlines."""
        bell = _icon('bell')
        self.assertTrue(any(node.attrib.get('fill') == 'currentColor' for node in bell.iter()))


if __name__ == '__main__':
    unittest.main()
