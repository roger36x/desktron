"""Windows filesystem-boundary adaptation, without changing stored path identities.

MAX_PATH can still apply when the machine policy is disabled. Use extended paths
at Python filesystem calls, not in session IDs, subprocess arguments or config.
POSIX is untouched. Do not suppress missing-file, permission or sharing errors.
"""
from functools import wraps
import builtins
import io
import ntpath
import os
import stat
import sys

_INSTALLED = False
_PREFIX = "\\\\?\\"
_DEVICE = "\\\\.\\"


def filesystem_path(path):
    """Expand long Windows filesystem paths; preserve fds, bytes and short paths.

    Normalize relative paths and dot components *before* adding the prefix.
    An already explicit device/extended path is not reinterpreted. Ambiguous
    trailing-dot/space components are rejected rather than changing identity.
    """
    if isinstance(path, int) or path is None:
        return path
    value = os.fspath(path)
    text = os.fsdecode(value)
    if not text or text.startswith((_PREFIX, _DEVICE)):
        return value
    # Most I/O uses short absolute paths. Do not add a GetFullPathName kernel
    # call to every read/stat; relative paths still require cwd resolution.
    units = len(text.encode("utf-16-le", errors="surrogatepass")) // 2
    if units < 240 and ntpath.isabs(text) and ntpath.splitdrive(text)[0]:
        return value
    absolute = ntpath.abspath(text)
    if len(absolute.encode("utf-16-le", errors="surrogatepass")) // 2 < 240:
        return value
    # GetFullPathName can strip trailing dots/spaces; inspect the input, not
    # just the already-normalized output, before changing Win32 namespaces.
    parts = text.replace("/", "\\").split("\\")
    if any(part not in ("", ".", "..") and part.endswith((".", " ")) for part in parts):
        raise ValueError("Ambiguous trailing dot/space in a long Windows path")
    expanded = (_PREFIX + "UNC\\" + absolute[2:] if absolute.startswith("\\\\")
                else _PREFIX + absolute)
    return os.fsencode(expanded) if isinstance(value, bytes) else expanded


def junction_target(path):
    """_winapi.CreateJunction adds its own NT prefix to the serialized target.

    Supplying a Win32 extended prefix there creates an invalid double namespace.
    This conversion is only for junction metadata, never ordinary file I/O.
    """
    value = ntpath.normpath(ntpath.abspath(os.fsdecode(path)))
    if value.startswith(_PREFIX + "UNC\\"):
        return "\\\\" + value[8:]
    return value[4:] if value.startswith(_PREFIX) else value


def _junction_buffer(target):
    import struct
    logical = junction_target(target)
    substitute = ("\\??\\UNC\\" + logical[2:] if logical.startswith("\\\\")
                  else "\\??\\" + logical)
    substitute = substitute.encode("utf-16-le")
    display = logical.encode("utf-16-le")
    paths = substitute + b"\x00\x00" + display + b"\x00\x00"
    if len(paths) + 16 > 16384:
        raise ValueError("Junction target exceeds the reparse buffer limit")
    return (struct.pack("<IHHHHHH", 0xA0000003, len(paths) + 8, 0,
                        0, len(substitute), len(substitute) + 2, len(display)) + paths)


def create_junction(target, link):
    """Create an owned junction, including long targets, without changing cwd.

    CPython's _winapi helper checks the unprefixed target with MAX_PATH and
    serializes an extended target with an invalid double NT prefix. For long
    targets, validate with extended I/O and write the normal NT reparse payload.
    On failure only the directory created by this call may be removed.
    """
    logical = junction_target(target)
    link = filesystem_path(ntpath.abspath(os.fsdecode(link)))
    if filesystem_path(logical) == logical:
        import _winapi
        return _winapi.CreateJunction(logical, link)
    if not stat.S_ISDIR(os.stat(filesystem_path(logical)).st_mode):
        raise NotADirectoryError("Junction target is not a directory")
    payload = _junction_buffer(logical)
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateDirectoryW.argtypes = (wintypes.LPCWSTR, wintypes.LPVOID)
    kernel.CreateDirectoryW.restype = wintypes.BOOL
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    if not kernel.CreateDirectoryW(link, None):
        raise ctypes.WinError(ctypes.get_last_error())
    invalid = ctypes.c_void_p(-1).value
    handle = invalid
    try:
        handle = kernel.CreateFileW(link, 0x40000000, 7, None, 3, 0x02200000, None)
        if handle == invalid:
            raise ctypes.WinError(ctypes.get_last_error())
        data = ctypes.create_string_buffer(payload)
        returned = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x000900A4, data, len(payload),
                                      None, 0, ctypes.byref(returned), None):
            raise ctypes.WinError(ctypes.get_last_error())
    except BaseException as error:
        if handle != invalid:
            kernel.CloseHandle(handle)
            handle = invalid
        try:
            os.rmdir(link)
        except OSError as cleanup_error:
            error.add_note("Owned junction cleanup failed: " + type(cleanup_error).__name__)
        raise
    finally:
        if handle != invalid:
            kernel.CloseHandle(handle)


