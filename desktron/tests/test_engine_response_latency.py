"""本地延迟预算：用真实调度函数，模型和外部 MCP 不参与计时。"""
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
from unittest import mock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import domains
from engine_adapters.cli_turn import TurnRunnerMixin
from engine_adapters.persist import _EngineProc, _PERSIST, _PERSIST_LOCK
import codex_appserver as app
from test_codex_appserver import FakeConnection, CATALOG

ROOT = Path(__file__).resolve().parents[2]


def test_stream_received_text_is_visible_next_frame():
    source = (ROOT / "frontend/app-chat-model.js").read_text(encoding='utf-8')
    opened = re.search(r"\nfunction hsOpenNarrative\(.*?\n\}", source, re.S).group()
    queued = re.search(r"\nfunction hsQueue\(.*?\n\}", source, re.S).group()
    script = r"""
const assert = require('node:assert/strict');
let now=0, nextId=0, frames=new Map(), paints=0;
function requestAnimationFrame(fn) { frames.set(++nextId, fn); return nextId; }
function cancelAnimationFrame(id) { frames.delete(id); }
function hsPaintLive() { paints++; }
function hsScroll() {}
function tick(ms) { now+=ms; const batch=[...frames.values()]; frames.clear(); batch.forEach(f=>f(now)); }
const results=[];
for (const hz of [30,60,120]) for (const size of [40,1000,20000]) {
  const part={text:'',done:true}; const start=now, count=paints;
  hsQueue({},part,'字'.repeat(size));
  hsQueue({},part,'🙂'); // 同一帧的增量合并，不能拆坏 Unicode 或重复绘制
  tick(1000/hz);
  assert.equal(part.text,'字'.repeat(size)+'🙂');
  assert.equal(part.buf,''); assert.equal(part.timer,0);
  assert.equal(part.done,false); assert.equal(paints-count,1);
  assert.equal(frames.size,0); // 空缓冲不再维持后台帧循环
  results.push({hz,chars:size,visible_ms:now-start,paints:paints-count});
}
// 主线程/后台标签暂停后，恢复的第一帧必须追上，不能继续慢慢排旧字。
const stalled={text:''}; hsQueue({},stalled,'已收到'); tick(3000);
assert.equal(stalled.text,'已收到'); assert.equal(frames.size,0);
console.log(JSON.stringify(results));
"""
    result = subprocess.run(["node", "-e", opened + queued + script], capture_output=True,
                            text=True, timeout=10, encoding='utf-8')
    assert result.returncode == 0, result.stderr
    print(result.stdout)


