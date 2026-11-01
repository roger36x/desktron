# 由 engine_adapters.py 分拆(2026-08-27,纯搬移;见 specs/2026-08-27-repo-partition-2.md)
from .cli_store import SessionStoreMixin
from .cli_turn import TurnRunnerMixin
from .recap import RecapMixin


class CliAdapter(SessionStoreMixin, TurnRunnerMixin, RecapMixin):
    recap_generate = None   # 引擎实现了才支持 recap(见 recap.py);None = 静默不做
    """契约实现基类:会话索引 + 消息存储 + prompt 子进程运行。"""

    name = "base"
    model_label = "?"
    binary = None
    # 一回合最多能安静多久(秒)。这是「多久没有任何动静」,不是「一回合最多跑多久」——
    # 目标驱动的长活动辄跑一两个小时,期间引擎一直在出帧;按总时长设线的话,跑得越认真
    # 越先被杀掉。真卡住的仍会在无声这么久之后收口,判据没有放宽。
    TURN_TIMEOUT = 900
    # 与 Runner 的 soft-stall 窗口一致。短静默仍是普通执行，不能仅凭没有新帧
    # 推断模型或连接异常；到期也只投影可恢复告警，不授予终态。
    STALL_SECONDS = TURN_TIMEOUT

    use_stdin_prompt = False   # True:提示词经 stdin 协议写入(双向 stream-json,支持权限询问)
    fork_flag = None           # 引擎侧「从被 resume 的会话另开一支」的标志;None = 该引擎不支持分支
    rewind_supported = False   # 是否支持回退(文件与对话回到某条用户消息之前);界面按此显隐按钮
    streams_text = False       # 字符级 delta;False = 整块出字,界面不显示流式光标
    asks_permission = False    # 有权限询问回路;False 时越权动作直接失败,不会停在无人应答的问句上
    PERMISSION_TIMEOUT = 300   # 权限询问无人响应自动拒绝(fail-closed),各引擎同一口径
    persist_supported = False  # 进程可跨回合常驻。两个前提:stdin 收得下一回合一帧;输出里划得出回合边界
    search_supported = True    # 会话正文搜索契约；具体引擎可改读自己的完整原始记录
    durable_delivery = True    # 普通 GUI 回合使用持久派发回执；Shepherd 使用更严格的自有账本

    def ordinary_storage_runtime(self):
        """Staged private-profile bridge, explicitly requested by an ordinary caller.

        This does not change existing dispatch/search/list behavior. The factory
        must not enable private launches until all read, auth and recovery paths
        use the same runtime. Hidden Shepherd workers use their own scopes.
        """
        if (self.name not in ('claude', 'codex', 'pi')
                or getattr(self, 'owner', self) is not self):
            from engine_profiles import ProfileError
            raise ProfileError('ordinary_profile_requires_ordinary_adapter')
        import os
        import threading
        from ordinary_engine_runtime import OrdinaryEngineRuntime
        lock = self.__dict__.setdefault('_ordinary_profile_lock', threading.Lock())
        with lock:
            runtime = self.__dict__.get('_ordinary_profile_runtime')
            if runtime is None:
                data_home = os.path.dirname(os.path.dirname(self.dir))
                runtime = OrdinaryEngineRuntime(data_home, self.name)
                self._ordinary_profile_runtime = runtime
            runtime.profile.validate(runtime.binding)
            return runtime

    def search_source_path(self, sid, record):
        """返回搜索索引的权威源文件；基类退回 Desk 的契约消息投影。"""
        return self._msg_path(sid)

    def search_documents(self, sid, record, path):
        """把权威源投影为可搜索的 user/assistant 正文与回合序号。"""
        from session_search import projection_documents
        return projection_documents(self._msg_path(sid))

    # ---- 界面契约 ----
    # 「模式」这一档在各引擎里叫的键不同:claude 的权限档存 mode,codex 家族的协作档存
    # collaboration_mode。前端原来按引擎名硬判(hsIsCodexLike),接一家新引擎就得回来改
    # 三处判断。改成引擎自报之后,前端只读 options_catalog().ui,不认引擎名。
    UI_MODE_KEY = "mode"
    UI_MODE_DEFAULT = ""       # 会话没选过时按哪一档显示;没有模式的引擎留空

    # Desk 只统一「下一轮能不能安全开始」这层语义，不统一各引擎的中断协议。
    # Claude 可由 result/进程边界确认，Codex 家族由 app-server 回合边界确认；前端与队列
    # 只认这三个状态，不再推测某个引擎的 writer、PTY 或 JSON-RPC 生命周期。
    TURN_RUNNING = "running"
    TURN_CANCELLING = "cancelling"
    TURN_READY = "ready"
