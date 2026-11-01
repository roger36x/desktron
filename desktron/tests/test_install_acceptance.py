"""Offline acceptance must boot the real payload, without user state or engines."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from unittest.mock import Mock, patch

import pytest
from provision import install_check
from shepherd import capabilities, cli

ROOT = Path(__file__).resolve().parents[2]


def test_environment_is_whitelisted_not_credential_blacklisted(tmp_path, monkeypatch):
    monkeypatch.setenv('UNEXPECTED_PROVIDER_SECRET', 'never-copy')
    monkeypatch.setenv('DESKTRON_APP_CONTROL_HOST_TOKEN', 'never-copy')
    monkeypatch.setenv('DESKTRON_NATIVE_WRITER_SESSION_ID', 'never-copy')
    monkeypatch.setenv('PYTHONPATH', '/user/overrides')
    monkeypatch.setenv('DESKTRON_RUNTIME_PATH', '/user/engines')
    env = install_check.isolated_env(ROOT, tmp_path)
    for name in ('UNEXPECTED_PROVIDER_SECRET', 'DESKTRON_APP_CONTROL_HOST_TOKEN',
                 'DESKTRON_NATIVE_WRITER_SESSION_ID', 'DESKTRON_RUNTIME_PATH'):
        assert name not in env
    assert env['PYTHONPATH'] == str(ROOT / 'desktron/lib')
    assert env['HOME'] == str(tmp_path / 'home')
    assert env['DESKTRON_DATA_HOME'] == str(tmp_path / 'data')
    assert env['DESKTRON_INSTALL_CHECK'] == '1'
    assert '/user/engines' not in env['PATH']


def test_real_backend_boot_probe_frontend_and_shepherd_status():
    report = install_check.check(ROOT)
    assert report['ok'], report
    assert report['checks'] == {
        'python_packages': 'passed', 'backend_probe': 'passed',
        'frontend_entry': 'passed', 'session_creation_offline': 'passed',
        'shepherd_initialization_offline': 'passed', 'shepherd_status_offline': 'passed'}
    assert report['model_requests'] == 'not_run'
    assert report['mode'] == 'isolated-offline'


def test_session_creation_failure_rejects_an_otherwise_ready_backend(monkeypatch):
    original = install_check._get

    def fail_create(opener, url, timeout):
        if isinstance(url, install_check.urllib.request.Request) and url.get_method() == 'POST':
            raise RuntimeError('session index commit failed')
        return original(opener, url, timeout)

    monkeypatch.setattr(install_check, '_get', fail_create)
    report = install_check.check(ROOT)
    assert not report['ok']
    assert report['checks']['backend_probe'] == 'passed'
    assert report['checks']['frontend_entry'] == 'passed'
    assert 'session_creation_offline' not in report['checks']
    assert 'session index commit failed' in report['error']
    assert report['model_requests'] == 'not_run'


def test_shepherd_initialization_failure_rejects_an_otherwise_ready_backend(monkeypatch):
    original = install_check.subprocess.run

    def fail_initialize(argv, **kwargs):
        if argv[1:3] == ['-X', 'utf8=0']:
            return subprocess.CompletedProcess(argv, 1, '', 'UnicodeDecodeError')
        return original(argv, **kwargs)

    monkeypatch.setattr(install_check.subprocess, 'run', fail_initialize)
    report = install_check.check(ROOT)
    assert not report['ok']
    assert report['checks']['session_creation_offline'] == 'passed'
    assert 'shepherd_initialization_offline' not in report['checks']
    assert 'Shepherd offline runner initialization failed' in report['error']
    assert report['model_requests'] == 'not_run'


def _payload(tmp_path, backend):
    (tmp_path / 'desktron/lib').mkdir(parents=True)
    (tmp_path / 'frontend').mkdir()
    (tmp_path / 'frontend/index.html').write_text('<!doctype html>')
    (tmp_path / 'desktron/lib/ui_server.py').write_text(backend)
    return tmp_path


def test_offline_shepherd_status_never_runs_engine_protocol_probes(capsys):
    with patch.object(capabilities.ClaudeConnector, 'availability') as claude, \
            patch.object(capabilities.PiConnector, 'availability') as pi, \
            patch.object(capabilities.CodexFamilyConnector, 'availability') as codex:
        assert cli.main(['status', '--offline']) == 0
    for probe in (claude, pi, codex):
        probe.assert_not_called()
    matrix = json.loads(capsys.readouterr().out)['connectors']
    assert set(matrix) == {'claude', 'codex', 'pi'}
    assert all(row['availability']['reason'] == 'not_checked' and not row['enabled']
               for row in matrix.values())


def test_missing_import_fails_acceptance_instead_of_passing_unit_count(tmp_path):
    root = _payload(tmp_path, 'import nonexistent_removed_delivery_module\n')
    report = install_check.check(root, timeout=3)
    assert not report['ok']
    assert 'ModuleNotFoundError' in report['error']
    assert 'nonexistent_removed_delivery_module' in report['error']
    assert report['checks'] == {'python_packages': 'passed'}


def test_missing_python_packages_fails_before_backend_spawn(monkeypatch):
    monkeypatch.setattr(install_check.importlib.util, 'find_spec', lambda name: None)
    spawn = Mock()
    monkeypatch.setattr(install_check.subprocess, 'Popen', spawn)
    report = install_check.check(ROOT)
    assert not report['ok'] and 'PyYAML' in report['error']
    spawn.assert_not_called()


def test_missing_pillow_fails_before_backend_spawn(monkeypatch):
    original = install_check.importlib.util.find_spec
    monkeypatch.setattr(install_check.importlib.util, 'find_spec',
                        lambda name: None if name == 'PIL' else original(name))
    spawn = Mock()
    monkeypatch.setattr(install_check.subprocess, 'Popen', spawn)
    report = install_check.check(ROOT)
    assert not report['ok'] and 'Pillow' in report['error']
    spawn.assert_not_called()


def test_incomplete_payload_is_rejected_before_spawn(tmp_path, monkeypatch):
    spawn = Mock()
    monkeypatch.setattr(install_check.subprocess, 'Popen', spawn)
    report = install_check.check(tmp_path)
    assert not report['ok'] and 'Incomplete payload' in report['error']
    spawn.assert_not_called()


def test_never_accepts_another_backends_probe_identity(monkeypatch):
    monkeypatch.setattr(install_check, '_get', lambda *args: json.dumps({
        'desk': True, 'pid': -1, 'boot_id': 'unrelated'}).encode())
    report = install_check.check(ROOT)
    assert not report['ok'] and 'identity mismatch' in report['error']


def test_startup_timeout_terminates_only_owned_child(tmp_path):
    root = _payload(tmp_path, 'import time\ntime.sleep(60)\n')
    report = install_check.check(root, timeout=0.1)
    assert not report['ok'] and 'timed out' in report['error']
    assert report['duration_s'] < 8


@pytest.mark.skipif(os.name == 'nt', reason='POSIX SIGTERM/SIGINT 转交契约；Windows 使用宿主事件')
@pytest.mark.parametrize('termination', [signal.SIGTERM, signal.SIGINT])
def test_interrupted_cli_reaps_its_separate_backend_session(tmp_path, termination):
    root = _payload(tmp_path / 'payload', '''import os, sys, time
from pathlib import Path
Path(sys.argv[4], 'backend.pid').write_text(str(os.getpid()))
time.sleep(60)
''')
    temporary = tmp_path / 'temporary'
    temporary.mkdir()
    proc = subprocess.Popen([sys.executable, str(Path(install_check.__file__)),
                             '--root', str(root), '--timeout', '30'],
                            env=dict(os.environ, TMPDIR=str(temporary)),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)
    child = None
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            markers = list(temporary.glob('desk-install-check-*/workspace/backend.pid'))
            if markers:
                child = int(markers[0].read_text())
                break
            time.sleep(0.02)
        assert child is not None, 'backend fixture never started'
        proc.send_signal(termination)
        out, error = proc.communicate(timeout=8)
        assert proc.returncode == 130, error
        assert json.loads(out)['ok'] is False
        with pytest.raises(ProcessLookupError):
            os.kill(child, 0)
        assert not list(temporary.glob('desk-install-check-*'))
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if child is not None:
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass


def test_report_saved_atomically_without_credentials(tmp_path):
    target = tmp_path / 'reports/install-check.json'
    install_check.save_report(target, {'ok': False, 'model_requests': 'not_run'})
    assert json.loads(target.read_text())['ok'] is False
    if sys.platform != 'win32':  # Windows permissions are ACLs, not POSIX mode bits.
        assert target.stat().st_mode & 0o777 == 0o600
    assert list(target.parent.iterdir()) == [target]


def test_quick_gate_and_installation_use_same_offline_acceptance():
    gate = (ROOT / 'scripts/dev/refactor-gate.sh').read_text(encoding='utf-8')
    release = (ROOT / 'scripts/dev/release.sh').read_text(encoding='utf-8')
    launcher = (ROOT / 'desktron/bin/desktron').read_text(encoding='utf-8')
    assert 'provision/install_check.py' in gate
    assert gate.index('provision/install_check.py') < gate.index('run-tests.py')
    assert 'install-check)' in launcher
    assert 'provision/install_check.py' in launcher
    assert 'install-check.json' in release
    assert release.index('desktron" prepare') < release.index('desktron" install-check')
    assert release.index('desktron" install-check') < release.index('open "$HOME/Applications/Desktron.app"')


def test_backend_skips_external_background_work_before_acceptance_serving():
    source = (ROOT / 'desktron/lib/ui_server.py').read_text(encoding='utf-8')
    boundary = source.index('if os.environ.get("DESKTRON_INSTALL_CHECK") == "1":')
    assert source.index('_SERVER_THREAD.start()') < boundary
    assert source.index('_APP_CONTROL_THREAD.start()') < boundary
    for external in ('resident").start_sweep', '_claude_theme_boot()',
                     'memory_scheduler").start_background', 'target=_prewarm_codex_catalog',
                     'larkbot_api.start_background', 'arrangement.scheduler',
                     'self_update").start_background', 'task_watch.start'):
        assert source.index(external, boundary) > boundary
    assert '_serve_until_shutdown()\n    sys.exit(0)' in source[boundary:]
