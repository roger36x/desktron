"""记忆引擎调度：定期刷新项目记忆，并产出两轴人审报告。

会话自动采集不在本调度器里做:Desk 引擎适配器在回合收尾后用 recap 直接写 journal。
旧版离线扫描会话文件并写 `[capture]` 指针的链路已于 2026-08-28 退役。

自动边界:机械分析与免闸区内蒸馏自动;SOT 合入与淘汰一律人审(记忆轴报 SOT 合入提名,
不给 gc --yes 指令;知识轴照旧)。有候选且候选集有变才通知(免闸 journal,指纹去重),
无候选整轮静默。可观测契约:tick 成功刷 last-run 清 last-error;失败写 last-error(带栈)
且按 RETRY 短间隔重试;首轮延后 START_GRACE 避宿主启动期竞态;status() 一条命令查死活。

env(MEMORY_ENGINE_* 优先,DESKTRON_* 兼容):DREAM=off 关;DREAM_INTERVAL 秒(默认 1800)。
"""
import hashlib
import json
import os
import threading
import time
from datetime import datetime

from . import journal, layout, project_memory
from .project_memory import env_get

KEEP_DAYS = 7        # journal 蒸馏窗口(与 journal.stale_days 默认一致)
START_GRACE = 90     # 首轮延后(秒):宿主启动期可能在做布局迁移/供给,即刻 tick 会竞态失败
RETRY = 300          # tick 失败后的重试间隔(秒)


def _curator_call(argv):
    """同进程调 curator(并仓后不再 spawn),返回 stdout;失败返回 None。"""
    import contextlib
    import io

    try:
        from curator.cli import main as curator_main
    except Exception:
        return None
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()):
            rc = curator_main(argv)
    except Exception:
        return None
    return buf.getvalue() if not rc else None


def _dream_dir(mem_root):
    d = layout.state_dir(mem_root, "dream")
    os.makedirs(d, exist_ok=True)
    return d


def gc_scan(root):
    """curator gc dry-run --json → dict;实例不存在/命令失败 → None(报告里如实标)。"""
    if not os.path.isfile(os.path.join(root, "ontology.yaml")):
        return None
    out = _curator_call(["--root", root, "gc", "--dry-run", "--json"])
    if not out or not out.strip():
        return None
    try:
        return json.loads(out.strip().splitlines()[-1])
    except ValueError:
        return None


def _fmt_gc_section(label, root_rel, scan, sot_nominate=False):
    lines = [f"## {label}({root_rel})"]
    if scan is None:
        lines.append("- 扫描不可用(实例缺失或 curator 失败),无数据")
        return lines, 0
    n = len(scan.get("promote") or []) + len(scan.get("evict") or []) + len(scan.get("review_due") or [])
    lines.append(f"- 免闸区 {scan.get('total', 0)} 项,保留 {scan.get('keep', 0)}")
    if scan.get("promote"):
        if sot_nominate:   # 记忆轴:promote=人审把成熟笔记结论合入 SOT 文档(编辑动作),不再 gc 挪文件
            lines.append(f"- SOT 合入提名({len(scan['promote'])}),审后人工编辑 <ws>/sot/ 对应主题文档:")
        else:
            lines.append(f"- 晋升候选 → {scan.get('promote_to')}({len(scan['promote'])}),审后执行:`curator --root {root_rel} gc --yes`")
        lines += [f"  - {r.get('id') or r.get('rel')}(质量 {r.get('quality')},{r.get('reason')},topic={r.get('topic') or '-'})"
                  for r in scan["promote"]]
    if scan.get("evict"):
        lines.append(f"- 淘汰候选({len(scan['evict'])},删除不可逆,须人审):")
        lines += [f"  - {r.get('id') or r.get('rel')}(质量 {r.get('quality')},{r.get('reason')})" for r in scan["evict"]]
    if scan.get("review_due"):
        lines.append(f"- 复验到期({len(scan['review_due'])}):" + "、".join(x.get("id") or x.get("rel") for x in scan["review_due"]))
    if not n:
        lines.append("- 无候选")
    return lines, n


