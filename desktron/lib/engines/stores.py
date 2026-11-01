"""会话落盘后端两族(第 2 步:list_by_cwd 内联自 _dir_sessions;list_all 委托标准 helper)。

各引擎的持久化只有两种形态:
- JsonlDirStore:编码 cwd 为目录名,目录下一个 jsonl 一会话。claude 与 cursor 共用(variant 区分)。
- RolloutGlobStore:rollout-*-<uuid>.jsonl 递归 glob,首行 session_meta 读 cwd。codex 使用。

低层解析 helper(_claude_jsonl_title / _claude_named / _claude_proj_enc / _cursor_* / _codex_sessions)留在
term.py,本模块经 lazy import 调用,避免 import 期循环。list_by_cwd 的实现搬自原 term._dir_sessions 的对应分支。
"""
import glob
import json
import os
import re
import threading
import time

from engines.base import SessionStore


class JsonlDirStore(SessionStore):
    """编码 cwd → 目录,目录下每个 jsonl 一会话。claude / cursor 共用,variant 区分解析器/编码。"""
    def __init__(self, variant):
        self.variant = variant   # "claude" | "cursor-agent"

    def _by_id(self, sid):
        import term
        return (term._claude_session_by_id if self.variant == "claude"
                else term._cursor_session_by_id)(sid)

    def list_all(self):
        import term
        return term._claude_sessions() if self.variant == "claude" else term._cursor_sessions()

    def list_by_cwd(self, wd):
        import term
        out = []
        if self.variant == "claude":
            d = os.path.expanduser(f"~/.claude/projects/{term._claude_proj_enc(wd)}")
            if os.path.isdir(d):
                for fn in os.listdir(d):
                    if not fn.endswith(".jsonl"):
                        continue
                    sid = fn[:-6]
                    title, mt = term._claude_jsonl_title(os.path.join(d, fn))
                    reg = term._claude_named().get(sid) or ""
                    if term._claude_auto_named(reg, wd):
                        reg = ""      # 引擎自动名(workspace-9c 这种)不算标题,退回内容派生
                    out.append({"id": sid, "title": (reg or title or sid[:8])[:80],
                                "time": int(mt * 1000)})
            out += self._claude_relocated(wd, {x["id"] for x in out})
        else:
            base = term._cursor_transcripts_dir(wd)
            if os.path.isdir(base):
                for sid in os.listdir(base):
                    path = term._cursor_path(sid, wd)
                    if not os.path.isfile(path):
                        continue
                    title, mt = term._cursor_jsonl_title(path)
                    out.append({"id": sid, "title": (title or sid[:8])[:80], "time": int(mt * 1000)})
        out.sort(key=lambda s: -s["time"])
        return out

    @staticmethod
    def _claude_relocated(wd, seen):
        """本工作目录的会话里,记录落在别处目录的那些(按 wd 编码扫不到,要补进来)。

        记录目录名在会话创建时由 cwd 定死,--resume 之后不迁移:一段在工作目录里跑着的会话,
        它的记录可能一直追加在出生目录下。这里不反向扫全机记录去猜归属——只认 meta 里已登记
        归属本工作目录的会话 id,再按 id 精确定位它的文件;定不到就不列。

        记录仍在某个工作目录编码出的目录下的会话不补:那一侧的 list_by_cwd 已经列了它,
        补进来只会让同一段会话在两个工作目录的列表里各出现一次。"""
        import term
        dom = term._domain_for_cwd(wd)
        if not dom:
            return []      # 外部仓等无归属目录:meta 的 domain 无从对应,按编码列到什么算什么
        out = []
        for sid, m in term._term_meta_read().items():
            if sid in seen or m.get("engine") != "claude" or m.get("task"):
                continue
            if (m.get("domain") or "") != dom:
                continue
            if term._transcript_cwd("claude", sid):
                continue
            row = term._claude_session_by_id(sid)   # 编码目录里都没有 → 走它的按 id 兜底
            if row:
                out.append(row)
        return out

    def title(self, sid):
        s = self._by_id(sid)
        return s.get("title") if s else None

    def cwd_of(self, sid):
        s = self._by_id(sid)
        return s.get("cwd") if s else None   # claude 记录无 cwd → None;cursor 有

    def owns(self, sid):
        return self._by_id(sid) is not None

    def delete(self, sid):
        import term
        if self.variant == "cursor-agent":
            term._cursor_del_transcript(sid)
            return
        for wd in term._workspace_dirs():
            try:
                os.remove(term._claude_path(sid, wd))
            except Exception:
                pass
        # 编码目录只覆盖当前的工作目录集合,而记录的目录名由会话创建时的 cwd 定死、--resume
        # 之后不迁移:一段会话的记录可能留在出生目录下,上面那圈一个也删不到。兜底按 id 精确
        # 命中——id 过 uuid 形状校验、文件名与 id 全等、文件必须直属 ~/.claude/projects 的某个
        # 子目录(见 _claude_transcript_hits)。删除不可逆,宁可删不到,也不做任何模糊匹配。
        for path in term._claude_transcript_hits(sid):
            try:
                os.remove(path)
            except OSError:
                pass


