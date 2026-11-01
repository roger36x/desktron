"""Filesystem contract: real long paths, isolated data, no login or OS changes."""
import builtins
import io
import ntpath
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
from unittest import mock

import pytest

LIB = Path(__file__).resolve().parents[1] / "lib"
sys.path.insert(0, str(LIB))
import oscompat
import windows_fs


@pytest.mark.parametrize("path", [
    "C:\\" + "a" * 240,
    "C:/" + "a" * 240,
    "\\\\server\\share\\" + "a" * 240,
    b"C:\\" + b"a" * 240,
    Path("C:/" + "a" * 240),
])
def test_filesystem_path_expands_without_changing_identity(path):
    converted = windows_fs.filesystem_path(path)
    decoded = os.fsdecode(converted)
    assert decoded.startswith("\\\\?\\")
    expected = ntpath.abspath(os.fsdecode(path))
    logical = ("\\\\" + decoded[8:] if decoded.startswith("\\\\?\\UNC\\") else decoded[4:])
    assert logical == expected
    assert isinstance(converted, bytes) == isinstance(path, bytes)


@pytest.mark.parametrize("path", ["", "a.txt", "C:\\short", b"C:\\short", 7, None,
                                  "\\\\?\\C:\\" + "a" * 240,
                                  "\\\\.\\pipe\\example"])
def test_short_descriptor_and_explicit_device_paths_are_preserved(path):
    assert windows_fs.filesystem_path(path) == path


def test_relative_path_is_normalized_before_prefix():
    original = "parent/../" + "a" * 250
    with mock.patch.object(windows_fs.ntpath, "abspath", return_value="C:\\base\\" + "a" * 250) as absolute:
        result = windows_fs.filesystem_path(original)
    absolute.assert_called_once_with(original)
    assert result == "\\\\?\\C:\\base\\" + "a" * 250


def test_utf16_length_includes_surrogate_pairs():
    assert windows_fs.filesystem_path("C:\\" + "😀" * 120).startswith("\\\\?\\")


@pytest.mark.parametrize("suffix", [".", " "])
def test_ambiguous_identity_is_not_silently_changed(suffix):
    with pytest.raises(ValueError, match="Ambiguous"):
        windows_fs.filesystem_path("C:\\" + "a" * 240 + suffix)


@pytest.mark.parametrize("target", ["C:\\synthetic\\target", "\\\\?\\C:\\synthetic\\target",
                                    "\\\\server\\share\\synthetic"])
def test_junction_payload_has_one_nt_namespace_and_byte_offsets(target):
    import struct
    payload = windows_fs._junction_buffer(target)
    tag, length, reserved, start, size, display_start, display_size = struct.unpack("<IHHHHHH", payload[:16])
    assert tag == 0xA0000003 and length == len(payload) - 8 and reserved == 0
    paths = payload[16:]
    substitute = paths[start:start + size].decode("utf-16-le")
    display = paths[display_start:display_start + display_size].decode("utf-16-le")
    assert substitute.startswith("\\??\\")
    assert "\\\\?\\" not in substitute
    assert display == windows_fs.junction_target(target)
    assert paths[size:size + 2] == b"\x00\x00"


def test_wrapper_preserves_fd_and_dir_fd_semantics():
    called = mock.Mock(return_value="ok")
    wrapped = windows_fs._path_call(called, ((0, "path"),))
    assert wrapped(12) == "ok"
    called.assert_called_with(12)
    path = "a" * 250
    wrapped(path=path, dir_fd=9)
    called.assert_called_with(path=path, dir_fd=9)
    binary = windows_fs._path_call(called, ((0, "src"), (1, "dst")))
    binary(path, path, src_dir_fd=8, dst_dir_fd=9)
    called.assert_called_with(path, path, src_dir_fd=8, dst_dir_fd=9)


@pytest.mark.parametrize("error", [FileNotFoundError("missing"), PermissionError("denied"),
                                  OSError("sharing violation")])
def test_wrapper_propagates_errors_without_fallback(error):
    wrapped = windows_fs._path_call(mock.Mock(side_effect=error), ((0, "path"),))
    with pytest.raises(type(error)) as got:
        wrapped("C:\\" + "a" * 240)
    assert got.value is error


