"""s0 journal 流水:d-<日期> 日段格式的读写、折叠与积压识别。格式单一源。

journal 是记忆管道的入口层(免闸、可丢弃):引擎适配器的隔离 recap 自动或按请求写入；
后端维护流程可直接追加。dreaming 只负责下游蒸馏,不再离线扫描会话。文件 = 头部两索引 + --- + 日段。
"""
import os
import re
import threading
from datetime import date, datetime, timedelta

from . import layout

STUB = "(compacted — 全文已折叠;见关键信息索引,或已蒸馏进记忆)"
TPL = ("# journal — {ws}\n\n"
       "> 当前工作流水(s0,免闸)。append 追加,定期蒸馏进记忆 + compact 老段保持有界。见 journal skill。\n\n"
       "## 日期索引\n\n## 关键信息索引\n\n---\n\n")
_DAY_RE = re.compile(r"## d-(\d{4}-\d{2}-\d{2})")
_APPEND_LOCK = threading.Lock()


def path_of(root, ws):
    s0 = layout.workspace_layout(root, ws)["journal"]
    return os.path.join(s0, "journal.md")


def append(root, ws, note, key=None, dedupe=None):
    """顶插一条流水到当天日段。

    ``dedupe`` 是调用方提供的稳定幂等键；同一个键已经落盘时直接返回，不再追加。
    标记跟正文放在同一次写入里，避免另建 receipt 后出现「receipt 有了、正文没写」的裂缝。
    """
    path = path_of(root, ws)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    marker = f"<!-- journal-dedupe:{dedupe} -->" if dedupe else ""
    with _APPEND_LOCK:
        try:
            with open(path, encoding="utf-8") as f:
                text = f.read()
        except FileNotFoundError:
            text = TPL.format(ws=ws)
        if marker and marker in text:
            return path
        today = date.today().isoformat()
        suffix = f" {marker}" if marker else ""
        entry = f"{datetime.now().strftime('%H:%M')} {note.strip()}{suffix}"
        day_hdr = f"## d-{today}\n"
        if day_hdr in text:
            text = text.replace(day_hdr, f"{day_hdr}{entry}\n", 1)
        else:
            text = text.replace("## 日期索引\n", f"## 日期索引\n- {today} → #d-{today}\n", 1)
            text = text.replace("\n---\n\n", f"\n---\n\n{day_hdr}{entry}\n\n", 1)
        if key:
            text = text.replace("## 关键信息索引\n",
                                f"## 关键信息索引\n- {key.strip()} → #d-{today}\n", 1)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    return path


def compact(root, ws, keep_days=7):
    """把超过窗口的老日子正文折叠成一行 stub(机械,不蒸馏;蒸馏先于折叠,折叠丢全文)。
    返回折叠的日段数;无 journal 时返回 None。"""
    path = path_of(root, ws)
    if not os.path.exists(path):
        return None
    text = open(path, encoding="utf-8").read()
    if "\n---\n" not in text:
        raise ValueError("journal.md 缺 --- 分隔(索引 / 正文)")
    head, body = text.split("\n---\n", 1)
    cutoff = date.today() - timedelta(days=keep_days)
    out, n = [], 0
    for sec in re.split(r"(?m)(?=^## d-)", body):
        m = _DAY_RE.match(sec)
        if not m:
            out.append(sec)
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            out.append(sec)
            continue
        rest = sec.split("\n", 1)
        already = rest[1].strip() if len(rest) > 1 else ""
        if d < cutoff and STUB not in already:
            out.append(f"## d-{m.group(1)}\n{STUB}\n\n")
            n += 1
        else:
            out.append(sec)
    with open(path, "w", encoding="utf-8") as f:
        f.write(head + "\n---\n" + "".join(out))
    return n


def stale_days(root, ws, keep_days=7, stub_mark="compacted"):
    """该工作区 journal 里超窗且未折叠的老日子(蒸馏候选)。返回排序后的日期串列表。"""
    path = path_of(root, ws)
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            body = f.read()
    except Exception:
        return []
    cutoff = date.today() - timedelta(days=keep_days)
    days = []
    for sec in re.split(r"(?m)(?=^## d-)", body):
        m = _DAY_RE.match(sec)
        if not m:
            continue
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
        except ValueError:
            continue
        rest = sec.split("\n", 1)[1] if "\n" in sec else ""
        if d < cutoff and rest.strip() and stub_mark not in rest:
            days.append(m.group(1))
    return sorted(days)


def distill_candidates(root):
    """扫全部工作区的蒸馏候选:{ws: [超窗未折叠日期,...]}。"""
    out = {}
    try:
        wss = [w for w in os.listdir(root) if not w.startswith((".", "_")) or w == "_desktop"]
    except Exception:
        return out
    for ws in wss:
        days = stale_days(root, ws)
        if days:
            out[ws] = days
    return out
