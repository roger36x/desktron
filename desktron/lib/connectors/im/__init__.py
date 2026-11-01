"""办公 IM 接入契约(IMSpec):一个供应商一个描述符,与 engines/EngineSpec 同构。

能力枚举表达真实差异,不是摆设——换供应商时按声明降级,领域代码不认供应商名字:
- 线程语义:飞书有话题群(1 话题 ↔ 1 会话)、Slack 有 thread、企微没有对应物,
  无线程的降级为一群一会话(remote 域的会话绑定模型按此分支)。
- 拉取方式:轮询官方 CLI vs webhook 推送。
- 可采集面:群消息/会议/文档三样不是每家都有(Slack 无文档流,Zoom 只有会议),
  事件类型(incident/meet/doc/chat)按声明裁剪。
- 身份:用户身份 vs bot 身份(飞书 vc +search 等接口硬性要求 user 身份)。

同款注册表:import 即注册,get() 按 key 取(缺省 lark,当前唯一实现)。
"""
import enum
import os
from dataclasses import dataclass


class ThreadModel(enum.Enum):
    """线程/子会话语义:远程入口(remote 域)的会话绑定模型按此降级。"""
    TOPIC_GROUP = "topic_group"   # 飞书:话题群,1 话题 ↔ 1 会话(现行绑定模型)
    THREAD = "thread"             # Slack 式消息 thread
    NONE = "none"                 # 企微等:无对应物,降级一群一会话


class PullMode(enum.Enum):
    """信息流(feed 域)取数方式。"""
    POLL_CLI = "poll_cli"         # 轮询官方 CLI(现行:sidecar 三线程 tick)
    WEBHOOK = "webhook"           # 事件推送(未接)


class Surface(enum.Enum):
    """可采集面:事件类型按 connector 声明裁剪。"""
    CHAT = "chat"                 # 群消息
    MEETING = "meeting"           # 会议(纪要/妙记)
    DOC = "doc"                   # 文档分享/变更


class Identity(enum.Enum):
    """鉴权身份。"""
    USER = "user"                 # 用户身份
    BOT = "bot"                   # 应用/机器人身份


@dataclass(frozen=True)
class IMSpec:
    """一个 IM 供应商一个描述符。impl 是供应商实现模块,收敛四类接口面:
    CLI 采集器(*_cached 族)/ 深链构造(chat_link、doc_link*)/ 同步池布局(pool_dir)/
    供应商缓存文件。领域代码经 get().impl 触达,不直接 import 供应商模块。"""
    key: str                      # 注册名(lark / wecom / slack …)
    name: str                     # 展示名
    thread_model: ThreadModel
    pull: PullMode
    surfaces: frozenset           # {Surface, ...}
    identity: Identity
    impl: object                  # 供应商实现模块


_REGISTRY = {}


def register(spec):
    _REGISTRY[spec.key] = spec


def get(key=None):
    """按 key 取描述符;缺省取 DESKTRON_IM 环境变量,再缺省 lark(当前唯一实现)。"""
    return _REGISTRY.get(key or os.environ.get("DESKTRON_IM", "lark"))


from connectors.im import lark as _lark   # noqa: E402  import 即注册(engines/ 同款)
register(_lark.SPEC)