class RolloutGlobStore(SessionStore):
    """rollout-*-<uuid>.jsonl 递归 glob,首行 session_meta 取 cwd / id。codex 使用。"""
    def __init__(self, engine, rollout_dir):
        self.engine = engine            # "codex"
        self._rollout_dir = rollout_dir

    def base_dir(self):
        """Return the rollout directory used by the Desk-launched engine.

        Codex normally shares global sessions through a symlink.
        Older installations can contain a real directory
        there instead (for example when the global target did not exist during
        provisioning).  In that case the engine writes into the managed directory,
        so listing the hard-coded global path makes every completed chat vanish
        from the sidebar.
        """
        if self.engine == "codex":
            try:
                from deskcfg import DATA_HOME
                parts = ("codex-home", "sessions")
                managed = os.path.join(os.path.dirname(DATA_HOME), *parts)
                if os.path.isdir(managed):
                    return managed
            except Exception:
                pass
        return os.path.expanduser(self._rollout_dir)

    def session_dirs(self):
        """实际写入目录优先，同时保留旧全局记录；软链不重复扫描。"""
        paths = {}
        for path in (self.base_dir(), os.path.expanduser(self._rollout_dir)):
            paths.setdefault(os.path.realpath(path), path)
        return tuple(paths.values())

    def list_all(self):
        import term
        return term._codex_sessions(self.engine)   # 标准 helper(带 5s 缓存)

    def list_by_cwd(self, wd):
        import term
        files = [p for base in self.session_dirs()
                 for p in glob.glob(base + "/**/rollout-*.jsonl", recursive=True)]
        out = []
        seen = set()
        for path in files:
            sid, title, mt = "", "", 0
            try:
                with open(path, encoding="utf-8") as f:
                    meta = json.loads(f.readline())
                    if meta.get("type") != "session_meta":
                        continue
                    pl = meta.get("payload") or {}
                    if (not pl.get("cwd") or os.path.realpath(pl["cwd"]) != os.path.realpath(wd)
                            or not pl.get("id") or pl["id"] in seen):
                        continue
                    sid = pl["id"]
                    title = term._codex_rollout_title(path, self.engine)
                mt = int(os.path.getmtime(path) * 1000)
            except Exception:
                continue
            if sid:
                seen.add(sid)
                out.append({"id": sid, "title": (title or sid[:8])[:80], "time": mt})
        out.sort(key=lambda s: -s["time"])
        return out

    def _find(self, sid):
        return next((x for x in self.list_all() if x["id"] == sid), None)

    def title(self, sid):
        s = self._find(sid)
        return s.get("title") if s else None

    def cwd_of(self, sid):
        s = self._find(sid)
        return s.get("cwd") if s else None

    def owns(self, sid):
        """按文件名精确定位并校验首行，不为归属判断构建完整会话列表。"""
        sid = str(sid or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F-]{36}", sid):
            return False
        for base in self.session_dirs():
            for path in glob.glob(base + f"/**/rollout-*-{sid}.jsonl", recursive=True):
                try:
                    with open(path, encoding="utf-8") as f:
                        meta = json.loads(f.readline())
                    payload = meta.get("payload") or {}
                    if meta.get("type") == "session_meta" and str(payload.get("id") or "") == sid:
                        return True
                except (OSError, ValueError, AttributeError):
                    continue
        return False

    def delete(self, sid):
        import term
        term._codex_del_rollout(sid, self.engine)


def pi_sessions_dir_name(cwd):
    """Pi 的 cwd 目录编码；点号不替换，与 Claude 的编码规则不同。"""
    value = os.path.abspath(cwd or "").lstrip("/\\")
    for char in "/\\:":
        value = value.replace(char, "-")
    return f"--{value}--"


def _pi_sessions_root():
    return os.path.expanduser(os.environ.get("PI_CODING_AGENT_SESSION_DIR")
                              or "~/.pi/agent/sessions")


