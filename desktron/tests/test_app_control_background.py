"""后台输入使用生产 Swift 方法与本地 WKWebView，不启动或控制 Desk。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import pytest

ROOT = Path(__file__).resolve().parents[2]


@unittest.skipUnless(sys.platform == 'darwin' and shutil.which('swiftc')
                     and os.environ.get('DESK_WEBKIT_TESTS') == '1', '需启用 macOS WebKit 测试')
@pytest.mark.desktop
class BackgroundInput(unittest.TestCase):
    def test_production_input_without_foreground(self):
        source = (ROOT / 'desktop/native/main.swift').read_text()
        def method(start, end):
            return source[source.index(start):source.index(end, source.index(start))]
        methods = method('    @available(macOS 11.0, *)\n    private func runNativeInput',
                         '    /// 把 press 的 key')
        methods += method('    private func nativeKeyEvents', '    private func runBrowserAgentCommand')
        dom = source.split('    private func appControlDOMScript() -> String {', 1)[1].split('"""#', 1)[0]
        methods += '    private func appControlDOMScript() -> String {' + dom + '"""#\n    }\n'
        fixture = (Path(__file__).parent / 'fixtures/app_control_background_webkit.swift').read_text()
        fixture = fixture.replace('    /* PRODUCTION_INPUT_METHODS */', methods)
        fixture = fixture.replace('/* PRODUCTION_BROWSER_VIEW */',
                                  method('final class BrowserWebView:', '// 悬浮标签 chip:'))
        with tempfile.TemporaryDirectory(prefix='desk-background-fixture-') as tmp:
            path = Path(tmp) / 'main.swift'
            path.write_text(fixture)
            binary = str(Path(tmp) / 'fixture')
            built = subprocess.run(['swiftc', str(path), '-o', binary], capture_output=True, text=True, timeout=90)
            self.assertEqual(built.returncode, 0, built.stderr)
            run = subprocess.run([binary], capture_output=True, text=True, timeout=50)
            self.assertEqual(run.returncode, 0, run.stderr)
            rows = json.loads(run.stdout)
            print(json.dumps(rows, ensure_ascii=False))
            self.assertEqual(len(rows), 3)
            for row in rows:
                self.assertFalse(row['app_active'])
                self.assertFalse(row['window_key'])
                self.assertEqual(row['user_field'], 'user text unchanged')
                self.assertTrue(row['user_focus_preserved'], row)
                self.assertEqual(row['clicks'], 1, row)
                self.assertTrue(row['trusted'], row)
                self.assertEqual(row['keys'], 1, row)
                self.assertEqual(row['value'], 'z', row)
                self.assertTrue(row['drag_start'], row)
                self.assertTrue(row['drag_end'], row)
                self.assertTrue(row['receipt']['pointer_ok'], row)
                self.assertTrue(row['receipt']['key_ok'], row)
                self.assertTrue(row['receipt']['drag_ok'], row)
                self.assertFalse(row['agent_triggered_handoff'], row)
                self.assertTrue(row['human_input_routed_first'], row)