def tick(host, verbose=False):
    """跑一轮：刷新项目记忆 → 两轴 GC 分析 → 报告/通知。返回候选总数。"""
    mem_scan = gc_scan(host.mem_root)
    kn_scan = gc_scan(host.knowledge_root)
    engine_line = project_memory.refresh(host)

    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [f"# dreaming 报告 {stamp}", "",
             "> 记忆引擎维护每个工作区的一份当前项目模型；SOT 合入与淘汰仍须人审。",
             "",
             "## 自动采集(recap)",
             "- 回合收尾后由引擎适配器直接写 journal；记忆引擎读取流水并整篇重写项目模型",
             f"- {engine_line}"]
    sec, n1 = _fmt_gc_section("记忆轴", host.mem_root, mem_scan, sot_nominate=True)
    lines += [""] + sec
    sec, n2 = _fmt_gc_section("知识轴", host.knowledge_root, kn_scan)
    lines += [""] + sec
    state = project_memory.load_state(project_memory.state_path(host.mem_root))
    pending = project_memory.candidates(host, state=state)
    lines += ["", "## 项目记忆"]
    if pending:
        lines.append("- 待刷新：" + "、".join(ws for ws, _ in pending))
    else:
        lines.append("- 全部工作区已是最新")
    # 项目记忆待刷新由引擎自行处理，不是人审候选；若把它计入通知，通知本身写入
    # _desktop journal 又会制造一次项目记忆变化，形成自触发循环。
    total = n1 + n2
    dd = _dream_dir(host.mem_root)
    with open(os.path.join(dd, "latest.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    # 候选集指纹:内容不变不重复通知(候选滞留是常态,重复刷会淹掉 journal)
    fp_src = json.dumps({
        "p": sorted(r.get("id") or r.get("rel") for r in ((mem_scan or {}).get("promote") or []) + ((kn_scan or {}).get("promote") or [])),
        "e": sorted(r.get("id") or r.get("rel") for r in ((mem_scan or {}).get("evict") or []) + ((kn_scan or {}).get("evict") or [])),
    }, ensure_ascii=False, sort_keys=True)
    fp = hashlib.sha256(fp_src.encode()).hexdigest()[:16]
    fp_path = os.path.join(dd, "notified-fp")
    last_fp = ""
    if os.path.exists(fp_path):
        try:
            with open(fp_path, encoding="utf-8") as f:
                last_fp = f.read().strip()
        except Exception:
            pass
    with open(os.path.join(dd, "last-run"), "w", encoding="utf-8") as f:
        f.write(f"{int(time.time())} candidates={total} fp={fp}\n")
    err_p = os.path.join(dd, "last-error")
    if os.path.exists(err_p):
        try:
            os.remove(err_p)   # 本轮成功,清旧失败痕:last-error 只表示「最近一轮失败」
        except OSError:
            pass

    if total and fp != last_fp:   # 有候选且集合有变才通知;经免闸 journal
        note = (f"dreaming:{total} 项候选待审(SOT 提名 {len((mem_scan or {}).get('promote') or [])}"
                f"、知识晋升 {len((kn_scan or {}).get('promote') or [])}、淘汰 {len((mem_scan or {}).get('evict') or [])}"
                f"+{len((kn_scan or {}).get('evict') or [])}),"
                f"清单:{os.path.join(dd, 'latest.md')}")
        if _curator_call(["--root", host.mem_root, "journal", "append",
                        "--topic", "_desktop", "--title", "dreaming", "--text", note]) is not None:
            try:
                with open(fp_path, "w", encoding="utf-8") as f:
                    f.write(fp + "\n")
            except OSError:
                pass
    if verbose:
        print("\n".join(lines))
        print(f"\n候选合计 {total};报告 {os.path.join(dd, 'latest.md')}")
    return total


def record_error(host, exc):
    """tick 失败落痕:.curator/dream/last-error(时间 + 栈)。"""
    import traceback
    msg = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {type(exc).__name__}: {exc}\n{traceback.format_exc()}"
    try:
        with open(os.path.join(_dream_dir(host.mem_root), "last-error"), "w", encoding="utf-8") as f:
            f.write(msg)
    except Exception:
        host.log("dreaming", f"tick 失败(无处落痕):{msg}")


def interval():
    try:
        return max(300, int(env_get("DREAM_INTERVAL", "1800")))
    except ValueError:
        return 1800


def enabled():
    return env_get("DREAM").lower() not in ("off", "0", "false")


def start_background(host, guard=None):
    """宿主调用:daemon 线程按间隔 tick。首轮延后 START_GRACE 秒(避开宿主启动期竞态),
    之后每轮成功等正常间隔、失败落 last-error 并按 RETRY 短间隔重试。"""
    if not enabled():
        return None
    def loop():
        time.sleep(START_GRACE)
        while True:
            try:
                if guard:
                    with guard() as admitted:
                        if admitted:
                            tick(host)
                else:
                    tick(host)
                wait = interval()
            except Exception as e:
                record_error(host, e)
                wait = min(RETRY, interval())
            time.sleep(wait)
    t = threading.Thread(target=loop, name="memory-dreaming", daemon=True)
    t.start()
    host.log("dreaming", f"调度器已启动(间隔 {interval()}s,首轮 {START_GRACE}s 后)")
    return t


def status_lines(host):
    """「调度器在不在工作」:上次 tick 时间/候选数/是否超期、最近失败、报告位置。"""
    dd = layout.state_dir(host.mem_root, "dream")
    out = [f"开关:{'on' if enabled() else 'off(MEMORY_ENGINE_DREAM/DESKTRON_DREAM)'};间隔 {interval()}s"]
    lr = os.path.join(dd, "last-run")
    if os.path.isfile(lr):
        with open(lr, encoding="utf-8") as f:
            parts = f.read().split()
        ts = int(parts[0])
        age = int(time.time()) - ts
        stale = age > interval() * 2
        out.append(f"上次 tick:{datetime.fromtimestamp(ts).strftime('%Y-%m-%d %H:%M')}({age // 60} 分钟前,"
                   f"{'超期,线程可能没在跑' if stale else '正常'});{' '.join(parts[1:])}")
    else:
        out.append("上次 tick:无记录(从未成功跑过)")
    le = os.path.join(dd, "last-error")
    if os.path.isfile(le):
        with open(le, encoding="utf-8") as f:
            head = f.read().strip().splitlines()
        out.append(f"最近失败:{head[0] if head else '(空)'}(全文 {le})")
    else:
        out.append("最近失败:无")
    rep = os.path.join(dd, "latest.md")
    out.append(f"报告:{rep if os.path.isfile(rep) else '(尚无)'}")
    return out
