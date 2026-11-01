"""Managed Python policy and bootstrap failures, using only fake local archives."""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest
from provision import setup_runtime

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / 'desktron/lib/provision/python_runtime.sh'


def _shell(tmp_path, script):
    env = dict(os.environ, HOME=str(tmp_path / 'home'),
               DESKTRON_DATA_HOME=str(tmp_path / 'data'),
               PROJECT_ROOT=str(tmp_path / 'checkout'),
               RUNTIME_FIXTURE=str(tmp_path))
    return subprocess.run(['bash', '-c', 'set -eu\nsource "$1"\n' + script,
                           'runtime-test', str(HELPER)], env=env,
                          capture_output=True, text=True, timeout=15, encoding='utf-8')


@pytest.mark.skipif(os.name == 'nt', reason='POSIX shell 托管 Python 启动契约；Windows 使用原生入口')
def test_python_minimum_and_architecture_are_both_checked(tmp_path):
    result = _shell(tmp_path, '''
desk_python_platform
desk_python_check "$2"
DESK_PYTHON_ARCH=unsupported
if desk_python_check "$2"; then exit 20; fi
'''.replace('"$2"', '"' + sys.executable + '"'))
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(os.name == 'nt', reason='POSIX shell 的开发解释器选择契约')
def test_development_python_allowed_but_not_a_release_fallback(tmp_path):
    checkout = tmp_path / 'checkout'
    checkout.mkdir()
    (checkout / '.git').write_text('gitdir: worktree', encoding='utf-8')
    result = _shell(tmp_path, 'desk_python_select\n')
    assert result.returncode == 0, result.stderr
    (checkout / '.git').unlink()
    result = _shell(tmp_path, 'desk_python_select\n')
    assert result.returncode != 0
    assert '托管运行时' in result.stderr


def _fixture(tmp_path, executable=True, checksum=True):
    archive = tmp_path / 'python.tgz'
    content = b'#!/bin/sh\nexit 0\n' if executable else b'#!/bin/sh\nexit 1\n'
    with tarfile.open(archive, 'w:gz') as tf:
        item = tarfile.TarInfo('python/bin/python3')
        item.mode = 0o755
        item.size = len(content)
        tf.addfile(item, io.BytesIO(content))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest() if checksum else '0' * 64
    name = 'cpython-3.12.14+20260814-aarch64-apple-darwin-install_only.tar.gz'
    (tmp_path / 'SHA256SUMS').write_text(digest + '  ' + name + '\n', encoding='utf-8')


FAKE_INSTALL = '''
uname() { case "$1" in -s) echo Darwin;; -m) echo arm64;; esac; }
desk_python_download() {
  case "$1" in
    */SHA256SUMS) cp "$RUNTIME_FIXTURE/SHA256SUMS" "$2";;
    *) cp "$RUNTIME_FIXTURE/python.tgz" "$2";;
  esac
}
'''


@pytest.mark.skipif(os.name == 'nt', reason='macOS shell 安装器的托管解释器选择契约')
def test_install_always_selects_managed_python_even_if_path_has_good_python(tmp_path):
    _fixture(tmp_path)
    result = _shell(tmp_path, FAKE_INSTALL + '''
desk_python_ensure
[ "$(command -v python3)" = "$DESK_PYTHON_HOME/bin/python3" ]
desk_python_download() { echo unexpected-download >&2; return 9; }
desk_python_ensure
''')
    assert result.returncode == 0, result.stderr
    assert 'unexpected-download' not in result.stderr
    assert len(list((tmp_path / 'runtime').glob('python-3.12.14-*'))) == 1
    assert not list((tmp_path / 'runtime').glob('python-setup.*'))
    assert not (tmp_path / 'runtime/python-install.lockdir').exists()


@pytest.mark.parametrize('failure', ['checksum', 'executable'])
def test_invalid_download_preserves_existing_runtime(tmp_path, failure):
    _fixture(tmp_path, executable=failure != 'executable', checksum=failure != 'checksum')
    old = tmp_path / 'runtime/python-3.12.14-darwin-arm64'
    old.mkdir(parents=True)
    (old / 'old-data').write_text('keep', encoding='utf-8')
    result = _shell(tmp_path, FAKE_INSTALL + 'desk_python_ensure\n')
    assert result.returncode != 0
    assert (old / 'old-data').read_text(encoding='utf-8') == 'keep'
    assert not list((tmp_path / 'runtime').glob('python-setup.*'))
    assert not (tmp_path / 'runtime/python-install.lockdir').exists()


