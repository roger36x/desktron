#!/usr/bin/env python3
"""按 scripts/payload-excludes.txt 复制发行 payload(跨平台，等价于 rsync --exclude-from)。

Windows 没有 rsync；Windows 的 sync-home 与发行打包用本模块。macOS 的 bash 流程仍用 rsync，
两者共用同一份排除清单，语义按 rsync 的规则解释：

- `/x`：只匹配 payload 顶层的 x；
- `x`、`*.pyc`：匹配任意层级上名称相同的文件或目录；
- `a/b`(含斜杠、不以斜杠开头)：匹配相对路径以 a/b 结尾的条目。
"""
import fnmatch
import hashlib
import os
import shutil
import stat
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
EXCLUDES = os.path.join(ROOT, "scripts", "payload-excludes.txt")


def load_patterns(path=EXCLUDES):
    patterns = []
    with open(path, encoding="utf-8") as stream:
        for raw in stream:
            line = raw.strip()
            if line and not line.startswith("#"):
                patterns.append(line)
    return patterns


def excluded(rel, patterns):
    """rel 为以 / 分隔的相对路径。"""
    rel = rel.strip("/")
    name = rel.rsplit("/", 1)[-1]
    for pattern in patterns:
        if pattern.startswith("/"):
            if fnmatch.fnmatchcase(rel, pattern[1:].rstrip("/")):
                return True
        elif "/" in pattern.rstrip("/"):
            pat = pattern.rstrip("/")
            if fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(rel, "*/" + pat):
                return True
        elif fnmatch.fnmatchcase(name, pattern.rstrip("/")):
            return True
    return False


def iter_payload(source, patterns):
    """按排除规则遍历 source，产出 (相对路径, 是否目录)。不跟随符号链接。"""
    for dirpath, dirnames, filenames in os.walk(source, topdown=True, followlinks=False):
        rel_dir = os.path.relpath(dirpath, source).replace(os.sep, "/")
        rel_dir = "" if rel_dir == "." else rel_dir
        keep = []
        for name in sorted(dirnames):
            rel = (rel_dir + "/" + name).lstrip("/")
            if excluded(rel, patterns):
                continue
            full = os.path.join(dirpath, name)
            if os.path.islink(full):
                yield rel, False
                continue
            keep.append(name)
            yield rel, True
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = (rel_dir + "/" + name).lstrip("/")
            if not excluded(rel, patterns):
                yield rel, False


def sync(source, dest, delete=True, patterns=None):
    """把 source 的 payload 同步到 dest；delete=True 时删除 dest 中多出的条目。"""
    patterns = load_patterns() if patterns is None else patterns
    os.makedirs(dest, exist_ok=True)
    wanted = set()
    for rel, is_dir in iter_payload(source, patterns):
        wanted.add(rel)
        src = os.path.join(source, *rel.split("/"))
        dst = os.path.join(dest, *rel.split("/"))
        if is_dir:
            os.makedirs(dst, exist_ok=True)
            continue
        if os.path.islink(src):
            if os.path.lexists(dst):
                os.unlink(dst)
            try:
                os.symlink(os.readlink(src), dst)
            except OSError:
                real = os.path.realpath(src)
                if os.path.isdir(real):
                    shutil.copytree(real, dst)
                elif os.path.isfile(real):
                    shutil.copy2(real, dst)
            continue
        try:
            if (os.path.isfile(dst) and os.path.getsize(dst) == os.path.getsize(src)
                    and int(os.path.getmtime(dst)) == int(os.path.getmtime(src))):
                continue
        except OSError:
            pass
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copy2(src, dst)
    if delete:
        for dirpath, dirnames, filenames in os.walk(dest, topdown=False):
            for name in filenames + dirnames:
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, dest).replace(os.sep, "/")
                if rel in wanted or rel == ".payload-rev":
                    continue
                if os.path.isdir(full) and not os.path.islink(full):
                    if not os.listdir(full):
                        os.rmdir(full)
                    continue
                os.unlink(full)
    return len(wanted)


def fingerprint(root):
    """与 bash sync-home 相同的 payload 指纹算法(排除 .payload-rev 自身)。"""
    digest = hashlib.sha256()
    root = os.path.abspath(root)
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        dirnames.sort()
        filenames.sort()
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if rel == ".payload-rev":
                continue
            info = os.lstat(full)
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                kind, data = b"L", os.readlink(full).encode()
            elif stat.S_ISDIR(info.st_mode):
                kind, data = b"D", b""
            elif stat.S_ISREG(info.st_mode):
                kind = b"F"
                with open(full, "rb") as stream:
                    data = stream.read()
            else:
                continue
            digest.update(rel.encode() + b"\0" + kind + str(mode).encode() + b"\0")
            digest.update(hashlib.sha256(data).digest())
    return digest.hexdigest()


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("用法: payload.py <源码根> <目标目录>", file=sys.stderr)
        sys.exit(2)
    count = sync(sys.argv[1], sys.argv[2])
    print("已同步 %d 项" % count)
