#!/usr/bin/env python3
"""cursor-agent hook 准入闸。

Cursor hooks 使用 camelCase 事件名与 JSON stdout permission。这里复用 codex_gate.py 的
memory/knowledge 路径判定,只适配 Cursor 的 payload/tool 名。
"""
import json
import os
import sys

def _emit(permission, msg=None):
    out = {"permission": permission}
    if msg:
        out["user_message"] = msg
        out["agent_message"] = msg
    try:
        sys.stdout.write(json.dumps(out, ensure_ascii=False) + "\n")
        sys.stdout.flush()
    except Exception:
        pass
    sys.exit(0)


def _allow():
    _emit("allow")


def _deny(msg):
    _emit("deny", msg)


try:
    from codex_gate import (
        _PATCH_FILE,
        _SH_READ,
        _SH_WRITE,
        _abs,
        _blocked_write,
        _check_archive_read,
        _git_inside_root,
        _SH_BULK_READ,
        _SH_STDIN,
        _skip,
    )
except Exception:
    _allow()


def _cwd(data, ti):
    cwd = ti.get("cwd") or ti.get("working_directory") or data.get("cwd") or ""
    if cwd:
        return cwd
    roots = data.get("workspace_roots")
    if isinstance(roots, list) and roots:
        return roots[0]
    return ""


def _path_value(ti):
    for key in ("file_path", "path", "target_file", "target", "filename", "filePath"):
        val = ti.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _check_read(path, cwd, bulk=True):
    if not path:
        return
    _check_archive_read(path, cwd, bulk=bulk, deny=_deny)
    ap = _abs(path, cwd)
    if _skip(ap):
        return
    root = _git_inside_root(ap)
    if root:
        nid = os.path.basename(path)[:-3] if path.endswith(".md") else os.path.basename(path)
        _deny(f"准入闸拦截:裸读 git 内笔记 {path} —— 走 `curator --root {root} show --id {nid}` "
              "(计访问 + 摆 confidence/freshness/就近覆盖/冲突)。工作层与缓存目录可直接读。")


def _check_write(path, cwd, action):
    if not path:
        return
    where = _blocked_write(_abs(path, cwd))
    if where:
        _deny(f"准入闸拦截:{action} 改 {where} 受管路径 {path} —— 必经 "
              f"{'mem_add.py' if where == 'memory' else 'mem_add.py --tier fact --kind fact'} 命令(底层调 curator 过闸),"
              "不要直接改文件。temp 免闸。")


def _check_patch(cmd, cwd):
    for m in _PATCH_FILE.finditer(cmd):
        _check_write(m.group(1).strip().strip("\"'"), cwd, "apply_patch")


def _check_shell(cmd, cwd):
    for m in _SH_WRITE.finditer(cmd):
        p = m.group(2)
        where = _blocked_write(_abs(p, cwd))
        if where:
            _deny(f"准入闸拦截:命令试图写 {where} 受管路径 {p} —— 必经 "
                  f"{'mem_add.py' if where == 'memory' else 'mem_add.py --tier fact --kind fact'} 命令(用 curator 过闸),不要 shell 直写。temp 免闸。")
    # 请求快照:任何读法都拒;整份读回只认会吐全文的命令。
    for raw in ([m.group(3) for m in _SH_READ.finditer(cmd)]
                + [m.group(2) for m in _SH_STDIN.finditer(cmd)]):
        _check_archive_read(raw, cwd, bulk=False, deny=_deny)
    for m in _SH_BULK_READ.finditer(cmd):
        _check_archive_read(m.group(3), cwd, deny=_deny)
    for m in _SH_READ.finditer(cmd):
        p = m.group(3)
        _check_read(p, cwd, bulk=False)


def _main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        _allow()
    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        ti = {}
    event = data.get("hook_event_name") or ""
    tn = data.get("tool_name") or ""
    cwd = _cwd(data, ti)

    if event == "beforeReadFile":
        _check_read(data.get("file_path") or _path_value(ti), cwd)
        _allow()

    if event == "beforeShellExecution":
        # Cursor 把命令放在顶层;不同版本也见过落在 tool_input 里,两处都取。
        cmd = data.get("command") or ti.get("command") or ""
        if isinstance(cmd, str):
            _check_shell(cmd, cwd)
        _allow()

    if tn in ("Read", "read_file", "ReadFile"):
        _check_read(_path_value(ti), cwd)
        _allow()

    if tn in ("Write", "Edit", "MultiEdit", "write_file", "Delete"):
        _check_write(_path_value(ti), cwd, tn)
        _allow()

    cmd = ti.get("command")
    if isinstance(cmd, str) and cmd:
        if tn == "apply_patch" or "*** Begin Patch" in cmd:
            _check_patch(cmd, cwd)
        if tn in ("Shell", "Bash") or event == "preToolUse":
            _check_shell(cmd, cwd)
    _allow()


def main():
    try:
        _main()
    except SystemExit:
        raise
    except Exception:
        _allow()


if __name__ == "__main__":
    main()
