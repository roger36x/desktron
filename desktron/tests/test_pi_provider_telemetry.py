"""Offline behavior coverage for the Pi provider telemetry extension."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_pi_provider_telemetry_behavior():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is required for Pi extension behavior tests")
    script = Path(__file__).with_name("pi_provider_telemetry.test.mjs")
    result = subprocess.run([node, "--test", str(script)], capture_output=True,
                            text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
