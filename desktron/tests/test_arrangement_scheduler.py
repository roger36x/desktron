"""安排调度器：声明持久化、下一次计算、到期领取与单轮执行。"""
import datetime as dt
import json
import os
import sys
import time
import types

import pytest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))

from arrangement import scheduler, store
from larkbot import peers, runner
from larkbot import store as agent_store


def _custom(**changes):
    return dict({"kind": "custom", "unit": "week", "every": 2,
                 "weekdays": [0, 2], "start_date": "2030-01-02", "time": "09:00"}, **changes)


def _next_date(trigger, after):
    stamp = scheduler.next_at(trigger, dt.datetime.fromisoformat(after).timestamp())
    return dt.datetime.fromtimestamp(stamp).isoformat(timespec="minutes") if stamp else None


def test_biweekly_uses_start_week_and_keeps_all_selected_days():
    trig = _custom()
    assert _next_date(trig, "2030-01-01T00:00") == "2030-01-02T09:00"
    assert _next_date(trig, "2030-01-02T09:00") == "2030-01-14T09:00"
    assert _next_date(trig, "2030-01-14T09:00") == "2030-01-16T09:00"
    assert _next_date(trig, "2030-01-24T12:00") == "2030-01-28T09:00"


def test_custom_daily_is_anchored_not_relative_to_restart():
    trig = _custom(unit="day", every=3)
    assert _next_date(trig, "2030-01-02T08:59") == "2030-01-02T09:00"
    assert _next_date(trig, "2030-01-02T09:00") == "2030-01-05T09:00"
    assert _next_date(trig, "2030-02-01T10:00") == "2030-02-04T09:00"


def test_monthly_clamps_to_month_end_without_drifting():
    trig = _custom(unit="month", every=1, start_date="2032-01-31")
    assert _next_date(trig, "2032-01-31T09:00") == "2032-02-29T09:00"
    assert _next_date(trig, "2032-02-29T09:00") == "2032-03-31T09:00"
    assert _next_date(_custom(unit="month", every=1, start_date="2031-01-31"),
                      "2031-01-31T09:00") == "2031-02-28T09:00"
    assert _next_date(_custom(unit="month", every=2, start_date="2031-12-31"),
                      "2031-12-31T09:00") == "2032-02-29T09:00"


def test_end_date_is_inclusive_and_never_returns_later_dates():
    trig = _custom(end_date="2030-01-16")
    assert _next_date(trig, "2030-01-16T08:00") == "2030-01-16T09:00"
    assert _next_date(trig, "2030-01-16T09:00") is None
    assert _next_date(trig, "2030-01-17T00:00") is None
    assert _next_date(_custom(end_date="2030-01-03"), "2030-01-02T09:00") is None


@pytest.mark.parametrize("changes", [
    {"every": 0}, {"every": 1.5}, {"every": True}, {"every": 1000},
    {"unit": "year"}, {"start_date": "2030-02-30"}, {"start_date": ""},
    {"weekdays": []}, {"weekdays": [7]}, {"weekdays": [True]}, {"weekdays": [1.5]},
    {"end_date": "2030-01-01"}, {"end_date": "bad"}, {"time": "24:00"},
])
def test_custom_invalid_rules_rejected(changes):
    with pytest.raises(ValueError):
        scheduler.normalize_trigger(_custom(**changes))


def test_custom_roundtrip_normalizes_weekdays_and_keeps_next_run(tmp_path):
    now = dt.datetime(2030, 1, 1).timestamp()
    with mock.patch.object(scheduler.time, "time", return_value=now):
        decl = scheduler.declaration({"instruction": "测试", "trigger": _custom(weekdays=[2, 0, 2])})
    assert decl["trigger"]["weekdays"] == [0, 2]
    row = store.create(str(tmp_path), decl)
    saved = store.get(str(tmp_path), row["id"])
    assert scheduler.declaration({"instruction": "改指令"}, saved)["next_at"] == decl["next_at"]
    assert saved["trigger"] == decl["trigger"]


def test_custom_due_claim_is_once_and_expired_offline_runs_are_skipped(tmp_path):
    root = str(tmp_path)
    due = dt.datetime(2030, 1, 2, 9).timestamp()
    row = store.create(root, {"instruction": "测试", "enabled": True,
                              "trigger": _custom(end_date="2030-01-02"), "next_at": due})
    with mock.patch.object(scheduler, "start_run") as run:
        assert scheduler.tick(root, due) == 1
        assert scheduler.tick(root, due) == 0
        run.assert_called_once()
    store.update(root, row["id"], {"next_at": due})
    with mock.patch.object(scheduler, "start_run") as run:
        assert scheduler.tick(root, due + 86400) == 0
        run.assert_not_called()
    assert store.get(root, row["id"])["next_at"] == 0


