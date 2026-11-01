"""盘上那几份文件的变化看护:发现改动就往 SSE 总线推一条,省掉每一页各自的轮询。

看三类(名字留着 task_watch 是历史,它现在管的不止任务):
  domains/*/TASKS.md             变了推 task.updated
  DATA_HOME/task-ledger.json     任务快照变了也推 task.updated
  .workbench/{apps,domains}.json 变了推 apps.updated

这些文件的道理相同,所以合在一个线程里扫:stat 一份文件是微秒级,再多几份也不值得
另起一个线程各睡各的。


为什么这一轮要放在服务端(2026-08-25 用户实报:一个会话里改了任务,别的会话要等好一会儿
才看得见)。标题和状态写入 `TASKS.md`，Agent 的当前快照由独立 `desk-task` 进程写入
`task-ledger.json`；两者都可能绕过当前后端进程。只在 API 响应后重画会漏掉另一条路径，
于是每一页只能自己定时来问——TaskDock 单源 5 秒一趟、聚合模式 30 秒,工作流页 5 秒,
任务页打开之后干脆不再刷。

把这一轮挪到服务端,两头都划算:本机 stat 一个文件是微秒级,而客户端问一趟经隧道要
0.23~1.9 秒(同日实测)。挪完之后延迟从 5 秒降到 1 秒,远端那一页还能少一串跨区往返。

只报「哪个工作流的清单动了」,不报动了什么:前端收到就去取,取数仍走原来那一条,
这里不复制一份解析逻辑。
"""
import json
import os
import threading

SCAN_SEC = 1.0
_MD = "TASKS.md"
_thread = None
_stop = None


def _fp(path):
    """一份清单的指纹。取 mtime_ns + size,不读内容:这一轮每秒都要跑,而两者一起看
    已经足够——同一纳秒内改成同样长度才会漏,那要求写入快到 stat 分辨不出。"""
    try:
        st = os.stat(path)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _scan(root):
    """domains 下每份 TASKS.md 的指纹。两层:工作流自己一份,容器的自定义子区各一份
    (见 tasks.py 的 tasks_md_path)。"""
    out = {}
    base = os.path.join(root, "domains")
    try:
        tops = [d for d in os.scandir(base) if d.is_dir()]
    except OSError:
        return out
    for d in tops:
        fp = _fp(os.path.join(d.path, _MD))
        if fp:
            out[os.path.join(d.path, _MD)] = fp
        try:
            subs = [s for s in os.scandir(d.path) if s.is_dir()]
        except OSError:
            continue
        for s in subs:
            fp = _fp(os.path.join(s.path, _MD))
            if fp:
                out[os.path.join(s.path, _MD)] = fp
    return out


# 注册表:agent 在对话里部署一个 app、reconcile 补默认工作区依赖,都不经前端 UI,
# 页面原先要靠每 2.5 秒问一次 /workbench/apps-rev 才知道(2026-08-25 实测:经隧道
# 30 秒里 12 次,每次换回 0 字节)。改成这边看着,变了推一条,那条轮询退成兜底。
_REG = ("apps.json", "domains.json")


def _scan_reg(root):
    out = {}
    for name in _REG:
        p = os.path.join(root, ".workbench", name)
        fp = _fp(p)
        if fp:
            out[p] = fp
    return out


def _rev_of(reg):
    """与 /workbench/apps-rev 同口径:两份文件 mtime 的最大值,毫秒。
    前端拿它跟自己记的上一个值比,两条路(事件与兜底轮询)因此认同一个数。"""
    return max((int(fp[0] // 1000000) for fp in reg.values()), default=0)


def _domain_of(root, path):
    """domains/<a>/TASKS.md → "a";domains/<a>/<b>/TASKS.md → "a:b"。认不出返回空串。"""
    rel = os.path.relpath(os.path.dirname(path), os.path.join(root, "domains"))
    parts = [p for p in rel.split(os.sep) if p and p != "."]
    return ":".join(parts[:2]) if parts else ""


def _ledger_path(data_home):
    return os.path.join(str(data_home), "task-ledger.json") if data_home else ""


def _ledger_snapshot(data_home):
    """只提取 task 行并稳定序列化。receipt、memory candidate 等变化不属于任务详情更新。"""
    path = _ledger_path(data_home)
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        return None
    rows = value.get("tasks") if isinstance(value, dict) else {}
    if not isinstance(rows, dict):
        return {}
    return {str(key): json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            for key, row in rows.items() if isinstance(row, dict)}


def _ledger_domain(key):
    """账本键是 <domain>:<task id>；domain 自身可含一层冒号。"""
    domain, sep, _tid = str(key or "").rpartition(":")
    return domain if sep else ""


def _loop(root, bus, stop, data_home=None):
    # 第一轮只建基线:进程刚起来,没人在等这一条,全推一遍反而让每一页白取一次。
    prev, prev_reg = _scan(root), _scan_reg(root)
    ledger_path = _ledger_path(data_home)
    prev_ledger_fp = _fp(ledger_path) if ledger_path else None
    prev_ledger = _ledger_snapshot(data_home)
    if prev_ledger is None:
        prev_ledger = {}
    while not stop.is_set():
        stop.wait(SCAN_SEC)
        if stop.is_set():
            return
        try:
            cur, cur_reg = _scan(root), _scan_reg(root)
            cur_ledger_fp = _fp(ledger_path) if ledger_path else None
        except Exception:
            continue          # 扫一次失败不结束看护:下一秒再来
        changed_domains = set()
        for path in set(prev) | set(cur):
            if prev.get(path) == cur.get(path):
                continue
            did = _domain_of(root, path)
            if did:
                changed_domains.add(did)
        prev = cur
        if cur_ledger_fp != prev_ledger_fp:
            cur_ledger = _ledger_snapshot(data_home)
            if cur_ledger is not None:  # 暂态读取失败不前移基线，下一轮重试
                changed = {key for key in set(prev_ledger) | set(cur_ledger)
                           if prev_ledger.get(key) != cur_ledger.get(key)}
                changed_domains.update({_ledger_domain(key) for key in changed} - {""})
                prev_ledger, prev_ledger_fp = cur_ledger, cur_ledger_fp
        # 同一 desk-task 操作常同时更新 TASKS.md 与 ledger；一轮内每个 domain 只广播一次。
        for did in sorted(changed_domains):
            bus.publish({"type": "task.updated", "properties": {"domain": did}})
        if cur_reg != prev_reg:
            bus.publish({"type": "apps.updated", "properties": {"rev": _rev_of(cur_reg)}})
            prev_reg = cur_reg


def start(root, bus, data_home=None):
    """起看护线程。重复调只起一个(ui_server 重载时会再调一次)。"""
    global _thread, _stop
    if _thread is not None and _thread.is_alive():
        return _thread
    _stop = threading.Event()
    _thread = threading.Thread(target=_loop, args=(root, bus, _stop, data_home),
                               name="task-watch", daemon=True)
    _thread.start()
    return _thread


def stop():
    global _thread, _stop
    if _stop is not None:
        _stop.set()
    _thread = None
