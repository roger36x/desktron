"""显式运行 DESK_WEBKIT_TESTS=1 pytest ...；只加载本地 WKWebView fixture。"""
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
                     and os.environ.get('DESK_WEBKIT_TESTS') == '1', '需显式启用 macOS WebKit 测试')
@pytest.mark.desktop
class WebKitWait(unittest.TestCase):
    def test_real_approval_revalidation_and_visible_locators(self):
        with tempfile.TemporaryDirectory(prefix='desk-approval-fixture-') as tmp:
            binary = str(Path(tmp) / 'approval-fixture')
            subprocess.run(['swiftc', str(Path(__file__).parent / 'fixtures/app_control_revalidate_webkit.swift'),
                            '-o', binary], check=True, capture_output=True, text=True, timeout=90)
            result = subprocess.run([binary, str(ROOT / 'desktop/native/main.swift')],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            cases = json.loads(result.stdout)
            self.assertEqual(len(cases), 10)
            self.assertEqual(cases[0]['result'], 'ok')
            self.assertEqual(cases[6]['result'], 'locator_ambiguous')

    def test_real_dom_conditions(self):
        with tempfile.TemporaryDirectory(prefix='desk-wait-fixture-') as tmp:
            binary = str(Path(tmp) / 'wait-fixture')
            subprocess.run(['swiftc', str(Path(__file__).parent / 'fixtures/app_control_wait_webkit.swift'),
                            '-o', binary], check=True, capture_output=True, text=True, timeout=90)
            result = subprocess.run([binary, str(ROOT / 'desktop/native/main.swift')],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            cases = json.loads(result.stdout)
            self.assertEqual(len(cases), 6)
            self.assertEqual(cases[0]['result'], 'ok')
            self.assertEqual(cases[2]['result'], 'wait_aborted')
            self.assertEqual(cases[3]['result'], 'resource_changed')

    def test_real_custom_controls_and_occlusion(self):
        with tempfile.TemporaryDirectory(prefix='desk-controls-fixture-') as tmp:
            binary = str(Path(tmp) / 'controls-fixture')
            subprocess.run(['swiftc', str(Path(__file__).parent / 'fixtures/app_control_controls_webkit.swift'),
                            '-o', binary], check=True, capture_output=True, text=True, timeout=90)
            result = subprocess.run([binary, str(ROOT / 'desktop/native/main.swift')],
                                    capture_output=True, text=True, timeout=40)
            self.assertEqual(result.returncode, 0, result.stderr)
            cases = json.loads(result.stdout)
            self.assertTrue(all(row['result'] == 'ok' for row in cases), cases)