def test_custom_rejects_new_rule_with_no_future_occurrence():
    with mock.patch.object(scheduler.time, "time", return_value=dt.datetime(2030, 1, 3).timestamp()):
        with pytest.raises(ValueError, match="未来执行时间"):
            scheduler.declaration({"instruction": "x", "trigger": _custom(end_date="2030-01-02")})


def _agent(root, aid="babyshark"):
    saved, err = agent_store.save(root, aid, {"name": "BabyShark", "engine": "codex"})
    assert not err
    return saved


def test_declaration_and_store_roundtrip(tmp_path):
    root = str(tmp_path)
    _agent(root)
    decl = scheduler.declaration({
        "instruction": "汇总最近一天的工作变化。", "agent_id": "babyshark",
        "trigger": {"kind": "weekdays", "time": "09:00"},
    })
    row = store.create(root, decl)
    got = store.get(root, row["id"])
    assert got["name"] == "汇总最近一天的工作变化"
    assert got["next_at"] > int(time.time())
    assert json.load(open(store.path(root), encoding="utf-8"))["items"][0]["id"] == row["id"]


def test_declaration_without_bot_uses_desk_agent(tmp_path):
    decl = scheduler.declaration({
        "instruction": "汇总最近一天的变化。",
        "trigger": {"kind": "manual"},
    })
    assert decl["agent_id"] == scheduler.DEFAULT_AGENT_ID


def test_desk_agent_uses_current_engine_without_lark_routing(tmp_path):
    wb = types.SimpleNamespace(_engine_pref=lambda: "codex")
    with mock.patch.dict(sys.modules, {"workbench": wb}):
        agent = scheduler.resolve_agent(str(tmp_path), scheduler.DEFAULT_AGENT_ID)
    assert agent["name"] == "Desktron"
    assert agent["engine"] == "codex"
    assert agent["cwd"] == str(tmp_path)
    assert agent["context"]["routing"] is False
    assert agent["context"]["memory"] is True


def test_run_without_bot_executes_with_desk_agent(tmp_path):
    root = str(tmp_path)
    decl = dict(scheduler.declaration({
        "instruction": "检查今日变化", "trigger": {"kind": "manual"},
    }), id="arr_desk")
    desk = {"id": scheduler.DEFAULT_AGENT_ID, "name": "Desktron", "engine": "codex",
            "cwd": root, "enabled": True, "context": {"routing": False}}
    with mock.patch.object(scheduler, "resolve_agent", return_value=desk), \
         mock.patch.object(scheduler.context, "build", return_value=("PROMPT", {})), \
         mock.patch.object(scheduler.runner, "run", return_value=("完成", "")) as run_once:
        run, err = scheduler.start_run(root, decl, test=True)
        assert not err
        for _ in range(100):
            got = store.run_get(root, run["id"])
            if got and got.get("status") not in ("queued", "running"):
                break
            time.sleep(.01)
    assert got["status"] == "succeeded"
    run_once.assert_called_once()
    assert run_once.call_args.args[0] == "codex"


def test_next_at_obeys_weekday_and_clock():
    base = dt.datetime.now().astimezone().replace(hour=10, minute=0, second=0, microsecond=0)
    nxt = dt.datetime.fromtimestamp(scheduler.next_at(
        {"kind": "weekly", "weekday": 2, "time": "08:30"}, base.timestamp())).astimezone()
    assert nxt > base
    assert nxt.weekday() == 2
    assert (nxt.hour, nxt.minute) == (8, 30)


def test_due_is_claimed_once_before_execution(tmp_path):
    root = str(tmp_path)
    _agent(root)
    row = store.create(root, {
        "name": "到期项", "instruction": "做一次", "agent_id": "babyshark", "enabled": True,
        "trigger": {"kind": "daily", "time": "09:00"}, "next_at": 946684700,
    })
    with mock.patch.object(scheduler, "start_run", return_value=(None, "")) as start:
        assert scheduler.tick(root, now=946684800) == 1
        assert scheduler.tick(root, now=946684800) == 0
    start.assert_called_once()
    assert store.get(root, row["id"])["next_at"] > 946684800


def test_run_uses_agent_context_and_records_result(tmp_path):
    root = str(tmp_path)
    _agent(root)
    decl = dict(scheduler.declaration({
        "name": "测试", "instruction": "检查索引", "agent_id": "babyshark",
        "trigger": {"kind": "manual"},
    }), id="arr_demo")
    with mock.patch.object(scheduler.context, "build", return_value=("PROMPT", {"memory": ["m1"]})), \
         mock.patch.object(scheduler.runner, "run", return_value=("检查完成", "")):
        run, err = scheduler.start_run(root, decl, test=True)
        assert not err
        for _ in range(100):
            got = store.run_get(root, run["id"])
            if got and got.get("status") not in ("queued", "running"):
                break
            time.sleep(.01)
    assert got["status"] == "succeeded"
    assert got["output"] == "检查完成"
    assert got["used"]["memory"] == ["m1"]


