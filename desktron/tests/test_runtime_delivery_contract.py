"""安装入口与干净环境的契约；只使用临时目录和伪下载，不安装真实软件。"""
import hashlib
import io
import os
from pathlib import Path
import subprocess
import tarfile
from unittest.mock import patch

import pytest
from provision import setup_runtime as runtime


@pytest.mark.parametrize('checksum', ['wrong', '', 'duplicate', 'good'])
def test_archive_is_verified_before_extraction(tmp_path, monkeypatch, checksum):
    monkeypatch.setattr(runtime, '_data_root', lambda: str(tmp_path))
    monkeypatch.setattr(runtime, 'node_command', lambda: '')
    monkeypatch.setattr(runtime, '_node_platform', lambda: ('linux', 'x64'))
    name = 'node-v%s-linux-x64' % runtime.NODE_VERSION
    source = io.BytesIO()
    with tarfile.open(fileobj=source, mode='w:gz') as tf:
        info = tarfile.TarInfo(name + '/bin/node')
        info.size = 4
        tf.addfile(info, io.BytesIO(b'node'))
    payload = source.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    def download(url, path):
        if url.endswith('.tar.gz'):
            Path(path).write_bytes(payload)
        else:
            line = digest + '  ' + name + '.tar.gz\n'
            Path(path).write_text({'good': line, 'duplicate': line * 2, 'wrong': '0' * 64 + '  ' + name + '.tar.gz\n', '': ''}[checksum], encoding='utf-8')
    monkeypatch.setattr(runtime, '_download', download)
    monkeypatch.setattr(runtime, '_node_ok', lambda command: True)
    with patch.object(runtime, '_safe_extract', wraps=runtime._safe_extract) as extract:
        if checksum == 'good':
            assert Path(runtime.install_node()).read_bytes() == b'node'
            assert extract.call_count == 1
        else:
            with pytest.raises(RuntimeError, match='校验失败'):
                runtime.install_node()
            extract.assert_not_called()
            assert not Path(runtime._managed_node_home()).exists()


@pytest.mark.skipif(os.name == 'nt', reason='POSIX 父 shell PATH 回传；Windows 原生启动器另测')
def test_shell_receives_managed_node_path_and_propagates_failure(tmp_path):
    launcher = (Path(__file__).resolve().parents[1] / 'bin/desktron').read_text(encoding='utf-8')
    function = launcher[launcher.index('prepare_runtime() {'):launcher.index('cmd="${1:-}";')]
    managed = tmp_path / 'managed node' / 'bin'
    managed.mkdir(parents=True)
    (managed / 'node').write_text('#!/bin/sh\nexit 0\n', encoding='utf-8')
    (managed / 'node').chmod(0o755)
    # Mock Python as a shell function; exercise the actual parent-shell function,
    # including spaces, rather than borrowing the developer's installed Node.
    script = function + '''
python3() { printf '%s\n' "$RUNTIME_TEST_BIN"; }
SCRIPT_DIR=/nonexistent
prepare_runtime || exit 10
test "$(command -v node)" = "$RUNTIME_TEST_BIN/node" || exit 11
python3() { return 7; }
if prepare_runtime; then exit 12; fi
'''
    env = dict(os.environ, RUNTIME_TEST_BIN=str(managed))
    result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True, encoding='utf-8')
    assert result.returncode == 0, result.stderr


def test_sync_home_selects_native_node_before_build_or_destination_write():
    launcher = (Path(__file__).resolve().parents[1] / 'bin/desktron').read_text(encoding='utf-8')
    sync = launcher.split('  sync-home)')[1].split('    ;;')[0]
    assert sync.index('prepare_runtime') < sync.index('build-builtin-apps.sh')
    assert sync.index('prepare_runtime') < sync.index('mkdir -p "$dest"')


@pytest.mark.parametrize('restore_fails', [False, True])
def test_failed_runtime_swap_never_discards_previous_directory(tmp_path, monkeypatch, restore_fails):
    monkeypatch.setattr(runtime, '_data_root', lambda: str(tmp_path))
    monkeypatch.setattr(runtime, 'node_command', lambda: '')
    monkeypatch.setattr(runtime, '_node_platform', lambda: ('linux', 'x64'))
    monkeypatch.setattr(runtime, '_download', lambda *args: None)
    monkeypatch.setattr(runtime, '_verify_archive', lambda *args: None)
    monkeypatch.setattr(runtime, '_node_ok', lambda *args: True)
    final = Path(runtime._managed_node_home())
    final.mkdir(parents=True)
    (final / 'old-data').write_text('keep', encoding='utf-8')
    def extract(archive, target):
        (Path(target) / final.name).mkdir()
    monkeypatch.setattr(runtime, '_safe_extract', extract)
    replace = os.replace
    def fail_new_install(source, target):
        if Path(source).parent.name == 'unpack' or (restore_fails and Path(source).name == 'previous'):
            raise OSError('injected swap failure')
        return replace(source, target)
    monkeypatch.setattr(runtime.os, 'replace', fail_new_install)
    with pytest.raises((OSError, RuntimeError)):
        runtime.install_node()
    if restore_fails:
        backups = list((tmp_path / 'runtime').glob('node-setup-*/previous/old-data'))
        assert len(backups) == 1 and backups[0].read_text(encoding='utf-8') == 'keep'
    else:
        assert (final / 'old-data').read_text(encoding='utf-8') == 'keep'