def test_scandir_facade_keeps_logical_path_and_uses_adapted_stat():
    native = mock.Mock(name="entry")
    native.name = "history.jsonl"
    directory = "C:\\logical\\" + "x" * 235
    entry = windows_fs._Entry(native, directory)
    assert os.fspath(entry) == os.path.join(directory, "history.jsonl")
    with mock.patch.object(os, "stat", return_value="info") as lookup:
        assert entry.stat(follow_symlinks=False) == "info"
        lookup.assert_called_once_with(entry.path, follow_symlinks=False)
    native.stat.assert_not_called()


def test_short_absolute_paths_and_scandir_metadata_keep_fast_native_path():
    with mock.patch.object(windows_fs.ntpath, "abspath") as resolve:
        assert windows_fs.filesystem_path("C:\\short.txt") == "C:\\short.txt"
        resolve.assert_not_called()
    native = mock.Mock()
    native.name = "history.jsonl"
    entry = windows_fs._Entry(native, "C:\\short")
    assert entry.stat(follow_symlinks=False) == native.stat.return_value
    native.stat.assert_called_once_with(follow_symlinks=False)


def test_posix_install_is_a_noop_in_fresh_process():
    # Only mock the platform at our module boundary; never load Windows shims
    # into a POSIX test process or compare to modules imported before oscompat.
    code = '''import os,builtins,io,sys
sys.path.insert(0,sys.argv[1])
import windows_fs
before=(os.stat,os.scandir,os.unlink,builtins.open,io.open)
windows_fs.sys=type('Platform',(),{'platform':'linux'})()
windows_fs.install()
assert before==(os.stat,os.scandir,os.unlink,builtins.open,io.open)
'''
    result = subprocess.run([sys.executable, "-S", "-c", code, str(LIB)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


@pytest.mark.parametrize("relative", [".", "home", "../outside/home"])
def test_resident_cleanup_validates_deleted_parent_not_only_home(tmp_path, relative):
    from shepherd.store import Conflict
    from shepherd.unified_runners import _clear_private_tree
    store = tmp_path / "store"
    store.mkdir()
    retained = store / "retained.json"
    retained.write_text("keep", encoding="utf-8")
    with pytest.raises(Conflict):
        _clear_private_tree(store / relative, store)
    assert retained.read_text(encoding="utf-8") == "keep"


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Windows filesystem")


def _long_directory(root):
    path = root
    while len(str(path)) < 285:
        path /= "synthetic-" + "x" * 35
    path.mkdir(parents=True)
    return path


@windows_only
def test_real_windows_create_read_stat_enumerate_rename_delete(tmp_path):
    directory = _long_directory(tmp_path)
    file = directory / "中文-😀.jsonl"
    text = '{"fixture":"中文😀"}\n'
    with builtins.open(file, "w", encoding="utf-8", newline="") as stream:
        stream.write(text)
    assert file.read_text(encoding="utf-8") == text
    with io.open(file, "r", encoding="utf-8") as stream:
        assert stream.read() == text
    fd = os.open(file, os.O_RDONLY)
    try:
        assert os.read(fd, 200).decode("utf-8") == text
    finally:
        os.close(fd)
    assert stat.S_ISREG(os.stat(file).st_mode)
    assert os.lstat(file).st_size == len(text.encode("utf-8"))
    assert os.listdir(directory) == [file.name]
    with os.scandir(directory) as scan:
        entry = next(scan)
        assert entry.path == str(file)
        assert os.fspath(entry) == str(file)
        assert entry.is_file() and not entry.is_dir()
        assert entry.stat().st_size == file.stat().st_size
    assert str(directory) in [base for base, _, _ in os.walk(tmp_path)]
    moved = directory / "renamed.jsonl"
    os.rename(file, moved)
    os.replace(moved, file)
    os.unlink(file)
    assert not file.exists()
    shutil.rmtree(directory)
    assert not directory.exists()


@windows_only
def test_real_windows_short_parent_long_filename_stat_and_cleanup(tmp_path):
    # Regression for the actual threshold shape: the parent fits, only the
    # appended history filename crosses MAX_PATH. Scandir alone is not enough.
    parent = tmp_path / ("p" * max(1, 222 - len(str(tmp_path)) - 1))
    parent.mkdir()
    file = parent / ("h" * 45 + ".jsonl")
    assert len(str(parent)) < 240 and len(str(file)) >= 260
    file.write_text("synthetic", encoding="utf-8")
    with os.scandir(parent) as scan:
        entry = next(scan)
        assert entry.stat(follow_symlinks=False).st_size == 9
    shutil.rmtree(parent)
    assert not parent.exists()


@windows_only
def test_real_windows_resident_rebuilds_twice_without_touching_store(tmp_path):
    from shepherd.unified_runners import _clear_private_tree
    store = tmp_path / "store"
    store.mkdir()
    authoritative = store / "retained.json"
    authoritative.write_text("synthetic source", encoding="utf-8")
    home = store / "resident" / "claude" / ("ubind_claude_" + "a" * 32) / "home"
    for _ in range(2):
        project = _long_directory(home / "memory.bootstrap-config" / "projects")
        history = project / "00000000-0000-0000-0000-000000000000.jsonl"
        history.write_text("synthetic history", encoding="utf-8")
        _clear_private_tree(home, store)
        assert not home.parent.exists()
        assert authoritative.read_text(encoding="utf-8") == "synthetic source"


@windows_only
def test_real_windows_resident_removes_readonly_git_objects_twice(tmp_path):
    from shepherd.unified_runners import _clear_private_tree
    store = tmp_path / "store"
    store.mkdir()
    source = store / "authoritative.json"
    source.write_text("keep", encoding="utf-8")
    home = store / "resident" / "claude" / "fixture" / "home"
    for _ in range(2):
        objects = _long_directory(home / "tmp" / "curator-test-fixture" / "inst" / ".git" / "objects")
        readonly = objects / "fixture-object"
        readonly.write_bytes(b"synthetic git object")
        os.chmod(readonly, stat.S_IREAD)
        try:
            assert readonly.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
            _clear_private_tree(home, store)
            assert not home.parent.exists()
            assert source.read_text(encoding="utf-8") == "keep"
        finally:
            if readonly.exists():
                os.chmod(readonly, stat.S_IWRITE)


@windows_only
@pytest.mark.parametrize("kind", ["writable", "hardlink", "directory", "sharing"])
def test_private_cleanup_does_not_hide_other_windows_denials(tmp_path, kind):
    from shepherd.unified_runners import _remove_private_readonly_file
    file = tmp_path / "fixture"
    if kind == "directory":
        file.mkdir()
        function = os.rmdir
    else:
        file.write_bytes(b"keep")
        function = os.unlink
    if kind == "hardlink":
        os.link(file, tmp_path / "outside-link")
    if kind in ("hardlink", "sharing"):
        os.chmod(file, stat.S_IREAD)
    error = PermissionError("synthetic denial")
    error.winerror = 32 if kind == "sharing" else 5
    try:
        before = file.lstat().st_file_attributes
        with pytest.raises(PermissionError) as got:
            _remove_private_readonly_file(function, str(file), (PermissionError, error, None))
        assert got.value is error
        assert file.exists() and file.lstat().st_file_attributes == before
    finally:
        os.chmod(file, stat.S_IWRITE)


@windows_only
def test_real_windows_resident_rejects_outside_and_junction_parent(tmp_path):
    from shepherd.store import Conflict
    from shepherd.unified_runners import _clear_private_tree
    store = tmp_path / "store"
    store.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = outside / "retained.txt"
    retained.write_text("keep", encoding="utf-8")
    with pytest.raises(Conflict):
        _clear_private_tree(outside / "home", store)
    junction = store / "link"
    oscompat.link_path(outside, junction, target_is_directory=True)
    try:
        with pytest.raises(Conflict):
            _clear_private_tree(junction / "home", store)
        assert retained.read_text(encoding="utf-8") == "keep"
    finally:
        oscompat.remove_link(junction)


@windows_only
def test_real_windows_recursive_cleanup_does_not_follow_junction(tmp_path):
    owned = _long_directory(tmp_path / "owned")
    outside = tmp_path / "outside"
    outside.mkdir()
    retained = outside / "retained.txt"
    retained.write_text("keep", encoding="utf-8")
    oscompat.link_path(outside, owned / "linked", target_is_directory=True)
    shutil.rmtree(tmp_path / "owned")
    assert retained.read_text(encoding="utf-8") == "keep"


@windows_only
def test_real_windows_long_store_identity_and_link_ancestor(tmp_path):
    from shepherd.store import Conflict
    from shepherd.unified_runners import _clear_private_tree
    store = _long_directory(tmp_path / "store")
    retained = store / "retained.json"
    retained.write_text("keep", encoding="utf-8")
    home = store / "resident" / "binding" / "home"
    home.mkdir(parents=True)
    (home / "history.jsonl").write_text("synthetic", encoding="utf-8")
    _clear_private_tree(home, store)
    assert not home.parent.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = store / "link"
    oscompat.link_path(outside, link, target_is_directory=True)
    try:
        with pytest.raises(Conflict):
            _clear_private_tree(link / "binding" / "home", store)
        assert retained.read_text(encoding="utf-8") == "keep"
        assert outside.exists()
    finally:
        oscompat.remove_link(link)


@windows_only
def test_real_windows_bytes_and_relative_paths_keep_enumeration_identity(tmp_path, monkeypatch):
    directory = _long_directory(tmp_path)
    file = directory / "history.jsonl"
    file.write_bytes(b"line\n\x1a\x00tail")
    raw_directory = os.fsencode(directory)
    with os.scandir(raw_directory) as scan:
        entry = next(scan)
        assert entry.path == os.fsencode(file)
        assert entry.stat().st_size == 11
        assert entry.inode() == os.lstat(file).st_ino
    monkeypatch.chdir(tmp_path)
    relative = file.relative_to(tmp_path)
    assert relative.read_bytes() == b"line\n\x1a\x00tail"
    with os.scandir(relative.parent) as scan:
        assert next(scan).path == str(relative)


@windows_only
def test_real_windows_relative_symlink_target_is_relative_to_long_link(tmp_path):
    deep = _long_directory(tmp_path)
    target = deep / "target"
    target.mkdir()
    retained = target / "retained.txt"
    retained.write_text("keep", encoding="utf-8")
    links = deep / "links"
    links.mkdir()
    alias = links / "alias"
    os.symlink(os.path.join("..", "target"), alias, target_is_directory=True)
    try:
        assert alias.is_symlink()
        assert (alias / "retained.txt").read_text(encoding="utf-8") == "keep"
    finally:
        oscompat.remove_link(alias)
    assert retained.read_text(encoding="utf-8") == "keep"


@windows_only
def test_real_windows_long_junction_failure_removes_only_owned_directory(tmp_path, monkeypatch):
    import ctypes
    target = _long_directory(tmp_path / "target")
    retained = target / "retained.txt"
    retained.write_text("keep", encoding="utf-8")
    alias = tmp_path / "alias"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    def denied(*args):
        ctypes.set_last_error(5)
        return False
    kernel.DeviceIoControl = mock.Mock(side_effect=denied)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel)
    with pytest.raises(PermissionError):
        windows_fs.create_junction(target, alias)
    assert not alias.exists()
    assert retained.read_text(encoding="utf-8") == "keep"
    alias.mkdir()
    existing = alias / "existing.txt"
    existing.write_text("do not remove", encoding="utf-8")
    with pytest.raises(FileExistsError):
        windows_fs.create_junction(target, alias)
    assert existing.read_text(encoding="utf-8") == "do not remove"


@windows_only
def test_install_is_idempotent_and_missing_paths_still_fail(tmp_path):
    before = (os.stat, os.scandir, os.unlink, builtins.open, io.open)
    oscompat.install()
    windows_fs.install()
    assert before == (os.stat, os.scandir, os.unlink, builtins.open, io.open)
    absent = _long_directory(tmp_path) / "missing"
    with pytest.raises(FileNotFoundError):
        absent.stat()
    with pytest.raises(FileNotFoundError):
        absent.read_text(encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        shutil.rmtree(absent)
