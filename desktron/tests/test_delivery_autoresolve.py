"""意外退出后的未知回执自动收口：写入锁判活、Claude 原生记录取证、后台重探。"""
import calendar
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "desktron" / "lib"))
_argv = sys.argv
sys.argv = ["ui_server.py", str(ROOT / "frontend"), "7999", "7998", str(ROOT)]
try:
    import engine_adapters as ea  # noqa: E402
    from engine_adapters.bus import EventBus  # noqa: E402
    from engine_adapters.delivery_recovery import writer_released  # noqa: E402
    from engine_adapters.pi import PiAdapter  # noqa: E402
finally:
    sys.argv = _argv


class WriterReleased(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "s.native-writer.lock")

    def test_missing_lock_file_means_no_writer(self):
        self.assertTrue(writer_released(self.path))

    def test_lock_held_by_another_process_is_alive(self):
        child = subprocess.Popen([sys.executable, "-c", (
            "import oscompat;import fcntl,os,sys,time;fd=os.open(sys.argv[1],os.O_RDWR|os.O_CREAT,0o600);"
            "fcntl.flock(fd,fcntl.LOCK_EX);print('locked',flush=True);time.sleep(30)"), self.path],
            stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(), "locked")
            self.assertFalse(writer_released(self.path))
        finally:
            child.kill()
            child.wait()
        self.assertTrue(writer_released(self.path))

    def test_probe_does_not_keep_the_lock(self):
        Path(self.path).touch()
        self.assertTrue(writer_released(self.path))
        fd = os.open(self.path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)   # 探测后新写入者仍能立即取得
        finally:
            os.close(fd)


def _row(kind, sec, content, **extra):
    row = {"type": kind, "uuid": f"{kind}-{sec}",
           "timestamp": "2026-10-01T10:00:%02d.000Z" % sec,
           "message": {"role": kind, "content": content}}
    row.update(extra)
    return row


def _assistant(sec, blocks, stop, **extra):
    row = _row("assistant", sec, blocks, **extra)
    row["message"]["stop_reason"] = stop
    return row


class ClaudeNativeOutcome(unittest.TestCase):
    SENT_MS = calendar.timegm(time.strptime("2026-10-01T10:00:10", "%Y-%m-%dT%H:%M:%S")) * 1000

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ad = ea.ClaudeAdapter.__new__(ea.ClaudeAdapter)
        self.ad._lock = threading.RLock()
        self.ad.index = {}
        self.path = os.path.join(self.tmp.name, "t.jsonl")
        self.ad._transcript_path = lambda esid: self.path

    def outcome(self, rows):
        with open(self.path, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        return self.ad.native_turn_outcome("s1", {"message_id": f"msg_u_{self.SENT_MS}",
                                                  "engine_session_id": "e1"})

    def test_final_answer_after_tools_is_completed(self):
        self.assertEqual(self.outcome([
            _row("user", 10, "做一件事"),
            _assistant(11, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], "tool_use"),
            _row("user", 12, [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
            _assistant(13, [{"type": "text", "text": "完成"}], "end_turn")]), "completed")

    def test_tool_without_result_is_interrupted(self):
        self.assertEqual(self.outcome([
            _row("user", 10, "做一件事"),
            _assistant(11, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], "tool_use")]),
            "interrupted")

    def test_synthetic_api_error_is_failed(self):
        self.assertEqual(self.outcome([
            _row("user", 10, "做一件事"),
            _assistant(11, [{"type": "tool_use", "id": "t1", "name": "Bash", "input": {}}], "tool_use"),
            _row("user", 12, [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]),
            _assistant(40, [{"type": "text", "text": "API Error: Connection dropped"}], "stop_sequence",
                       isApiErrorMessage=True)]), "failed")

    def test_previous_interrupt_marker_is_not_the_turn_start(self):
        self.assertEqual(self.outcome([
            _row("user", 7, "[Request interrupted by user]"),
            _row("user", 10, [{"type": "text", "text": "新的问题"}, {"type": "image", "source": {}}]),
            _row("user", 10, [{"type": "text", "text": "[Image: source: /tmp/a.png]"}]),
            _assistant(12, [{"type": "text", "text": "回答"}], "end_turn")]), "completed")

    def test_request_never_recorded_is_interrupted(self):
        self.assertEqual(self.outcome([
            _row("user", 1, "更早的一轮"),
            _assistant(2, [{"type": "text", "text": "旧回答"}], "end_turn")]), "interrupted")

    def test_next_human_turn_bounds_the_scan(self):
        self.assertEqual(self.outcome([
            _row("user", 10, "第一问"),
            _assistant(11, [{"type": "text", "text": "答"}], "end_turn"),
            _row("user", 20, "第二问"),
            _assistant(21, [{"type": "tool_use", "id": "t9", "name": "Bash", "input": {}}], "tool_use")]),
            "completed")


class BackgroundRetry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.workspace = root / "workspace"
        (self.workspace / "prompts").mkdir(parents=True)
        (self.workspace / "prompts/style.md").write_text("")
        self.data = str(root / "data")
        self.ad = PiAdapter(self.data, str(self.workspace), EventBus())
        self.ad.delivery.boot_id = "boot_old"
        self.sid = self.ad.create_session()["id"]
        self.rid = "prompt_sent"
        d = self.ad.delivery
        d.claim(self.sid, self.rid, {"text": "hello"})
        d.stored(self.sid, self.rid, message_id="msg_u_1")
        d.dispatched(self.sid, self.rid)

    def test_resolves_and_publishes_ready_after_writer_exits(self):
        path = self.ad.native_writer_lock(self.sid)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        bus = EventBus()
        events = bus.subscribe()
        with mock.patch("engine_adapters.delivery_recovery.RETRY_DELAYS", (0.05,) * 60):
            restored = PiAdapter(self.data, str(self.workspace), bus)
            self.assertEqual(restored.delivery.get(self.sid, self.rid)["status"], "uncertain")
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            # 结案先于推送：等到 ready 推送真正到达再断言，避免并行负载下的时序抖动。
            rows, deadline = [], time.time() + 10
            while time.time() < deadline and not any(
                    e["type"] == "session.state" and e["properties"].get("state") == "ready" for e in rows):
                while not events.empty():
                    rows.append(events.get_nowait())
                time.sleep(0.05)
        self.assertEqual(restored.delivery.get(self.sid, self.rid)["status"], "finished")
        states = [e["properties"]["state"] for e in rows if e["type"] == "session.state"]
        self.assertEqual(states[-1], "ready")
        envs = [e["properties"] for e in rows if e["type"] == "session.environment"]
        self.assertTrue(any(p.get("stage") == "delivery" and p.get("state") == "ready" for p in envs))


if __name__ == "__main__":
    unittest.main()