@pytest.mark.parametrize('restore_fails', [False, True])
@pytest.mark.skipif(os.name == 'nt', reason='POSIX mv/tar 故障注入；Windows 原生目录交换另测')
def test_swap_failure_restores_or_keeps_unique_recovery_copy(tmp_path, restore_fails):
    _fixture(tmp_path)
    old = tmp_path / 'runtime/python-3.12.14-darwin-arm64'
    old.mkdir(parents=True)
    (old / 'old-data').write_text('keep', encoding='utf-8')
    script = FAKE_INSTALL + '''
mv() {
  case "$1" in */python) return 1;; esac
''' + ('  case "$1" in */previous) return 1;; esac\n' if restore_fails else '') + '''
  command mv "$@"
}
desk_python_ensure
'''
    result = _shell(tmp_path, script)
    assert result.returncode != 0
    if restore_fails:
        copies = list((tmp_path / 'runtime').glob('python-setup.*/previous/old-data'))
        assert len(copies) == 1 and copies[0].read_text(encoding='utf-8') == 'keep'
    else:
        assert (old / 'old-data').read_text(encoding='utf-8') == 'keep'
    assert not (tmp_path / 'runtime/python-install.lockdir').exists()


def test_node_rejects_new_enough_emulated_architecture(monkeypatch):
    monkeypatch.setattr(setup_runtime, '_node_platform', lambda: ('darwin', 'arm64'))
    monkeypatch.setattr(setup_runtime, '_version_ok', lambda command: True)
    monkeypatch.setattr(setup_runtime.shutil, 'which', lambda name: '/usr/local/bin/node')
    monkeypatch.setattr(subprocess, 'run', lambda *a, **kw: type('Result', (), {
        'returncode': 0, 'stdout': 'x64\n'})())
    assert setup_runtime.node_command() == ''


def test_node_uses_native_managed_binary_before_wrong_path_binary(tmp_path, monkeypatch):
    monkeypatch.setenv('DESKTRON_DATA_HOME', str(tmp_path / 'data'))
    monkeypatch.setattr(setup_runtime, '_node_platform', lambda: ('darwin', 'arm64'))
    monkeypatch.setattr(setup_runtime, '_version_ok', lambda command: True)
    managed = str(tmp_path / 'runtime/node-v22.23.2-darwin-arm64/bin/node')
    monkeypatch.setattr(setup_runtime.shutil, 'which', lambda name: '/usr/local/bin/node')
    monkeypatch.setattr(subprocess, 'run', lambda args, **kw: type('Result', (), {
        'returncode': 0, 'stdout': 'arm64\n' if args[0] == managed else 'x64\n'})())
    assert setup_runtime.node_command() == managed


def test_node_architecture_probe_fails_closed_on_bad_cpu_type(monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('Bad CPU type in executable')
    monkeypatch.setattr(subprocess, 'run', fail)
    assert not setup_runtime._architecture_ok('/broken/node')


def test_manifest_and_installer_share_managed_runtime_contract():
    manifest = (ROOT / 'manifest.yaml').read_text(encoding='utf-8')
    installer = (ROOT / 'scripts/dev/release.sh').read_text(encoding='utf-8')
    launcher = (ROOT / 'desktron/bin/desktron').read_text(encoding='utf-8')
    assert 'name: python3\n    min: "3.11"' in manifest
    assert 'provision/python_runtime.sh' in installer
    assert 'provision/python_runtime.sh' in launcher
    requirements = (ROOT / 'desktron/lib/provision/python-requirements.txt').read_text(encoding='utf-8')
    assert 'PyYAML==6.0.3' in requirements
    assert 'Pillow==12.1.1' in requirements
    assert 'PIL.__version__ == "12.1.1"' in HELPER.read_text(encoding='utf-8')
    assert 'desk_python_dependencies || return 1' in HELPER.read_text(encoding='utf-8')
    assert '(3,9)' not in installer
    assert 'python-*/bin' not in launcher
