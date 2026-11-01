"""本机会话全文检索：流式读引擎记录，增量落 SQLite FTS。

索引只收真人与助手可见正文，不收系统注入、思考和工具输出。会话消息文件只保留
最近若干回合，因此历史检索以引擎原始记录为准；找不到原始记录时才退回 Desk 投影。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from deskcfg import DATA_HOME


INDEX_VERSION = "2"
SEARCH_LIMIT = 400


def _at_ms(value):
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:
        return 0


def _part_text(parts):
    return "\n\n".join(str(p.get("text") or "").strip() for p in (parts or [])
                         if isinstance(p, dict) and p.get("type") == "text"
                         and str(p.get("text") or "").strip()).strip()


def contract_documents(messages):
    """契约消息 → 检索文档，并为每条消息标出从前往后的回合号。"""
    docs, turn = [], -1
    for message in messages or []:
        info = message.get("info") or {}
        role = info.get("role")
        if role == "user" and not (info.get("compact") or info.get("goal") or info.get("aside")):
            text = _part_text(message.get("parts"))
            if text:
                turn += 1
        elif role != "assistant":
            continue
        text = _part_text(message.get("parts"))
        if text and turn >= 0:
            docs.append({"message_id": str(info.get("id") or ""), "role": role,
                         "at": int(info.get("at") or 0), "turn": turn, "text": text})
    return docs, turn + 1


def projection_documents(path):
    rows = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    rows.append(json.loads(line))
                except (ValueError, TypeError):
                    continue
    except OSError:
        return [], 0
    return contract_documents(rows)


def claude_documents(adapter, path, sid, record):
    """按 Claude 现有投影口径流式摘正文，避免把数百 MB 记录整棵读进内存。"""
    docs, turn = [], -1
    assistant, assistant_id, assistant_at = [], "", 0
    aside_id = ""
    cuts = list((record or {}).get("recall_cuts") or [])

    def flush():
        nonlocal assistant, assistant_id, assistant_at, aside_id
        text = "\n\n".join(assistant).strip()
        if text and turn >= 0:
            mid = ("msg_x_" + aside_id) if aside_id else ("msg_a_" + assistant_id)
            docs.append({"message_id": mid, "role": "assistant", "at": assistant_at,
                         "turn": turn, "text": text})
        assistant, assistant_id, assistant_at, aside_id = [], "", 0, ""

    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return [], 0
    with fh:
        for line in fh:
            try:
                row = json.loads(line)
            except (ValueError, TypeError):
                continue
            typ = row.get("type")
            if typ not in ("user", "assistant") or row.get("isSidechain"):
                continue
            at = _at_ms(row.get("timestamp"))
            if at and any(a <= at <= b for a, b in cuts):
                continue
            message = row.get("message") or {}
            if typ == "assistant" and message.get("model") == "<synthetic>":
                continue
            if typ == "user" and row.get("isMeta"):
                continue
            if typ == "user" and getattr(adapter, "_injected_user", lambda _r: False)(row):
                flush()
                aside_id = str(row.get("uuid") or "")
                continue
            content = message.get("content")
            blocks = content if isinstance(content, list) else (
                [{"type": "text", "text": content}] if isinstance(content, str) else [])
            if typ == "user" and any(isinstance(b, dict) and b.get("type") == "tool_result"
                                     for b in blocks):
                continue
            texts = [str(b.get("text") or "").strip() for b in blocks
                     if isinstance(b, dict) and b.get("type") == "text"
                     and str(b.get("text") or "").strip()
                     and not str(b.get("text") or "").lstrip().startswith("<system-reminder>")]
            text = "\n\n".join(texts).strip()
            if typ == "user":
                if row.get("isCompactSummary") or not text:
                    continue
                if text.startswith(("<local-command-stdout>", "<local-command-stderr>",
                                    "<local-command-caveat>")):
                    continue
                if text.startswith(("<command-name>", "<command-message>", "<command-args>")):
                    name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
                    args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
                    text = ((name.group(1).strip() if name else "") + " " +
                            (args.group(1).strip() if args else "")).strip()
                interrupted = re.match(r"\[Request interrupted by user[^\]]*\]\s*", text)
                if interrupted:
                    text = text[interrupted.end():].strip()
                if not text:
                    continue
                flush()
                turn += 1
                docs.append({"message_id": "msg_u_" + str(row.get("uuid") or turn),
                             "role": "user", "at": at, "turn": turn, "text": text})
            elif text:
                if not assistant_id:
                    assistant_id = str(row.get("uuid") or len(docs))
                assistant_at = at or assistant_at
                assistant.append(text)
    flush()
    return docs, turn + 1


def codex_documents(adapter, path, sid, record):
    """流式摘 Codex 对话正文；消息 id 与 _group_rollout 的时间戳规则一致。"""
    docs, turn, used = [], -1, {}
    assistant, assistant_at, last_at = [], 0, 0
    cuts = list((record or {}).get("recall_cuts") or [])
    dropping = False

    def mkid(kind, at):
        base = f"msg_{kind}_{at}"
        n = used.get(base, 0)
        used[base] = n + 1
        return base if not n else f"{base}_{n}"

    def flush():
        nonlocal assistant, assistant_at
        text = "\n\n".join(assistant).strip()
        if text and turn >= 0:
            docs.append({"message_id": mkid("a", assistant_at or last_at),
                         "role": "assistant", "at": assistant_at or last_at,
                         "turn": turn, "text": text})
        assistant, assistant_at = [], 0

    esid = str((record or {}).get("engine_sid") or "")
    try:
        fh = open(path, "rb")
    except OSError:
        return [], 0
    with fh:
        for raw in fh:
            try:
                row = json.loads(raw)
            except (ValueError, TypeError):
                continue
            at = int(getattr(adapter, "_iso_ms", _at_ms)(row.get("timestamp")) or 0)
            if at:
                last_at = at
            try:
                if esid and getattr(adapter, "_goal_from_rollout_output", lambda *_: None)(row, esid):
                    flush()
                    continue
            except Exception:
                pass
            payload = row.get("payload") or {}
            if row.get("type") == "event_msg" and payload.get("type") == "turn_aborted":
                flush()
                continue
            for item in getattr(adapter, "_rollout_items", lambda _r: [])(row):
                if item.get("type") != "message":
                    continue
                role = item.get("role")
                if role == "user":
                    text = getattr(adapter, "_rollout_user_text")(item)
                    if not text:
                        continue
                    flush()
                    dropping = bool(at and any(int(a) <= at <= int(b) for a, b in cuts))
                    if dropping:
                        continue
                    turn += 1
                    docs.append({"message_id": mkid("u", at), "role": "user", "at": at,
                                 "turn": turn, "text": text})
                elif (not dropping and role == "assistant"
                      and item.get("phase") != "commentary"):
                    text = getattr(adapter, "_rollout_message_text")(item)
                    if text:
                        assistant.append(text)
                        assistant_at = at or assistant_at
    flush()
    return docs, turn + 1


def _signature(path):
    try:
        st = os.stat(path)
        return f"{os.path.realpath(path)}:{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return ""


def _has_message(messages, message_id):
    if not message_id:
        return True
    for message in messages or []:
        info = message.get("info") or {}
        if str(info.get("id") or "") == message_id:
            return True
        if any(str((part or {}).get("messageID") or "") == message_id
               for part in (message.get("parts") or []) if isinstance(part, dict)):
            return True
    return False


def history_around(adapter, sid, turn_from_end, before=6, after=6, message_id=""):
    """返回目标回合附近的小窗口；消息标识用于校正索引后新增回合造成的偏移。"""
    turn_from_end = max(0, int(turn_from_end or 0))
    before, after = max(0, int(before)), max(0, int(after))
    def window(target):
        newer = max(0, target - after)
        limit = before + 1 + min(after, target)
        rows = adapter.history(sid, newer, limit) if hasattr(adapter, "history") else []
        if not rows:
            rows = adapter._history_window(adapter.messages(sid), newer, limit)
        return rows or [], newer

    rows, newer = window(turn_from_end)
    message_id = str(message_id or "")
    if message_id and not _has_message(rows, message_id):
        # 搜索结束到点击之间可能又完成了若干回合；此时「距末尾几轮」会变，消息 id 不变。
        # 只在猜测窗口未命中时流式读一次源记录校正，正常点击没有额外开销。
        record = dict((adapter.index or {}).get(sid) or {})
        path = adapter.search_source_path(sid, record)
        docs, turns = adapter.search_documents(sid, record, path)
        hit = next((doc for doc in docs if doc["message_id"] == message_id), None)
        if hit is not None:
            turn_from_end = max(0, turns - 1 - int(hit["turn"]))
            rows, newer = window(turn_from_end)
    return {"messages": rows or [], "newer": newer}


class SessionSearch:
    def __init__(self, path=None, engine=""):
        self.path = path or os.path.join(DATA_HOME, "session-search.sqlite3")
        self.engine = str(engine or "")
        self.lock = threading.Lock()
        self.building = False
        self.done = 0
        self.total = 0
        self.error = ""
        self._schema()

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute("PRAGMA journal_mode=WAL")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _schema(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._db() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version != int(INDEX_VERSION):
                db.execute("DROP TABLE IF EXISTS docs")
                db.execute("DROP TABLE IF EXISTS sessions")
            db.execute("CREATE TABLE IF NOT EXISTS sessions ("
                       "sid TEXT PRIMARY KEY, engine TEXT, title TEXT, domain TEXT, updated INTEGER, "
                       "signature TEXT, turns INTEGER)")
            try:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
                           "text, sid UNINDEXED, message_id UNINDEXED, turn_from_end UNINDEXED, "
                           "role UNINDEXED, at UNINDEXED, engine UNINDEXED, tokenize='trigram')")
            except sqlite3.OperationalError:
                db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
                           "text, sid UNINDEXED, message_id UNINDEXED, turn_from_end UNINDEXED, "
                           "role UNINDEXED, at UNINDEXED, engine UNINDEXED)")
            db.execute(f"PRAGMA user_version={int(INDEX_VERSION)}")

    def snapshot(self, adapter):
        if self.engine and adapter.name != self.engine:
            raise ValueError("search index belongs to another engine")
        out = []
        with adapter._lock:
            rows = [(sid, dict(record)) for sid, record in (adapter.index or {}).items()]
        for sid, record in rows:
            if record.get("task"):
                continue
            path = adapter.search_source_path(sid, record)
            out.append({"adapter": adapter, "sid": sid, "record": record, "path": path,
                        "signature": _signature(path)})
        return out

    def ensure(self, adapter):
        snapshot = self.snapshot(adapter)
        with self._db() as db:
            have = {row[0]: tuple(row[1:]) for row in db.execute(
                "SELECT sid,signature,engine,title,domain,updated FROM sessions")}
        wanted = {}
        for item in snapshot:
            record = item["record"]
            wanted[item["sid"]] = (item["signature"], item["adapter"].name,
                                    str(record.get("title") or "对话"),
                                    str(record.get("domain") or ""),
                                    int((record.get("time") or {}).get("updated") or 0))
        stale = set(have) != set(wanted) or any(have.get(sid) != sig for sid, sig in wanted.items())
        if not stale:
            return
        with self.lock:
            if self.building:
                return
            self.building, self.done, self.total, self.error = True, 0, len(snapshot), ""
        threading.Thread(target=self._sync, args=(snapshot,), name="session-search", daemon=True).start()

    def _sync(self, snapshot):
        seen = set()
        try:
            for item in snapshot:
                adapter, sid, record = item["adapter"], item["sid"], item["record"]
                seen.add(sid)
                with self._db() as db:
                    old = db.execute("SELECT signature FROM sessions WHERE sid=?", (sid,)).fetchone()
                    if not old or old[0] != item["signature"]:
                        docs, turns = adapter.search_documents(sid, record, item["path"])
                        db.execute("DELETE FROM docs WHERE sid=?", (sid,))
                        for doc in docs:
                            db.execute("INSERT INTO docs VALUES (?,?,?,?,?,?,?)", (
                                doc["text"], sid, doc["message_id"],
                                max(0, turns - 1 - int(doc["turn"])), doc["role"],
                                int(doc["at"] or 0), adapter.name))
                    else:
                        row = db.execute("SELECT turns FROM sessions WHERE sid=?", (sid,)).fetchone()
                        turns = int((row or [0])[0] or 0)
                    tm = record.get("time") or {}
                    db.execute("INSERT OR REPLACE INTO sessions VALUES (?,?,?,?,?,?,?)", (
                        sid, adapter.name, str(record.get("title") or "对话"),
                        str(record.get("domain") or ""), int(tm.get("updated") or 0),
                        item["signature"], turns))
                with self.lock:
                    self.done += 1
            with self._db() as db:
                for (sid,) in db.execute("SELECT sid FROM sessions").fetchall():
                    if sid not in seen:
                        db.execute("DELETE FROM docs WHERE sid=?", (sid,))
                        db.execute("DELETE FROM sessions WHERE sid=?", (sid,))
        except Exception as exc:
            with self.lock:
                self.error = str(exc)[:200]
        finally:
            with self.lock:
                self.building = False

    @staticmethod
    def _snippet(text, query, radius=70):
        text, query = str(text or "").strip(), str(query or "").strip()
        at = text.lower().find(query.lower())
        if at < 0:
            at = 0
        lo, hi = max(0, at - radius), min(len(text), at + len(query) + radius)
        chunk = re.sub(r"\s+", " ", text[lo:hi]).strip()
        return ("…" if lo else "") + chunk + ("…" if hi < len(text) else "")

    #: 每段会话留几条命中。展示只用头两条,其余留给「展开」——展开是纯前端动作,
    #: 再打一趟接口在远端要一两秒,而这些片段本来就在这次结果里。
    HITS_KEEP = 10

    def query(self, query, limit=30, sort=""):
        query = str(query or "").strip()
        if len(query) < 2:
            return self.state([])
        params = []
        if len(query) >= 3:
            literal = '"' + query.replace('"', '""') + '"'
            sql = ("SELECT text,sid,message_id,turn_from_end,role,at,engine,bm25(docs) "
                   "FROM docs WHERE docs MATCH ?")
            params.append(literal)
        else:
            sql = ("SELECT text,sid,message_id,turn_from_end,role,at,engine,0 "
                   "FROM docs WHERE instr(lower(text),lower(?))>0")
            params.append(query)
        if self.engine:
            sql += " AND engine=?"
            params.append(self.engine)
        sql += " ORDER BY 8, CAST(at AS INTEGER) DESC LIMIT ?"
        params.append(SEARCH_LIMIT)
        with self._db() as db:
            rows = db.execute(sql, params).fetchall()
            meta = {row[0]: row[1:] for row in db.execute(
                "SELECT sid,engine,title,domain,updated FROM sessions").fetchall()}
        groups = {}
        for text, sid, mid, turn_from_end, role, at, row_engine, score in rows:
            if sid not in meta:
                continue
            group = groups.setdefault(sid, {"session_id": sid, "engine": meta[sid][0],
                "title": meta[sid][1], "domain": meta[sid][2], "updated": meta[sid][3],
                "count": 0, "score": float(score or 0), "hits": []})
            group["count"] += 1
            group["score"] = min(group["score"], float(score or 0))
            group["newest"] = max(group.get("newest", 0), int(at or 0))
            if len(group["hits"]) < self.HITS_KEEP:
                group["hits"].append({"message_id": mid, "turn_from_end": int(turn_from_end or 0),
                                      "role": role, "at": int(at or 0),
                                      "snippet": self._snippet(text, query)})
        # 按时间排时用「这段会话里最新的那条命中」,不用会话的 updated:两者经常差很远
        # (同一批结果里,会话更新在今天而命中在三周前),而人要的是「哪句话说得最近」。
        # 卡内的命中也跟着按时间排,否则卡片之间按时间、卡片之内按相关度,读起来是两套秩序。
        if str(sort or "") == "time":
            for group in groups.values():
                group["hits"].sort(key=lambda h: -h["at"])
            results = sorted(groups.values(), key=lambda x: -x["newest"])[:max(1, limit)]
        else:
            results = sorted(groups.values(), key=lambda x: (x["score"], -x["updated"]))[:max(1, limit)]
        for group in results:
            group.pop("score", None)
            group.pop("newest", None)
        return self.state(results)

    def state(self, results):
        with self.lock:
            return {"results": results, "indexing": self.building,
                    "progress": {"done": self.done, "total": self.total},
                    "error": self.error}


_SEARCHES = {}
_SEARCH_LOCK = threading.Lock()


def search_sessions(query, adapter, sort=""):
    """只检索指定 adapter 的独立索引；调用方不能省略引擎后跨库查询。"""
    engine = str(adapter.name or "")
    if not engine or not getattr(adapter, "search_supported", False):
        return {"results": [], "indexing": False, "progress": {"done": 0, "total": 0},
                "error": "该引擎不支持会话搜索"}
    with _SEARCH_LOCK:
        search = _SEARCHES.get(engine)
        if search is None:
            safe = re.sub(r"[^a-zA-Z0-9_.-]", "_", engine)
            path = os.path.join(DATA_HOME, "session-search", safe + ".sqlite3")
            search = _SEARCHES[engine] = SessionSearch(path, engine=engine)
    search.ensure(adapter)
    return search.query(query, sort=sort)
