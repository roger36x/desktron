"""Real offline startup with damaged optional settings in a disposable HOME."""
from pathlib import Path

import pytest

from provision import install_check

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('raw', ['{}', '{bad', '[]'])
def test_corrupt_tailscale_does_not_stop_local_http(monkeypatch, raw):
    original = install_check.isolated_env
    files = []

    def isolated_with_corrupt_config(root, sandbox):
        env = original(root, sandbox)
        # Exercise the default bind policy, not an explicit loopback override.
        env.pop('DESKTRON_REMOTE_BIND', None)
        path = Path(env['HOME']) / '.config/desktron/tailscale.json'
        path.parent.mkdir(parents=True)
        path.write_text(raw, encoding='utf-8')
        files.append(path)
        return env

    original_stop = install_check._stop
    preserved = []

    def stop_and_check(proc):
        try:
            preserved.append(files[-1].read_text(encoding='utf-8') == raw)
            log = files[-1].parents[3] / 'backend.log'
            # backend.log is in the sandbox, adjacent to its isolated HOME.
            logged = 'Tailscale 配置读取失败' in log.read_text(encoding='utf-8')
            preserved.append(logged)
        finally:
            original_stop(proc)

    monkeypatch.setattr(install_check, 'isolated_env', isolated_with_corrupt_config)
    monkeypatch.setattr(install_check, '_stop', stop_and_check)
    report = install_check.check(ROOT)
    assert report['ok'], report
    assert report['checks']['backend_probe'] == 'passed'
    assert report['checks']['frontend_entry'] == 'passed'
    assert report['model_requests'] == 'not_run'
    assert preserved == [True, True]
