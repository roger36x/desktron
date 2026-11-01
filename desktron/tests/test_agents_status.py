"""Agents 页状态聚合(lib/agents_status.py):纯文件读取,tmp 目录全覆盖。"""
import json
import time
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "lib"))
import agents_status as ags


def _mk_dream(root, last_run="1784639609 candidates=31 fp=abc", latest=None, pmstate=None):
    d = os.path.join(root, ".memory", ".curator", "dream")
    os.makedirs(d, exist_ok=True)
    if last_run is not None:
        with open(os.path.join(d, "last-run"), "w", encoding='utf-8') as f:
            f.write(last_run)
    if pmstate is not None:
        pd = os.path.join(root, ".memory", ".curator", "project-memory")
        os.makedirs(pd, exist_ok=True)
        with open(os.path.join(pd, "engine-state.json"), "w", encoding='utf-8') as f:
            json.dump(pmstate, f)
    if latest is not None:
        with open(os.path.join(d, "latest.md"), "w", encoding='utf-8') as f:
            f.write(latest)


LATEST = """# dreaming 报告

## 自动采集(recap)
- 回合收尾后由引擎适配器直接写 journal;dreaming 不再扫描会话文件

## 项目记忆
- 待刷新：incident、thinking、proj-kbkit
"""


def test_memory_engine_status_parses_dream_artifacts(tmp_path):
    _mk_dream(str(tmp_path), pmstate={"last_run_ws": {"incident": 1784606242, "thinking": 1784555713},
                                     "last_ok": 1784606242, "fails": 0}, latest=LATEST)
    st = ags.memory_engine_status(str(tmp_path))
    assert st["running"] is True
    assert st["last_run"] == 1784639609
    assert st["candidates"] == 31
    assert st["next_run"] == 1784639609 + st["interval_s"]
    assert st["project_memory"]["last_ok"] == 1784606242
    assert st["project_memory"]["ws_done"] == 2
    assert st["project_memory_pending"] == 3
    assert any("项目记忆" in l for l in st["report_tail"])


def test_memory_engine_status_fail_stop(tmp_path):
    _mk_dream(str(tmp_path), pmstate={"last_run_ws": {}, "fails": 3})
    st = ags.memory_engine_status(str(tmp_path))
    assert st["project_memory"]["stopped"] is True
    assert st["project_memory"]["fails"] == 3


def test_project_memory_status_and_failure_reason(tmp_path):
    _mk_dream(str(tmp_path), pmstate={
        "last_run_ws": {"incident": 1784900000, "thinking": 1784800000},
        "last_ok": 1784900000,
        "fails": 3,
        "last_error": {"ts": 1784999999, "reason": "no-sentinel",
                       "detail": "API error: connection refused", "ws": ["incident"]}})
    jd = ags.memory_engine_status(str(tmp_path))["project_memory"]
    assert jd["stopped"] is True and jd["fails"] == 3
    assert jd["ws_done"] == 2
    assert jd["last_ok"] == 1784900000
    assert jd["last_error"]["reason"] == "no-sentinel"
    assert "connection refused" in jd["last_error"]["detail"]


def test_project_memory_host_slept_not_stopped(tmp_path):
    """宿主睡眠中断:原因留痕但 fails 不推进,不显示为停机。"""
    _mk_dream(str(tmp_path), pmstate={
        "last_run_ws": {"risk": 1784900000}, "fails": 0,
        "last_error": {"ts": 1784999999, "reason": "host-slept/timeout",
                       "detail": "超过 600 秒未结束", "ws": ["risk"]}})
    jd = ags.memory_engine_status(str(tmp_path))["project_memory"]
    assert jd["stopped"] is False and jd["fails"] == 0
    assert jd["last_error"]["reason"] == "host-slept/timeout"


def test_project_memory_missing_state(tmp_path):
    """状态文件缺失(从没跑过):给空壳不炸。"""
    _mk_dream(str(tmp_path))
    jd = ags.memory_engine_status(str(tmp_path))["project_memory"]
    assert jd == {"fails": 0, "stopped": False, "ws_done": 0, "last_ok": 0, "last_error": {}}


def test_memory_engine_status_empty_root(tmp_path):
    st = ags.memory_engine_status(str(tmp_path))
    assert st["running"] is True
    assert "last_run" not in st
    assert st["project_memory_pending"] == 0
    assert st["report_tail"] == []


