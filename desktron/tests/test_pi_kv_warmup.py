"""Offline behavioral coverage for the optional Pi KV warmup extension."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_pi_kv_warmup_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for Pi extension behavior tests")
    script = Path(__file__).with_name("pi_kv_warmup.test.mjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
