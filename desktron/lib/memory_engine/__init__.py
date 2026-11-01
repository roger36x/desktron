"""memory-engine:curator 内核之上、可独立使用的记忆引擎。

分层:curator(band/note/闸/GC/journal/search 内核,无记忆语义)→ 本包(记忆语义:布局契约、
journal 流水、命令纪律、项目记忆生成、候选分析)→ 宿主(Desk 等:
注入备忘与日志,接线 recap 与 UI)。

宿主接入面:providers.Host(memos / memos_done / log 注入)+
scheduler.start_background(host)。引擎不 import 任何宿主代码。
设计:docs/knowledge-memory-management.md。
"""
from . import layout, journal, gate, commands, providers, project_memory, scheduler  # noqa: F401

__version__ = "0.1.0"