def test_invalid_trigger_is_rejected():
    try:
        scheduler.declaration({"instruction": "x", "agent_id": "a",
                               "trigger": {"kind": "cron", "time": "* * * * *"}})
    except ValueError as ex:
        assert "触发方式" in str(ex)
    else:
        raise AssertionError("cron 不在一期支持范围")


def test_peer_forward_allowlist_does_not_accept_lookalike_path():
    peer = {"url": "http://127.0.0.1:1", "token": "x"}
    data, err = peers.call(peer, "/workbench/arrangements-evil")
    assert data is None
    assert "不允许转发" in err


def test_partial_output_with_error_is_still_failed(tmp_path):
    root = str(tmp_path)
    _agent(root)
    decl = dict(scheduler.declaration({
        "name": "超时", "instruction": "检查", "agent_id": "babyshark",
        "trigger": {"kind": "manual"},
    }), id="arr_timeout")
    with mock.patch.object(scheduler.context, "build", return_value=("PROMPT", {})), \
         mock.patch.object(scheduler.runner, "run", return_value=("做到一半", "超时，已终止")):
        run, _ = scheduler.start_run(root, decl, test=True)
        for _ in range(100):
            got = store.run_get(root, run["id"])
            if got and got.get("status") not in ("queued", "running"):
                break
            time.sleep(.01)
    assert got["status"] == "failed"
    assert got["output"] == "做到一半"


def test_once_runner_processes_are_cancelled_on_desk_cleanup():
    proc = mock.Mock(pid=12345)
    proc.poll.return_value = None
    with runner._PROCS_LOCK:
        runner._PROCS.add(proc)
    try:
        with mock.patch.object(runner, "_kill_process") as kill:
            runner.cancel_all()
        kill.assert_called_once_with(proc)
        assert runner._finish_process(proc) is True
    finally:
        with runner._PROCS_LOCK:
            runner._PROCS.discard(proc)
            runner._CANCELLED.discard(proc)


def test_once_schedule_is_claimed_only_once_and_keeps_original_clock(tmp_path):
    now = dt.datetime(2030, 1, 2, 8, 0).timestamp()
    trigger = {"kind": "once", "date": "2030-01-02", "time": "08:05"}
    with mock.patch.object(scheduler.time, "time", return_value=now):
        decl = scheduler.declaration({"instruction": "做一次", "trigger": trigger})
    assert decl["next_at"] == now + 300
    row = store.create(str(tmp_path), decl)
    assert len(store.claim_due(str(tmp_path), now + 900, scheduler.next_at)) == 1
    assert store.claim_due(str(tmp_path), now + 1200, scheduler.next_at) == []
    completed = store.get(str(tmp_path), row["id"])
    assert completed["next_at"] == 0
    with mock.patch.object(scheduler.time, "time", return_value=now + 1200):
        edited = scheduler.declaration({"instruction": "修改指令"}, completed)
    assert edited["next_at"] == 0
    assert edited["trigger"] == trigger


def test_once_schedule_rejects_past_time():
    with pytest.raises(ValueError, match="晚于当前时间"):
        scheduler.declaration({"instruction": "x", "trigger": {
            "kind": "once", "date": "2000-01-01", "time": "00:00"}})


@pytest.mark.parametrize("trigger", [
    {"kind": "once", "date": "2030-02-30", "time": "09:00"},
    {"kind": "once", "date": "2030-01-01", "time": "25:00"},
    {"kind": "once", "time": "09:00"},
    *({"kind": "interval", "minutes": value} for value in [0, -1, 1.5, 10081, True, None]),
])
def test_invalid_precise_schedules_are_rejected(trigger):
    with pytest.raises(ValueError):
        scheduler.normalize_trigger(trigger)


def test_interval_clock_survives_instruction_edit_and_skips_missed_runs(tmp_path):
    with mock.patch.object(scheduler.time, "time", return_value=1000):
        decl = scheduler.declaration({"instruction": "x", "trigger": {"kind": "interval", "minutes": 15}})
    assert decl["next_at"] == 1900
    with mock.patch.object(scheduler.time, "time", return_value=1200):
        assert scheduler.declaration({"instruction": "y"}, decl)["next_at"] == 1900
    row = store.create(str(tmp_path), decl)
    claimed = store.claim_due(str(tmp_path), 10000, scheduler.next_at)
    assert len(claimed) == 1
    assert store.get(str(tmp_path), row["id"])["next_at"] == 10900
    assert store.claim_due(str(tmp_path), 10000, scheduler.next_at) == []
