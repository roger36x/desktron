import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_web_tools_node_suites():
    result = subprocess.run([
        'node', '--test',
        str(ROOT / 'desktron/tests/app_control_client.test.mjs'),
        str(ROOT / 'desktron/tests/browser_search.test.mjs'),
        str(ROOT / 'desktron/tests/browser_search_optimization.test.mjs'),
        str(ROOT / 'desktron/tests/web_tools_core.test.mjs'),
        str(ROOT / 'desktron/tests/pi_web.test.mjs'),
    ], cwd=ROOT, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