def pi_session_path(sid, cwd=""):
    """按完整会话 id 精确定位 Pi JSONL；不接受通配或模糊 id。"""
    sid = str(sid or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{5,127}", sid):
        return ""
    root = _pi_sessions_root()
    # 默认 ~/.pi/agent/sessions 下按 cwd 分目录；一旦设置
    # PI_CODING_AGENT_SESSION_DIR，Pi 0.84.x 会把它当作最终目录，JSONL 直接落在其下。
    # 两种都扫，兼容用户迁移过的混合目录。
    patterns = [os.path.join(root, "*_" + sid + ".jsonl")]
    if cwd:
        patterns.append(os.path.join(root, pi_sessions_dir_name(cwd), "*_" + sid + ".jsonl"))
    else:
        patterns.append(os.path.join(root, "*", "*_" + sid + ".jsonl"))
    hits = []
    for path in dict.fromkeys(path for pattern in patterns for path in glob.glob(pattern)):
        try:
            with open(path, encoding="utf-8") as f:
                header = json.loads(f.readline())
            if header.get("type") == "session" and str(header.get("id") or "") == sid:
                hits.append(path)
        except Exception:
            continue
    if not hits:
        return ""
    return max(hits, key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0)


class PiSessionStore(SessionStore):
    """Pi v3 会话：默认按 cwd 分目录，自定义 session dir 时直接落在该目录。"""

    _TTL = 5

    def __init__(self):
        self._lock = threading.Lock()
        self._list_cache = (0.0, [])
        self._row_cache = {}

    @staticmethod
    def _content_text(content):
        if isinstance(content, str):
            return content
        return "".join(str(block.get("text") or "") for block in (content or [])
                       if isinstance(block, dict) and block.get("type") == "text")

    def _row(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        key = (st.st_mtime_ns, st.st_size)
        with self._lock:
            cached = self._row_cache.get(path)
            if cached and cached[0] == key:
                return dict(cached[1])
        header, first, named = {}, "", ""
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                header = json.loads(f.readline())
                if header.get("type") != "session" or not header.get("id"):
                    return None
                # 标题的内容回落只需首条发言；限制读取量，侧栏不能为长记录付整份扫描。
                for _ in range(800):
                    line = f.readline()
                    if not line or f.tell() > 2_000_000:
                        break
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    msg = obj.get("message") or {}
                    if obj.get("type") == "message" and msg.get("role") == "user":
                        first = self._content_text(msg.get("content")).strip().splitlines()[0][:80]
                        if first:
                            break
            # /session-name 追加在尾部；只扫尾窗取最后一次，避免为改名读完整条历史。
            with open(path, "rb") as f:
                start = max(0, st.st_size - 512_000)
                f.seek(start)
                if start:
                    f.readline()
                for raw in f:
                    if b'"session_info"' not in raw:
                        continue
                    try:
                        obj = json.loads(raw)
                    except ValueError:
                        continue
                    if obj.get("type") == "session_info":
                        named = str(obj.get("name") or "").strip()[:80]
        except (OSError, ValueError):
            return None
        sid = str(header.get("id"))
        row = {"id": sid, "title": named or first or sid[:8],
               "time": int(st.st_mtime * 1000), "cwd": str(header.get("cwd") or ""),
               "_path": path}
        with self._lock:
            self._row_cache[path] = (key, dict(row))
        return row

    def _rows(self):
        now = time.time()
        with self._lock:
            at, cached = self._list_cache
            if now - at < self._TTL:
                return [dict(x) for x in cached]
        root = _pi_sessions_root()
        files = list(dict.fromkeys(
            glob.glob(os.path.join(root, "*.jsonl"))
            + glob.glob(os.path.join(root, "*", "*.jsonl"))))
        files.sort(key=lambda p: -(os.path.getmtime(p) if os.path.exists(p) else 0))
        rows = [row for row in (self._row(path) for path in files[:500]) if row]
        rows.sort(key=lambda row: -row["time"])
        with self._lock:
            self._list_cache = (now, [dict(x) for x in rows])
        return rows

    def list_all(self):
        return [{k: v for k, v in row.items() if k != "_path"} for row in self._rows()]

    def list_by_cwd(self, wd):
        want = os.path.realpath(os.path.abspath(wd or ""))
        return [{k: v for k, v in row.items() if k != "_path"}
                for row in self._rows()
                if os.path.realpath(os.path.abspath(row.get("cwd") or "")) == want]

    def _find(self, sid):
        hit = next((row for row in self._rows() if row.get("id") == sid), None)
        if hit:
            return hit
        path = pi_session_path(sid)
        return self._row(path) if path else None

    def title(self, sid):
        row = self._find(sid)
        return row.get("title") if row else None

    def cwd_of(self, sid):
        row = self._find(sid)
        return row.get("cwd") if row else None

    def owns(self, sid):
        return bool(self._find(sid))

    def delete(self, sid):
        row = self._find(sid)
        if not row:
            return
        try:
            os.remove(row["_path"])
        except OSError:
            return
        with self._lock:
            self._list_cache = (0.0, [])
            self._row_cache.pop(row["_path"], None)
