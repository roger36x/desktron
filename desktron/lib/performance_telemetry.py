"""Privacy-safe, local-only performance telemetry.

Only bounded scalar metadata is accepted. Sensitive values are HMACed in memory and never
written. Files are daily buckets, retained for at most 14 complete days, and never traversed
through symlinked telemetry directories.
"""
from __future__ import annotations

import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import threading
import time

RETENTION_DAYS = 14
MAX_FILE_BYTES = 2_000_000
HMAC_HEX_LENGTH = 32
_KEY_LOCK = threading.Lock()

_SAFE_NAME = re.compile(r"[A-Za-z0-9._:/+\-]{1,160}\Z")
_ENGINE_NAME = re.compile(r"[A-Za-z0-9_-]{1,48}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{32}\Z")
_OUTCOMES = {"completed", "cancelled", "error"}
_TRANSPORTS = {"unknown", "http_sse", "websocket", "app_server"}
_CHANGE_REASONS = {
    "unknown", "new_conversation", "new_provider_request", "fork",
    "model_route_change", "resident_recreate", "retry_same_payload",
}
_TIMING_FIELDS = {
    "stored_ms", "grounded_ms", "prepared_ms", "submitted_ms", "resident_ms",
    "first_event_ms", "first_reasoning_ms", "first_text_ms", "first_tool_ms",
    "engine_done_ms", "terminal_ms", "committed_ms", "ready_ms",
}
_COMMON_FIELDS = {"seq", "request_seq", "model", "provider"}
_USAGE_FIELDS = _COMMON_FIELDS | {
    "input", "cache_read", "total", "binding_fp", "system_fp", "resident_reused",
    "transport", "continuation_used", "fingerprint_epoch_fp", "request_epoch_fp", "session_fp",
    "cache_key_fp", "conversation_fp", "session_changed", "cache_key_changed",
    "conversation_changed", "change_reason", "system_segment_fp", "tools_segment_fp",
    "history_segment_fp", "reasoning_segment_fp", "tail_segment_fp", "identity_mode",
}
_EVENT_FIELDS = {
    "pi_usage": _USAGE_FIELDS,
    "response_timing": {"seq", "outcome"} | _TIMING_FIELDS,
}


