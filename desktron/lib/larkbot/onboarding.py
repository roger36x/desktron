"""Bot 接入进度独立于终端视图保存；只存元数据，不存授权票据或 Secret。"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import contextlib
import fcntl
import json
import os
import re
import shlex
import time
import uuid
from pathlib import Path

ROOT = Path.home() / ".config/desktron/bot-onboarding"
FIELDS = {"phase", "app_id", "name", "agent_id", "create_requested"}


@contextlib.contextmanager
def _locked():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(ROOT / ".lock", "a") as lock:
        os.chmod(lock.name, 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def _path(jid):
    if not re.fullmatch(r"[a-f0-9]{32}", str(jid)):
        raise ValueError("接入记录无效")
    return ROOT / (jid + ".json")


def _read(jid):
    with _path(jid).open(encoding='utf-8') as f:
        return json.load(f)


def _save(job):
    job["updated"] = time.time()
    path = _path(job["id"])
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding='utf-8') as f:
        json.dump(job, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _alive(job):
    import termpty
    return any(t["id"] == job.get("terminal") and t.get("alive") for t in termpty.pty_list())


def _public(job):
    out = dict(job, alive=_alive(job))
    if not out["alive"] and out["phase"] not in ("complete", "cancelled", "needs_attention"):
        out["phase"] = "interrupted"
    return out


def get(jid):
    with _locked():
        return _public(_read(jid))


def listing():
    with _locked():
        jobs = [json.loads(p.read_text(encoding='utf-8')) for p in ROOT.glob("*.json")]
        return [_public(j) for j in sorted(jobs, key=lambda j: j["updated"], reverse=True)
                if not j.get("hidden")]


def checkpoint(jid, generation, **fields):
    """只有当前运行能写入；取消后或旧进程的迟到结果不能覆盖新一轮。"""
    if set(fields) - FIELDS:
        raise ValueError("接入记录仅允许保存公开进度字段")
    with _locked():
        job = _read(jid)
        if job["generation"] != generation or job["phase"] == "cancelled":
            raise RuntimeError("接入已取消或由新一轮接管")
        job.update(fields)
        _save(job)


def _launch(job):
    import deskcfg
    import term
    job["generation"] += 1
    job["phase"] = "running"
    _save(job)  # 进程启动前持久化，刷新页面不会再建一份
    wizard = Path(__file__).resolve().parents[2] / "bin/lark-bot-wizard.py"
    argv = [str(wizard), "--have-app" if job["have_app"] else "--auto",
            "--onboarding", job["id"], "--generation", str(job["generation"]),
            "--port", str(deskcfg.UI_PORT)]
    if job["backend"]:
        argv += ["--backend", job["backend"]]
    sess = term._shell_term_open(command=shlex.join(argv))
    if not sess:
        job["phase"] = "interrupted"
        _save(job)
        raise RuntimeError("接入终端未能启动，可稍后继续")
    job["terminal"] = sess["id"]
    _save(job)
    return _public(job)


def start(backend="", have_app=False, jid=None):
    if backend and not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", backend):
        raise ValueError("目标设备无效")
    with _locked():
        if jid:
            job = _read(jid)
            if job["phase"] in ("complete", "cancelled"):
                return _public(job)
        else:
            # 重复点击、请求响应丢失后重试，都继续同一目标上的未完成记录。
            jobs = [json.loads(p.read_text(encoding='utf-8')) for p in ROOT.glob("*.json")]
            job = next((j for j in jobs if j["backend"] == backend
                        and j["have_app"] == bool(have_app)
                        and j["phase"] not in ("complete", "cancelled")), None)
            if job is None:
                job = {"id": uuid.uuid4().hex, "backend": backend, "have_app": bool(have_app),
                       "generation": 0, "phase": "pending", "created": time.time(),
                       "terminal": "", "app_id": "", "agent_id": "", "name": "",
                       "create_requested": False}
        return _public(job) if _alive(job) else _launch(job)


def cancel(jid):
    import termpty
    with _locked():
        job = _read(jid)
        job.update(phase="cancelled", hidden=True, generation=job["generation"] + 1)
        _save(job)
        # 仅结束这条接入进程；不删除飞书应用或已登记的 Bot。
        if job.get("terminal"):
            termpty.pty_kill(job["terminal"])
    return {"ok": True}