def test_recap_status_distinguishes_success_failure_and_pending(tmp_path):
    data = tmp_path / "data"
    for name, state in {
        "claude": {"last_ok": 100, "last_write": 90, "fails": 0,
                   "last_result": "written", "last_error": {}},
        "codex": {"last_ok": 0, "fails": 2,
                  "last_error": {"ts": 110, "detail": "协议字段冲突"}},
    }.items():
        root = data / "adapter" / name
        root.mkdir(parents=True)
        (root / "recap-state.json").write_text(json.dumps(state), encoding="utf-8")
    requests = data / "adapter" / "codex" / "recap-requests"
    requests.mkdir()
    (requests / "one.json").write_text("{}", encoding="utf-8")
    recap = ags.memory_engine_status(str(tmp_path), data_home=str(data))["recap"]
    assert recap["last_ok"] == 100 and recap["last_write"] == 90
    assert recap["pending"] == 1
    assert recap["failed_engines"] == ["codex"]
    assert recap["engines"]["codex"]["last_error"]["detail"] == "协议字段冲突"


def test_recap_status_prefers_shepherd_over_legacy_failures(tmp_path):
    """GUI 已切到 Shepherd 统一会话层后,旧直连引擎的陈旧失败只作历史详情。"""
    data = tmp_path / "data"
    for name, state in {
        "shepherd": {"last_ok": 300, "last_write": 300, "fails": 0,
                     "last_result": "written", "last_error": {}},
        "codex": {"last_ok": 100, "last_write": 100, "fails": 1,
                    "last_result": "cancelled",
                    "last_error": {"ts": 110, "detail": "app-server 输出通道已关闭，未收到完整终态"}},
    }.items():
        root = data / "adapter" / name
        root.mkdir(parents=True)
        (root / "recap-state.json").write_text(json.dumps(state), encoding="utf-8")
    recap = ags.memory_engine_status(str(tmp_path), data_home=str(data))["recap"]
    assert recap["last_ok"] == 300 and recap["last_write"] == 300
    assert recap["failed_engines"] == []
    assert recap["legacy_failed_engines"] == ["codex"]
    assert recap["engines"]["shepherd"]["last_result"] == "written"


# 成功时刻按「跑测试的此刻」现算,不写死日期:源超过 24 小时没成功就判 degraded,
# 而 last_ok 只取未 degraded 的源(agents_status 的口径)。写死日期的话,过了那天之后
# 三个源全成 degraded、last_ok 变空,这条测试从此必失败——2026-07-16 起就是这样。
def test_agents_status_shape(tmp_path):
    _mk_dream(str(tmp_path))
    st = ags.agents_status(str(tmp_path))
    # desk = Desk 后端自己有没有在跑旧代码(2026-08-21 加,见 test_desk_stale_code.py)
    # resident = 常驻引擎进程占了多少内存、离预算还有多远(2026-08-23 加,
    # 见 test_resident_budget.py;闸从「按个数」改成「按内存」之后必须看得见)
    assert set(st) == {"ts", "memory", "desk", "resident"}
    assert st["memory"]["id"] == "memory-engine"
    assert not hasattr(ags, "lark_sync_status")


# ---- 「服务器有更新」的判据:必须与打包排除清单同口径 ----
# 这一节回归的是一个假警报:界面报「有更新」,而服务器上的内容与本机要推的那份逐字节相同。
# 起因是本机跑了一次测试,Python 在 lib/__pycache__ 下写了个 .pyc——它在排除清单里、
# 根本不会被推送,却让判据认定本机变新了(2026-08-25 实见:全场只有那一个文件比上次推送新)。
# 判据问的必须是「会被推过去的那些东西变了没有」,不是「home 目录里有没有文件动过」。

def _touch(p, mtime):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding='utf-8') as f:
        f.write("x")
    os.utime(p, (mtime, mtime))


def _newest_like_core(home, skip):
    """照 core/api.py 那段的剪枝重放一遍,用来验判据本身。"""
    newest = 0
    for dp, dn, fn in os.walk(home):
        dn[:] = [d for d in dn if d not in skip]
        for name in fn:
            if name.endswith(".pyc"):
                continue
            newest = max(newest, int(os.stat(os.path.join(dp, name)).st_mtime))
    return newest

