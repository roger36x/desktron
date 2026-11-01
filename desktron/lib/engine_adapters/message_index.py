"""Rebuildable byte-offset index for the adapter JSONL read model.

The JSONL file remains authoritative.  This sidecar only records message and turn
start offsets, so UI windows can seek directly to the requested turns instead of
parsing the complete projection.  Every read validates the source inode/size/mtime;
a missing, stale or corrupt sidecar is rebuilt under the caller's per-session lock.
"""
from __future__ import annotations

import json
import os
import threading


VERSION = 2
MAX_SNAPSHOT_RETRIES = 4


class JsonlTurnIndex:
    def __init__(self, path, is_turn_start):
        self.path = os.fspath(path)
        self.sidecar = self.path + ".turn-index.jsonl"
        self.is_turn_start = is_turn_start
        self._cached = None

    @staticmethod
    def _stat_stamp(st):
        return {"dev": st.st_dev, "ino": st.st_ino, "mtime_ns": st.st_mtime_ns,
                "size": st.st_size}

    @classmethod
    def _stamp(cls, path):
        return cls._stat_stamp(os.stat(path))

    @staticmethod
    def _valid_offsets(messages, turns, size):
        return (all(isinstance(offset, int) and 0 <= offset < size
                    for offset in messages)
                and messages == sorted(set(messages))
                and turns == sorted(set(turns))
                and set(turns).issubset(messages))

    def _load(self):
        stamp = self._stamp(self.path)
        if self._cached is not None and self._cached.get("source") == stamp:
            return self._cached
        try:
            messages, turns, footer = [], [], None
            with open(self.sidecar, encoding="utf-8") as stream:
                header = json.loads(stream.readline())
                if (header.get("version") != VERSION
                        or header.get("identity") != {key: stamp[key] for key in ("dev", "ino")}):
                    raise ValueError("stale turn index")
                for line in stream:
                    value = json.loads(line)
                    if isinstance(value, list) and len(value) == 2:
                        offset, starts_turn = value
                        if not isinstance(offset, int) or not isinstance(starts_turn, bool):
                            raise ValueError("invalid turn index entry")
                        messages.append(offset)
                        if starts_turn:
                            turns.append(offset)
                    elif isinstance(value, dict) and set(value) in ({"source"}, {"source", "valid_size"}):
                        footer = value
                    else:
                        raise ValueError("invalid turn index row")
            if footer is None or footer.get("source") != stamp:
                raise ValueError("stale turn index")
            valid_size = footer.get("valid_size", stamp["size"])
            if (not isinstance(valid_size, int) or not 0 <= valid_size <= stamp["size"]
                    or not self._valid_offsets(messages, turns, valid_size)):
                raise ValueError("stale turn index")
            self._cached = {"version": VERSION, "source": stamp, "valid_size": valid_size,
                            "messages": messages, "turns": turns}
            return self._cached
        except (OSError, ValueError, TypeError, AttributeError):
            return self._rebuild()

    def _store(self, row):
        tmp = "%s.%d.%d.tmp" % (self.sidecar, os.getpid(), threading.get_ident())
        try:
            identity = {key: row["source"][key] for key in ("dev", "ino")}
            turns = set(row["turns"])
            with open(tmp, "w", encoding="utf-8") as stream:
                stream.write(json.dumps({"version": VERSION, "identity": identity},
                                        separators=(",", ":")) + "\n")
                for offset in row["messages"]:
                    stream.write(json.dumps([offset, offset in turns],
                                            separators=(",", ":")) + "\n")
                stream.write(json.dumps({"source": row["source"],
                                         "valid_size": row.get("valid_size", row["source"]["size"])},
                                        separators=(",", ":")) + "\n")
            os.replace(tmp, self.sidecar)
            self._cached = row
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass

    def _rebuild(self):
        for _attempt in range(MAX_SNAPSHOT_RETRIES):
            messages, turns, valid_size = [], [], 0
            with open(self.path, "rb") as stream:
                while True:
                    offset = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    if not line.strip():
                        valid_size = stream.tell()
                        continue
                    try:
                        value = json.loads(line)
                    except (ValueError, UnicodeDecodeError):
                        # Only an incomplete final write is recoverable.  A corrupt
                        # middle row must not silently merge the following records.
                        if not line.endswith(b"\n") and stream.read(1) == b"":
                            break
                        raise ValueError("invalid JSONL projection at byte %d" % offset)
                    messages.append(offset)
                    if self.is_turn_start(value):
                        turns.append(offset)
                    valid_size = stream.tell()
                source = self._stat_stamp(os.fstat(stream.fileno()))
            if source == self._stamp(self.path):
                break
            # The path was replaced while the old inode was being scanned. Retry
            # from the new authority, but never recurse forever under a hot writer.
        else:
            raise RuntimeError("JSONL projection changed during every index rebuild")
        row = {"version": VERSION, "source": source, "valid_size": valid_size,
               "messages": messages, "turns": turns}
        # The index is disposable, but replacement must be atomic so readers never
        # observe half JSON.  No fsync is needed: a lost sidecar is rebuilt.
        self._store(row)
        return row

    def refresh(self):
        self._cached = None
        try:
            return self._rebuild()
        except FileNotFoundError:
            try:
                os.remove(self.sidecar)
            except OSError:
                pass
            return {"version": VERSION, "source": {}, "messages": [], "turns": []}

    def append(self, value):
        """Append one record and advance the sidecar in O(1) bytes."""
        try:
            row = self._load()
        except FileNotFoundError:
            row = {"version": VERSION, "source": {}, "messages": [], "turns": []}
        raw = (json.dumps(value, ensure_ascii=False) + "\n").encode("utf-8")
        valid_size = row.get("valid_size", (row.get("source") or {}).get("size", 0))
        with open(self.path, "r+b" if os.path.exists(self.path) else "w+b") as stream:
            stream.truncate(valid_size)
            if valid_size:
                stream.seek(valid_size - 1)
                if stream.read(1) != b"\n":
                    stream.seek(0, os.SEEK_END)
                    stream.write(b"\n")
            stream.seek(0, os.SEEK_END)
            offset = stream.tell()
            stream.write(raw)
        starts_turn = bool(self.is_turn_start(value))
        row["messages"].append(offset)
        if starts_turn:
            row["turns"].append(offset)
        row["source"] = self._stamp(self.path)
        row["valid_size"] = row["source"]["size"]
        self._cached = row
        if not os.path.exists(self.sidecar):
            self._store(row)
            return
        # One append block is enough.  Old source footers remain as checkpoints;
        # the loader accepts only the final footer, so a torn/corrupt tail causes
        # a rebuild from the authoritative JSONL instead of a partial index read.
        try:
            with open(self.sidecar, "a", encoding="utf-8") as stream:
                stream.write(json.dumps([offset, starts_turn], separators=(",", ":")) + "\n")
                stream.write(json.dumps({"source": row["source"], "valid_size": row["valid_size"]},
                                        separators=(",", ":")) + "\n")
        except OSError:
            self._cached = None

    def raw_window(self, *, have=0, limit=None):
        """Return complete JSONL records for a turn window.

        ``have`` skips turns from the newest end.  ``limit=None`` selects every
        remaining turn and is used for the initial recent-window response.
        """
        have = max(0, int(have or 0))
        limit = None if limit is None else max(0, int(limit))
        for _attempt in range(MAX_SNAPSHOT_RETRIES):
            try:
                row = self._load()
            except FileNotFoundError:
                return b""
            turns = row["turns"]
            if not turns:
                begin, end = 0, row.get("valid_size", row["source"]["size"])
                if not row["messages"] or limit == 0:
                    return b""
            else:
                end_turn = max(0, len(turns) - have)
                if end_turn <= 0:
                    return b""
                start_turn = 0 if limit is None else max(0, end_turn - limit)
                if start_turn == end_turn:
                    return b""
                # Records before the first human turn belong to the oldest page.
                # This preserves compact/goal/aside prelude rows without making
                # every newer page reread the JSONL prefix.
                begin = 0 if start_turn == 0 else turns[start_turn]
                end = turns[end_turn] if end_turn < len(turns) else row.get(
                    "valid_size", row["source"]["size"])
            with open(self.path, "rb") as stream:
                if self._stat_stamp(os.fstat(stream.fileno())) == row["source"]:
                    stream.seek(begin)
                    return stream.read(end - begin)
            self._cached = None
        raise RuntimeError("JSONL projection changed during every indexed read")

    def turn_count(self):
        try:
            return len(self._load()["turns"])
        except FileNotFoundError:
            return 0

    def trim_to_recent(self, turns):
        """Rewrite the source to its newest turns using a streaming byte copy."""
        try:
            row = self._load()
        except FileNotFoundError:
            return False
        starts = row["turns"]
        turns = max(0, int(turns))
        if len(starts) <= turns:
            return False
        cut = starts[len(starts) - turns] if turns else row["source"]["size"]
        tmp = "%s.%d.%d.tmp" % (self.path, os.getpid(), threading.get_ident())
        try:
            with open(self.path, "rb") as source, open(tmp, "wb") as target:
                source.seek(cut)
                while True:
                    block = source.read(1024 * 1024)
                    if not block:
                        break
                    target.write(block)
            os.replace(tmp, self.path)
            self.refresh()
            return True
        except OSError:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise
