"""Pure protected-path policy shared by UI browsing and native agents."""
import os
import sys


def _expanded(paths, *, home=None, windows=False):
    home = os.path.abspath(os.path.expanduser(home or "~"))
    values = []
    for value in paths:
        if value == "~":
            path = home
        elif value.startswith("~/"):
            path = os.path.join(home, value[2:])
        else:
            path = os.path.abspath(os.path.expanduser(value))
        values.append(os.path.normcase(path) if windows else path)
    return tuple(values)


def protected_dirs(*, platform=None, home=None):
    """Directories an automatic traversal must not enter without user intent."""
    platform = platform or sys.platform
    if platform == "win32":
        return _expanded((
            "~/Desktop", "~/Documents", "~/Downloads", "~/Music", "~/Pictures",
            "~/Videos", "~/AppData", "~/OneDrive",
        ), home=home, windows=True)
    return _expanded((
        "~/Desktop", "~/Documents", "~/Downloads", "~/Music", "~/Pictures",
        "~/Movies", "~/Library", "~/.Trash", "/Volumes",
    ), home=home)


PROTECTED_DIRS = protected_dirs()
MACOS_AGENT_PROTECTED_DIRS = PROTECTED_DIRS if sys.platform == "darwin" else ()


def is_protected_dir(path, *, roots=None, platform=None):
    """Return whether path is one of the protected roots or a descendant."""
    platform = platform or sys.platform
    try:
        value = os.path.abspath(os.path.expanduser(str(path or "")))
    except Exception:
        return False
    if platform == "win32":
        value = os.path.normcase(value)
    selected = PROTECTED_DIRS if roots is None else tuple(roots)
    return any(value == root or value.startswith(root + os.sep) for root in selected)
