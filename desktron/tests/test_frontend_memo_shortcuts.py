"""Workflow memo keyboard shortcut regressions."""
import json
import os
import re
import subprocess
import unittest
import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class FrontendMemoShortcutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = _srcmap.panel_src()
        cls.indent_fn = re.search(
            r"function _wfMemoIndent\(.*?\n}\nfunction _wfEdOffset", cls.source, re.S
        ).group(0).rsplit("\nfunction _wfEdOffset", 1)[0]

    def run_indent(self, text, start, end, outdent=False):
        script = self.indent_fn + "\nconsole.log(JSON.stringify(_wfMemoIndent(" + ",".join([
            json.dumps(text), str(start), str(end), "true" if outdent else "false"
        ]) + ")));"
        proc = subprocess.run(["node", "-e", script], check=True, capture_output=True, text=True)
        return json.loads(proc.stdout)

    def test_tab_indents_current_line(self):
        self.assertEqual(
            self.run_indent("alpha\nbeta", 2, 2),
            {"text": "  alpha\nbeta", "start": 4, "end": 4},
        )

    def test_tab_indents_selected_lines(self):
        self.assertEqual(
            self.run_indent("a\nb", 0, 3),
            {"text": "  a\n  b", "start": 2, "end": 7},
        )

    def test_tab_at_document_start_indents_only_first_line(self):
        self.assertEqual(
            self.run_indent("\nitem", 0, 0),
            {"text": "  \nitem", "start": 2, "end": 2},
        )

    def test_shift_tab_outdents_selected_lines(self):
        self.assertEqual(
            self.run_indent("  alpha\n\tbeta", 0, 14, True),
            {"text": "alpha\nbeta", "start": 0, "end": 10},
        )

    def test_editor_captures_tab_and_shift_tab(self):
        editor = re.search(r"function _wfNoteEd\(.*?\n}\nfunction _wfEdSet", self.source, re.S).group(0)
        self.assertIn('ev.key !== "Tab"', editor)
        self.assertIn("_wfEdIndentSelection(ed, domId, ev.shiftKey)", editor)


if __name__ == "__main__":
    unittest.main()
