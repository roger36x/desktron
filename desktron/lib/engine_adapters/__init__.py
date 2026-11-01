# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
import re as re_mod
"""引擎适配器:把 claude code 的官方 CLI headless 模式翻译成 desktron 引擎契约
(7 个端点 + 4 类 SSE 事件)。

v0 取向:包装 CLI(claude -p --output-format stream-json),零新依赖;
SDK(claude-agent-sdk)是同一接口的库形态,需要 hooks/权限回调时可换内核,契约不变。
订阅认证:复用 CLI 本地登录态(claude auth login),适配器不碰凭证。
注:codex 适配器已移除(2026-06-13,降级为社区可选)——按本契约新写一个 CliAdapter 子类即可接入。
会话与消息持久化:适配器自管(DATA/adapter/<engine>/),引擎侧 transcript 不解析。
"""
import atexit, collections, copy, json, os, queue, shutil, subprocess, threading, time, uuid
import urllib.parse as urllib_parse, urllib.request as urllib_request

# 下面的 re-export 全部自动生成:原单文件的每一个顶层名都在,外部 import 路径不变。
# os/time/threading/subprocess 在上方显式 import:测试有 ea.os 这类猴补写法。
from .bus import EventBus, SCENE_SEP, SCENE_SEPS, SESSION_WINDOW, _TurnWatch
from .persist import _ADAPTERS, _EngineProc, _PERSIST, _PERSIST_IDLE, _PERSIST_LOCK, _PERSIST_MAX, _PersistUnusable, _bg_forget, _persist_drop, _persist_evict, _persist_reap, _persist_roster, _persist_trim
from .cli_store import SessionStoreMixin
from .cli_turn import TurnRunnerMixin
from .cli import CliAdapter
from .claude_transcript import ClaudeTranscriptMixin
from .claude import ClaudeAdapter
from .codex_rollout import CodexRolloutMixin
from .codex_subagents import CodexSubagentsMixin
from .codex import CODEX_DELTA, CodexAdapter
from .pi import PiAdapter
from .shepherd import ShepherdAdapter, ShepherdTrialAdapter
try:
    from .persist import _resident
except ImportError:
    pass
