"""grounding 的构造：由 Desk 显式产出，不再依赖引擎读盘。

分为「系统约定」与「当前上下文」，切法是是否随数据增长：

- 系统约定（内部键 `constant`）：进入系统提示词。角色与站位、工作流职责、红线、取用命令表。
  长度不随记忆条数、任务勾选、文件访问变化，因此每回合的前缀是稳定的，缓存不会被打断。
- 当前上下文（内部键 `scene`）：完整项目记忆、任务查询入口、引用代码仓的约定。
  适配器通过 context_delivery 每轮重算，在用户帧外投递当前快照，不重复追加为历史消息。

在此之前 grounding 只有一条路：合成一份 AGENTS.md 放进域目录，靠引擎起手自己读盘。
那条路只在 cwd 落域目录时生效，而 GUI 起的会话 cwd 常落工作台根（归属为 _desktop 的
会话尤其如此），于是拿不到任何工作流 grounding。会话既然已由 Desk 以无头方式起，
注入什么就应当由 Desk 决定。渲染逻辑仍复用 domains 里的那几个段落函数，单一源不动。

用法：
    from grounding import build
    g = build(workspace, domain, first_turn=True)
    # constant 与 scene 经 context_delivery 装配进系统/developer 载体，不拼接用户原话。
"""
from .builder import build   # noqa: F401