def _private_directory(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or path.is_symlink() or info.st_uid != os.getuid():
        raise ValueError("unsafe telemetry directory")
    os.chmod(path, 0o700)


def _safe_open_append(path: Path):
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        os.close(fd)
        raise ValueError("unsafe telemetry file")
    os.fchmod(fd, 0o600)
    return os.fdopen(fd, "a", encoding="utf-8")


class PerformanceTelemetry:
    def __init__(self, data_dir: str, engine: str, now=None):
        self.data_dir = Path(data_dir)
        self.diagnostics = self.data_dir / "diagnostics"
        self.root = self.diagnostics / "performance"
        self.engine = engine if _ENGINE_NAME.fullmatch(str(engine or "")) else "unknown"
        self.directory = self.root / self.engine
        self.keys = self.diagnostics / "keys"
        self.key_path = self.keys / "ttft-hmac-v1"
        self._now = now or time.time
        self._lock = threading.Lock()
        self._key = None
        self.key_epoch = ""
        self._last_cleanup = 0.0

    def _ensure_directories(self) -> None:
        for path in (self.diagnostics, self.root, self.directory):
            _private_directory(path)

    def _ensure_key(self) -> bytes:
        if self._key is not None:
            return self._key
        with _KEY_LOCK:
            _private_directory(self.diagnostics)
            _private_directory(self.keys)
            try:
                info = os.lstat(self.key_path)
                if not stat.S_ISREG(info.st_mode) or self.key_path.is_symlink() \
                        or info.st_uid != os.getuid():
                    raise ValueError("unsafe telemetry key")
            except FileNotFoundError:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                fd = os.open(self.key_path, flags, 0o600)
                try:
                    os.fchmod(fd, 0o600)
                    os.write(fd, os.urandom(32))
                    os.fsync(fd)
                finally:
                    os.close(fd)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(self.key_path, flags)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                    raise ValueError("unsafe telemetry key")
                os.fchmod(fd, 0o600)
                key = os.read(fd, 64)
            finally:
                os.close(fd)
            if len(key) != 32:
                raise ValueError("invalid telemetry key")
            self._key = key
            self.key_epoch = hashlib.sha256(key).hexdigest()[:12]
            return key

    def fingerprint(self, field: str, value) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", str(field or "")):
            raise ValueError("invalid fingerprint domain")
        if isinstance(value, bytes):
            body = value
        elif isinstance(value, str):
            body = value.encode("utf-8")
        else:
            body = json.dumps(value, ensure_ascii=False, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")
        message = b"desk-ttft-v1\0" + field.encode("ascii") + b"\0" + body
        return hmac.new(self._ensure_key(), message, hashlib.sha256).hexdigest()[:HMAC_HEX_LENGTH]

    def _day(self) -> str:
        return time.strftime("%Y%m%d", time.gmtime(self._now()))

    def _session_prefix(self, sid: str) -> str:
        return self.fingerprint("session_file", str(sid or ""))

    def _event_path(self, sid: str, day=None) -> Path:
        return self.directory / f"{self._session_prefix(sid)}.{day or self._day()}.jsonl"

    def cleanup(self, force=False) -> None:
        now = self._now()
        if not force and now - self._last_cleanup < 24 * 60 * 60:
            return
        self._last_cleanup = now
        if not self.directory.exists():
            return
        info = self.directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or self.directory.is_symlink() or info.st_uid != os.getuid():
            raise ValueError("unsafe telemetry directory")
        cutoff_day = int(time.strftime("%Y%m%d", time.gmtime(now - RETENTION_DAYS * 86400)))
        for path in self.directory.iterdir():
            try:
                item = path.lstat()
                match = re.fullmatch(r"[0-9a-f]{32}\.(\d{8})\.jsonl", path.name)
                if match and stat.S_ISREG(item.st_mode) and int(match.group(1)) < cutoff_day:
                    path.unlink()
            except (OSError, ValueError):
                continue

    @staticmethod
    def _validate_field(name: str, value):
        if name == "seq":
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 10**15:
                raise ValueError("invalid turn sequence telemetry field")
        elif name in {"request_seq", "input", "cache_read", "total"}:
            if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10**9:
                raise ValueError("invalid numeric telemetry field")
        elif name in _TIMING_FIELDS:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 10**9:
                raise ValueError("invalid timing telemetry field")
        elif name in {"resident_reused", "continuation_used", "session_changed",
                      "cache_key_changed", "conversation_changed"}:
            if not isinstance(value, bool):
                raise ValueError("invalid boolean telemetry field")
        elif name in {"model", "provider"}:
            if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
                raise ValueError("invalid label telemetry field")
        elif name.endswith("_fp"):
            if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
                raise ValueError("invalid fingerprint telemetry field")
        elif name == "transport":
            if value not in _TRANSPORTS:
                raise ValueError("invalid transport telemetry field")
        elif name == "identity_mode":
            if value not in {"legacy", "native"}:
                raise ValueError("invalid identity mode telemetry field")
        elif name == "change_reason":
            if value not in _CHANGE_REASONS:
                raise ValueError("invalid change reason telemetry field")
        elif name == "outcome":
            if value not in _OUTCOMES:
                raise ValueError("invalid outcome telemetry field")
        else:
            raise ValueError("unknown telemetry field")

    def append(self, event: str, sid: str, fields: dict) -> bool:
        allowed = _EVENT_FIELDS.get(event)
        if allowed is None or not isinstance(fields, dict) or set(fields) - allowed:
            raise ValueError("unknown telemetry event or field")
        clean = {}
        for name, value in fields.items():
            if value is None or value == "":
                continue
            self._validate_field(name, value)
            clean[name] = value
        try:
            self._ensure_key()
            self._ensure_directories()
            self.cleanup()
            row = {"event": event, "at_ms": int(self._now() * 1000), "key_epoch": self.key_epoch,
                   **clean}
            path = self._event_path(sid)
            data = json.dumps(row, ensure_ascii=True, separators=(",", ":")) + "\n"
            with self._lock:
                if path.exists() and path.stat().st_size >= MAX_FILE_BYTES:
                    return False
                with _safe_open_append(path) as stream:
                    stream.write(data)
                    stream.flush()
                return True
        except (OSError, ValueError):
            return False

    def record_timing(self, sid: str, seq: int, marks: dict, outcome: str) -> bool:
        fields = {"seq": int(seq or 0), "outcome": outcome}
        for name in _TIMING_FIELDS:
            value = (marks or {}).get(name)
            if value is not None:
                fields[name] = value
        return self.append("response_timing", sid, fields)

    def delete(self, sid: str) -> None:
        try:
            if not self.directory.exists() or self.directory.is_symlink():
                return
            prefix = self._session_prefix(sid) + "."
            for path in self.directory.iterdir():
                info = path.lstat()
                if path.name.startswith(prefix) and path.name.endswith(".jsonl") \
                        and stat.S_ISREG(info.st_mode):
                    path.unlink()
        except (OSError, ValueError):
            pass
