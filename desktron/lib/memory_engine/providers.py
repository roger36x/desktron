"""宿主接入面:Host 注入点集合。

引擎不 import 任何宿主代码。会话采集已移到 Desk 引擎适配器的 recap 路径,Host 不再接收
离线会话源、工作区目录枚举或 cwd 映射;旧 SessionSource 扫描接口于 2026-08-28 退役。
"""
import os
from dataclasses import dataclass


@dataclass
class Host:
    """宿主注入点。repo_root=数据根(工作区树),mem_root 默认 <repo_root>/.memory。"""
    repo_root: str
    mem_root: str = ""
    knowledge_root: str = ""
    log: object = None                  # (mod, msg) -> None
    # 备忘提供者:() -> {ws: [{nid, task, content, hash}]}。人在面板手写的备忘,
    # 信噪比远高于会话记录,是项目模型的高优先级输入。格式归宿主(引擎不认识 notepad),
    # 未注入时生成器退回上一版模型与 journal。
    memos: object = None
    # 备忘被项目模型吸收后回调:(ws, [{nid, hash}]) -> None,宿主记账下次不重复送入。
    memos_done: object = None
    # recap Work Event 的长期认知候选：ws -> [{kind, statement, receipts, confirmed}]。
    # 候选是生成输入，不等于 pinned；是否提升由项目模型契约判定。
    memory_candidates: object = None
    # 当前事实来源：ws -> {workspace_dir, repos, description}。生成项目模型时，代码、任务与
    # 工作区说明优先于历史记忆，避免把旧 notes 里的“待重启/尚未实现”继续带到下一版。
    workspace_context: object = None
    # 旁路模型调用的用量记账：(kind, model, {input, output, cached, cache_write}, outcome) -> None。
    # 只传数字；未注入时不记录。
    record_usage: object = None

    def __post_init__(self):
        self.repo_root = os.path.abspath(self.repo_root)
        self.mem_root = self.mem_root or os.path.join(self.repo_root, ".memory")
        self.knowledge_root = self.knowledge_root or os.path.join(self.repo_root, "knowledge")
        if self.log is None:
            self.log = lambda mod, msg: print(f"[{mod}] {msg}", flush=True)