def test_retired_grounding_skips_unused_composition(tmp_path, monkeypatch):
    monkeypatch.setattr(domains, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(domains, "_domain_dir", lambda _: str(tmp_path / "domains/test"))
    monkeypatch.setattr(domains, "_domain_dir_override", lambda _: "")
    monkeypatch.setattr(domains, "_legacy_agents_md_off", lambda: True)
    with mock.patch.object(domains, "_grounding_index", return_value="unused") as build, \
         mock.patch.object(domains, "_skill_index", return_value="unused") as skills, \
         mock.patch.object(domains, "_repos_segment", return_value="") as repos, \
         mock.patch.object(domains, "_context_segment", return_value=""), \
         mock.patch.object(domains, "_provision_memory") as provision, \
         mock.patch.object(domains, "_drop_legacy_agents_md") as drop:
        result = domains._ensure_domain_dir("test")
    assert result == str(tmp_path / "domains/test")
    build.assert_not_called()
    skills.assert_not_called()
    repos.assert_not_called()
    drop.assert_called_once_with(result)
    provision.assert_called_once_with(result, "test")


def test_claude_idle_reader_wakes_without_polling():
    """真实队列/锁；固定卡在一次空队列等待中，避免依赖碰巧踩中的轮询相位。"""
    entered = threading.Event()

    class ObservedQueue(queue.Queue):
        def get(self, *args, **kwargs):
            entered.set()
            return super().get(*args, **kwargs)

    ep = _EngineProc(mock.Mock(), ())
    ep.lines = ObservedQueue()
    runner = TurnRunnerMixin()
    runner._trace = mock.Mock()
    runner._aside_finish = mock.Mock()
    sid = "latency-test-only"
    with _PERSIST_LOCK:
        _PERSIST[sid] = ep
    worker = threading.Thread(target=runner._persist_idle, args=(sid, ep), daemon=True)
    try:
        worker.start()
        assert entered.wait(1)
        start = time.monotonic()
        assert runner._persist_take(sid, ep, {})
        elapsed = time.monotonic() - start
        ep.reader_lock.release()
        worker.join(1)
        assert not worker.is_alive()
        print(f"idle_handoff_ms={elapsed * 1000:.2f}")
        assert elapsed < 0.2
    finally:
        with _PERSIST_LOCK:
            _PERSIST.pop(sid, None)
        ep.wanted = True
        ep.lines.put("")
        worker.join(1)


def test_codex_mcp_inventory_never_blocks_turn_submission():
    class SlowInventory(FakeConnection):
        def request(self, method, *args, **kwargs):
            if method == "mcpServerStatus/list":
                raise AssertionError("展示用的 MCP 清单不能占住发送路径")
            return super().request(method, *args, **kwargs)

    with mock.patch.object(app, "_Connection", SlowInventory), \
         mock.patch.object(app, "catalog", return_value=CATALOG):
        assert app.run_turn("/work", {}, "", "hello", {}, binary="codex") == "thread-1"
    assert "turn/start" in dict(FakeConnection.last.calls)


def test_timing_is_monotonic_first_occurrence_only():
    from engine_adapters import response_timing as rt
    with mock.patch.object(rt.time, "monotonic", side_effect=[10, 10.025, 10.05]):
        timing = rt.ResponseTiming()
        ctx = {"_response_timing": timing}
        rt.mark(ctx, "first_text_ms")
        rt.mark(ctx, "first_text_ms")
        rt.mark(ctx, "ready_ms")
    assert timing.marks == {"first_text_ms": 25, "ready_ms": 50}
    rt.mark({}, "first_text_ms")  # 历史恢复、自续轮和其他引擎可以没有计时器


def test_legacy_grounding_still_builds_when_injection_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(domains, "REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(domains, "_domain_dir", lambda _: str(tmp_path / "domains/test"))
    monkeypatch.setattr(domains, "_domain_dir_override", lambda _: "")
    monkeypatch.setattr(domains, "_legacy_agents_md_off", lambda: False)
    with mock.patch.object(domains, "_grounding_index", return_value="旧路必须可用"), \
         mock.patch.object(domains, "_skill_index", return_value="不应注入的技能清单") as skills, \
         mock.patch.object(domains, "_repos_segment", return_value=""), \
         mock.patch.object(domains, "_context_segment", return_value=""), \
         mock.patch.object(domains, "_provision_memory"):
        result = domains._ensure_domain_dir("test")
    body = (Path(result) / "AGENTS.md").read_text(encoding='utf-8')
    assert "旧路必须可用" in body
    assert "不应注入的技能清单" not in body
    skills.assert_not_called()
    assert (Path(result) / "CLAUDE.md").read_text(encoding='utf-8').strip() == "@AGENTS.md"


def test_codex_late_mcp_auth_failure_remains_visible():
    class LateMcp(FakeConnection):
        def read(self, timeout=30):
            if not getattr(self, "reported", False):
                self.reported = True
                return {"method": "mcpServer/startupStatus/updated", "params": {
                    "name": "browser", "status": "failed", "failureReason": "reauthenticationRequired"}}
            return super().read(timeout)

    seen = []
    with mock.patch.object(app, "_Connection", LateMcp), \
         mock.patch.object(app, "catalog", return_value=CATALOG):
        app.run_turn("/work", {}, "", "hello", {}, binary="codex", on_environment=seen.append)
    assert seen[-1]["state"] == "warning"
    assert seen[-1]["mcp"]["failed"] == 1
    assert any("reauthenticationRequired" in d for d in seen[-1]["details"])


@pytest.mark.skipif(os.environ.get("DESK_ENGINE_LATENCY_PROBE") != "1", reason="真实引擎只读探测需显式启用")
def test_real_codex_readonly_startup_probe(tmp_path):
    # 不恢复真实 thread、不调用 turn/start、不发模型请求；仅初始化和读取 MCP 清单。
    conn = None
    result = {}
    start = time.monotonic()
    try:
        conn = app._Connection(cwd=str(tmp_path), env=os.environ.copy(), binary="codex")
        result["spawn_ms"] = round((time.monotonic() - start) * 1000, 2)
        at = time.monotonic()
        conn.initialize(overall=8)
        result["initialize_ms"] = round((time.monotonic() - at) * 1000, 2)
        at = time.monotonic()
        try:
            status = conn.request("mcpServerStatus/list", {"limit": 100, "detail": "toolsAndAuthOnly"},
                                  timeout=5, overall=8)
            result["mcp_inventory_ms"] = round((time.monotonic() - at) * 1000, 2)
            result["mcp_count"] = len(status.get("data") or [])
        except app.AppServerError as exc:
            result["mcp_inventory_ms"] = round((time.monotonic() - at) * 1000, 2)
            result["mcp_error_type"] = type(exc).__name__
    finally:
        if conn is not None:
            conn.close()
    print(json.dumps(result))
