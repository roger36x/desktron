"""常驻引擎进程的内存预算(跨引擎共用一份)。

此前每个引擎各管各的池,闸都写在「同时留几个会话」上:claude 是 _PERSIST_MAX,
codex 家族是 _SESSION_MAX,默认都是 6。问题在于单位成本差一个数量级——2026-08-23
实测:一条常驻的 claude 进程 450~510MB,且随对话继续增长;一份 codex app-server 连同
它的三个子进程(JS 壳 5MB、原生二进制 30~38MB、code-mode helper 11MB、node_repl 6MB)
约 60MB。同样写 6,前者的上限是 3GB,后者是 0.36GB。而两个池互不相识,没有任何一处
看总量:claude 池满了只挤 claude,哪怕 codex 那边正闲着五个更该收的。

用户当天实见 Desktron 涨到 5.73GB。按上面这本账,这个数不需要任何泄漏就到得了;
他还观察到「会回落一点」,那正是闲置三十分钟后被各自的 reaper 收掉。

所以把闸从「按个数」改成「按内存」:两个池都到这里登记,准入之前先按真实 RSS 结一次账,
超了就跨引擎一起挑「最久没用过、且不在跑回合」的那个收掉。个数上限保留为第二道闸——
极小的进程无限增多同样不好,那一层各池自己管。

RSS 按进程树算,不按单个进程:codex 那一份里壳只有 5MB,占地方的全是它的子进程。
"""
import collections
import os
import subprocess
import threading
import time

#: 预算。取物理内存的一小份,并卡上下限——机器大不等于该让引擎吃掉那么多,
#: 机器小也得留出至少能跑两三条会话的余地。可用环境变量覆盖(单位 MB)。
def _default_budget():
    try:
        ram = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, ValueError, OSError):
        ram = 8 << 30
    return int(min(max(ram * 0.06, 1536 << 20), 4 << 30))


def budget_bytes():
    raw = os.environ.get("DESKTRON_RESIDENT_BUDGET_MB", "").strip()
    if raw:
        try:
            return max(256 << 20, int(raw) << 20)
        except ValueError:
            pass
    return _default_budget()


_pools = {}          # 名字 -> (列出常驻项, 收掉某一项)
_lock = threading.Lock()
_last = {"at": 0.0, "rss": {}}
_RSS_TTL = 3.0       # RSS 采样的保鲜期:一次 ps 要几十毫秒,连着结账不必每次都采


def add_pool(name, list_fn, drop_fn):
    """登记一个常驻池。

    list_fn() → [(sid, pid, busy, at)];pid 是这一份的根进程,树上的都算它的。
    drop_fn(sid) → 收掉那一份。两者都不该抛。
    """
    with _lock:
        _pools[name] = (list_fn, drop_fn)


def _tree_rss():
    """一次 ps 拿到全表,返回 pid → 这棵树的 RSS 字节数。"""
    now = time.time()
    if now - _last["at"] < _RSS_TTL:
        return _last["rss"]
    try:
        r = subprocess.run(["ps", "-axo", "pid=,ppid=,rss="],
                           capture_output=True, text=True, timeout=10)
    except Exception:
        return _last["rss"]
    own = {}
    kids = collections.defaultdict(list)
    for ln in r.stdout.splitlines():
        f = ln.split()
        if len(f) != 3:
            continue
        try:
            pid, ppid, rss = int(f[0]), int(f[1]), int(f[2])
        except ValueError:
            continue
        own[pid] = rss << 10
        kids[ppid].append(pid)
    out = {}

    def total(pid, seen):
        if pid in out:
            return out[pid]
        if pid in seen:          # ps 快照里父子关系可能自成环(进程刚换过父),不递归死
            return 0
        seen.add(pid)
        s = own.get(pid, 0) + sum(total(k, seen) for k in kids.get(pid, ()))
        out[pid] = s
        return s

    for pid in list(own):
        total(pid, set())
    _last["at"], _last["rss"] = now, out
    return out


