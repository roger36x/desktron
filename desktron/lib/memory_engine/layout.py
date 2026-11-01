"""记忆工作层的分层布局契约与懒迁移(v3,语义正交)。

- <ws>/journal 流水:发生过什么。隔离 recap 自动或按请求生成,后端免闸只追加(旧名 s0)
- <ws>/notes  项目模型:记忆引擎整篇重写的当前态,免闸(旧名 s1)
- <ws>/sot    SOT:现在是什么。每工作区固定几篇主题文档,有界、合入不追加,过闸人审编辑(旧 shared 改造)
- _sealed     红线封存:什么不能出本机。全局、与分层正交(旧名 private)

promote=把成熟笔记的结论合入 SOT 文档(人审编辑动作),不是挪文件。
本模块只认显式 root(记忆实例根,如 <数据根>/.memory),不感知宿主配置。
"""
import os
import shutil
from importlib import resources

BUCKETS = ("journal", "notes", "sot", "sealed")
# 兼容别名(旧 tier/band 名 → 新 band):private/temp/s0/s1/shared 仍被接受。
ALIAS = {"private": "sealed", "temp": "journal", "s0": "journal",
         "s1": "notes", "shared": "sot"}

_MIGRATED = set()   # 已迁移的 root(进程内缓存,免每次 stat 标记文件)


def state_dir(root, *sub):
    """记忆实例的运行态 sidecar 目录(`.curator/`,可再拼子路径)。

    经引擎的 resolve_dir 拿路径而不是自己拼,保持与内核同一个口径;内核不可用时兜底直拼。
    (旧名 `.kbkit/` 的兼容与一次性迁移已于 2026-08-25 撤除,见 curator.state.resolve_dir。)
    """
    try:
        from curator.state import resolve_dir
        base = resolve_dir(root)
    except Exception:
        base = os.path.join(root, ".curator")
    return os.path.join(base, *sub)


def seed_text(name):
    """实例种子(ontology/rules)的权威文本,包数据单一源。"""
    return resources.files("memory_engine").joinpath(f"seeds/{name}").read_text(encoding="utf-8")


def workspace_layout(root, ws):
    """(root, 工作区 id) → 各 band 绝对路径。唯一出口;含旧键别名(过渡期兼容)。"""
    migrate(root)
    lay = {
        "journal": os.path.join(root, ws, "journal"),
        "notes":  os.path.join(root, ws, "notes"),
        "sot":    os.path.join(root, ws, "sot"),
        "sealed": os.path.join(root, "_sealed"),
    }
    for old, new in ALIAS.items():
        lay[old] = lay[new]
    return lay


def ensure_instance(root):
    """root 缺 ontology/rules 时用种子物化(运行态实例不入仓,首写时建)。"""
    os.makedirs(root, exist_ok=True)
    for name in ("ontology.yaml", "rules.yaml"):
        dst = os.path.join(root, name)
        if not os.path.exists(dst):
            with open(dst, "w", encoding="utf-8") as f:
                f.write(seed_text(name))


def migrate(root):
    """懒迁移链:v2(band-first→workspace-first)→ v3(s1/shared→notes)→ v4(s0→journal)。幂等。"""
    if root in _MIGRATED:
        return
    state_dir(root)          # 先确保 sidecar 目录在,布局标记落在里面
    _migrate_v2(root)
    _migrate_v3(root)
    _migrate_v4(root)
    _MIGRATED.add(root)


def _reindex(root):
    """并仓后同进程调 curator(不再 spawn)。迁移期出错不该阻断,照旧吞掉。"""
    try:
        import contextlib
        import io

        from curator.cli import main as curator_main
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            curator_main(["--root", root, "reindex"])
    except Exception:
        pass


def _move_children(src, dst):
    if not os.path.isdir(src):
        return
    os.makedirs(dst, exist_ok=True)
    for n in os.listdir(src):
        s, d = os.path.join(src, n), os.path.join(dst, n)
        if not os.path.exists(d):
            shutil.move(s, d)
    try:
        os.rmdir(src)
    except OSError:
        pass


def _swap_stale_ontology(root, freshness_key):
    """实例 ontology 缺新 band 声明时换成种子(旧声明会让 curator 找错目录)。"""
    dst = os.path.join(root, "ontology.yaml")
    if not os.path.exists(dst):
        return
    with open(dst, encoding="utf-8") as f:
        if freshness_key in f.read():
            return
    with open(dst, "w", encoding="utf-8") as f:
        f.write(seed_text("ontology.yaml"))


def _migrate_v2(root):
    """旧布局(band-first:temp/<ws>、local/{private,shared}/<ws>)→ workspace-first。"""
    marker = state_dir(root, "layout-v2")
    if os.path.exists(marker) or not os.path.isdir(root):
        return
    try:
        old_temp = os.path.join(root, "temp")
        if os.path.isdir(old_temp):
            for ws in list(os.listdir(old_temp)):
                if ws.startswith(".") or not os.path.isdir(os.path.join(old_temp, ws)):
                    continue
                _move_children(os.path.join(old_temp, ws), os.path.join(root, ws, "s0"))
            try: os.rmdir(old_temp)
            except OSError: pass
        old_shared = os.path.join(root, "local", "shared")
        if os.path.isdir(old_shared):
            for ws in list(os.listdir(old_shared)):
                if ws.startswith(".") or not os.path.isdir(os.path.join(old_shared, ws)):
                    continue
                _move_children(os.path.join(old_shared, ws), os.path.join(root, ws, "shared"))
            try: os.rmdir(old_shared)
            except OSError: pass
        _move_children(os.path.join(root, "local", "private"), os.path.join(root, "_sealed"))
        try: os.rmdir(os.path.join(root, "local"))
        except OSError: pass
        _swap_stale_ontology(root, "{topic}")
        _reindex(root)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("layout v2 (workspace-first) migrated\n")
    except Exception:
        pass


def _migrate_v3(root):
    """v2(s0/s1/shared)→ v3(s0/notes/sot):s1 与 shared 的存量都是判断档案,平移进 notes;
    sot(当前权威态)从空开始由人策展,不预建目录。"""
    marker = state_dir(root, "layout-v3")
    if os.path.exists(marker) or not os.path.isdir(root):
        return
    try:
        for ws in list(os.listdir(root)):
            wsd = os.path.join(root, ws)
            if ws.startswith((".", "_")) or not os.path.isdir(wsd):
                continue
            for old in ("s1", "shared"):
                _move_children(os.path.join(wsd, old), os.path.join(wsd, "notes"))
        _swap_stale_ontology(root, "{topic}/notes")
        _reindex(root)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("layout v3 (s0/notes/sot) migrated\n")
    except Exception:
        pass


def _migrate_v4(root):
    """v3(s0)→ v4(journal):s0 是 s0/s1 那套分代编号的残留,notes 早已改成语义名,这一层跟上。
    改后层名与 journal skill、`curator journal` 命令、`journal.md` 文件名一致,少一个要解释的概念。"""
    marker = state_dir(root, "layout-v4")
    if os.path.exists(marker) or not os.path.isdir(root):
        return
    try:
        for ws in list(os.listdir(root)):
            wsd = os.path.join(root, ws)
            if ws.startswith((".", "_")) or not os.path.isdir(wsd):
                continue
            _move_children(os.path.join(wsd, "s0"), os.path.join(wsd, "journal"))
        _swap_stale_ontology(root, "{topic}/journal")
        _reindex(root)
        os.makedirs(os.path.dirname(marker), exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write("layout v4 (s0 -> journal) migrated\n")
    except Exception:
        pass