def _path_call(function, names):
    @wraps(function)
    def call(*args, **kwargs):
        args = list(args)
        for index, name in names:
            # Preserve descriptor-relative semantics, including unsupported-call
            # errors on Windows, instead of interpreting relative to process cwd.
            if kwargs.get("dir_fd") is not None or kwargs.get(name + "_dir_fd") is not None:
                continue
            if index < len(args):
                args[index] = filesystem_path(args[index])
            elif name in kwargs:
                kwargs[name] = filesystem_path(kwargs[name])
        return function(*args, **kwargs)
    return call


class _Entry:
    """DirEntry facade: keep the caller's path spelling, never publish a prefix."""
    def __init__(self, entry, directory):
        self._entry = entry
        self.name = entry.name
        self.path = os.path.join(directory, entry.name)

    def __fspath__(self):
        return self.path

    def inode(self):
        return self.stat(follow_symlinks=False).st_ino

    def is_symlink(self):
        return self._entry.is_symlink()

    def is_junction(self):
        checker = getattr(self._entry, "is_junction", None)
        if checker:
            return checker()
        return getattr(self.stat(follow_symlinks=False), "st_reparse_tag", 0) == 0xA0000003

    def stat(self, *, follow_symlinks=True):
        # A short parent may contain a filename that crosses MAX_PATH; the
        # original DirEntry's native stat can otherwise bypass our os.stat.
        if filesystem_path(self.path) == self.path:
            return self._entry.stat(follow_symlinks=follow_symlinks)
        return os.stat(self.path, follow_symlinks=follow_symlinks)

    def _kind(self, checker, predicate, follow_symlinks):
        if follow_symlinks and self.is_symlink():
            try:
                return predicate(self.stat().st_mode)
            except FileNotFoundError:
                return False
        return checker(follow_symlinks=follow_symlinks)

    def is_dir(self, *, follow_symlinks=True):
        return self._kind(self._entry.is_dir, stat.S_ISDIR, follow_symlinks)

    def is_file(self, *, follow_symlinks=True):
        return self._kind(self._entry.is_file, stat.S_ISREG, follow_symlinks)


class _Scan:
    def __init__(self, iterator, directory):
        self._iterator = iterator
        self._directory = directory

    def __iter__(self):
        return self

    def __next__(self):
        return _Entry(next(self._iterator), self._directory)

    def close(self):
        self._iterator.close()

    def __enter__(self):
        self._iterator.__enter__()
        return self

    def __exit__(self, *args):
        return self._iterator.__exit__(*args)


def install():
    """Install once, after oscompat's binary-open, retry and junction policies."""
    global _INSTALLED
    if sys.platform != "win32" or _INSTALLED:
        return
    _INSTALLED = True
    for name in ("open", "stat", "lstat", "mkdir", "rmdir", "unlink", "remove",
                 "listdir", "readlink", "access", "chmod", "utime", "truncate"):
        setattr(os, name, _path_call(getattr(os, name), ((0, "path"),)))
    for name in ("rename", "replace", "link"):
        setattr(os, name, _path_call(getattr(os, name), ((0, "src"), (1, "dst"))))
    # A relative symlink target is relative to its *link*, not process cwd.
    os.symlink = _path_call(os.symlink, ((1, "dst"),))
    builtins.open = _path_call(builtins.open, ((0, "file"),))
    io.open = _path_call(io.open, ((0, "file"),))
    original_scan = os.scandir

    @wraps(original_scan)
    def scandir(path=None):
        iterator = original_scan(filesystem_path(path))
        if isinstance(path, int):
            return iterator
        directory = os.fspath(path) if path is not None else "."
        return _Scan(iterator, directory)

    os.scandir = scandir