def items():
    """所有池里的常驻项,带上各自的 RSS。"""
    with _lock:
        pools = dict(_pools)
    rss = _tree_rss()
    rows = []
    for name, (list_fn, _drop) in pools.items():
        try:
            got = list_fn() or []
        except Exception:
            continue
        for sid, pid, busy, at in got:
            rows.append({"pool": name, "sid": sid, "pid": pid, "busy": bool(busy),
                         "at": float(at or 0), "rss": rss.get(int(pid or 0), 0)})
    return rows


def used_bytes():
    return sum(x["rss"] for x in items())


def tree_rss_bytes(pid, *, fresh=False):
    """Best-effort RSS for one process tree; fresh bypasses the short shared cache."""
    try:
        if fresh:
            _last["at"] = 0.0
        return int(_tree_rss().get(int(pid), 0) or 0)
    except (TypeError, ValueError):
        return 0


def make_room(keep_pool="", keep_sid="", budget=None):
    """结账:超预算就跨引擎收,直到回到预算之下。返回收掉的 (池, sid) 列表。

    正在跑回合的一律不动——收一个在跑的等于把用户这一回合的活弄丢,而回收本身只是
    为了省资源。全都在跑就让它超一会儿,那比打断谁都强(与两个池原来的取舍一致)。
    """
    cap = budget_bytes() if budget is None else budget
    gone = []
    tried = set()                # 已经收过的不再选:drop 没能真把它摘掉时(池子实现有问题、
                                 # 或那一项刚被别的线程换掉),不带这一层就会对着同一个目标
                                 # 空转到上限为止(2026-08-23 拿盘上真进程演算时撞到)。
    for _ in range(32):          # 收到预算之下即止;给个上限,免得判据出错时空转
        rows = items()
        if sum(x["rss"] for x in rows) <= cap:
            break
        idle = [x for x in rows
                if not x["busy"] and (x["pool"], x["sid"]) not in tried
                and not (x["pool"] == keep_pool and x["sid"] == keep_sid)]
        if not idle:
            break
        victim = min(idle, key=lambda x: x["at"])
        tried.add((victim["pool"], victim["sid"]))
        with _lock:
            drop = (_pools.get(victim["pool"]) or (None, None))[1]
        if not drop:
            break
        try:
            dropped = drop(victim["sid"])
        except Exception:
            break
        if dropped is False:
            continue
        gone.append((victim["pool"], victim["sid"]))
        _last["at"] = 0.0        # 收掉一个就重新采,不然下一轮还按旧数结账
    return gone


#: 周期结账的间隔。准入那一道挡不住这次实见的情形——三个会话就地长大,期间一次新准入
#: 都没有,两池各自的 reaper 又只看闲置时长不看内存,于是没有任何一处会去收。
_SWEEP = 45
_sweeping = {"on": False, "last": []}


def sweep_once():
    """结一次账,记下收了谁并报进日志。循环体单独拎出来,是为了测得到。"""
    try:
        gone = make_room()
    except Exception:
        return []
    if gone:
        _sweeping["last"] = gone
        try:
            import deskcfg
            deskcfg.log("resident", "超预算,收掉 " + "、".join(
                "%s/%s" % (pool, sid[:16]) for pool, sid in gone))
        except Exception:
            pass
    return gone


def _sweep_loop():
    while True:
        time.sleep(_SWEEP)
        sweep_once()


def start_sweep():
    """起周期结账。ui_server 调一次;重复调只起一条。"""
    if _sweeping["on"]:
        return
    _sweeping["on"] = True
    threading.Thread(target=_sweep_loop, daemon=True, name="resident-sweep").start()


def snapshot():
    """给 /workbench/agents/status 与排障用的一份现状。"""
    rows = sorted(items(), key=lambda x: -x["rss"])
    cap = budget_bytes()
    used = sum(x["rss"] for x in rows)
    return {"budget_mb": round(cap / (1 << 20)), "used_mb": round(used / (1 << 20)),
            "over": used > cap, "sweeping": _sweeping["on"],
            "last_evicted": ["%s/%s" % (p, x) for p, x in _sweeping["last"]],
            "items": [{"pool": x["pool"], "sid": x["sid"], "busy": x["busy"],
                       "rss_mb": round(x["rss"] / (1 << 20), 1),
                       "idle_s": round(max(0.0, time.time() - x["at"]))} for x in rows]}
