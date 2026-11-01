"""Offline contract tests for Provider capabilities, session snapshots and affinity."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_provider_runtime_contract():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for Provider runtime contract tests")
    script = Path(__file__).with_name("provider_runtime_contract.test.mjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
