"""引擎接入抽象:描述符与共用能力位。

一个引擎一个描述符 EngineSpec:身份(名字/图标/强调色/一句说明)、可用性、凭证形态,
外加落盘后端 SessionStore(列会话、取标题、按 id 定位、删记录)。设置页与引擎切换菜单
读的是它,前端因此不必为每个引擎硬编码一份常量。

TUI 2026-08-20 退场后,这里不再有启动相关的东西:argv 拼装、环境隔离、准入闸 provision、
id 策略、工作态信号,连同 _term_open 一起撤了。会话由适配器(engine_adapters)按回合
起进程,工作目录与可写根在那一侧按会话算。
"""
import enum


class SettingsShape(enum.Enum):
    """设置页凭证卡形态(前端按此渲染,经 manifest 下发)。"""
    LOGIN_ONLY = "login_only"           # 仅登录态 + 登录入口
    GATEWAY = "gateway"                 # claude/codex:登录/API key + 网关 baseURL
    MODEL_LIST = "model_list"           # 登录 + 模型清单


# ---- 落盘后端:SessionStore 各族的共同接口 ----

class SessionStore:
    """会话持久化后端。各引擎收敛成两族实现(JsonlDirStore / RolloutGlobStore)。

    读的是引擎自己的记录,不是工作台的存储——它同时服务两件事:侧栏列出工作台开过的会话,
    以及把用户在命令行里跑过的那段会话认领进来(/session/adopt)。
    """

    def list_all(self):
        raise NotImplementedError

    def list_by_cwd(self, wd):
        raise NotImplementedError

    def title(self, sid):
        raise NotImplementedError

    def cwd_of(self, sid):
        raise NotImplementedError

    def owns(self, sid):
        raise NotImplementedError

    def delete(self, sid):
        raise NotImplementedError


# ---- 引擎描述符 ----

class EngineSpec:
    """一个引擎的接入契约。子类填静态字段,并实现 available / credentials。"""
    # 身份
    key = ""
    bin = ""
    display = ""
    icon = ""
    accent = ""
    desc = ""

    settings_shape = SettingsShape.LOGIN_ONLY

    store: SessionStore = None

    def available(self):
        raise NotImplementedError

    def credentials(self):
        raise NotImplementedError

    def manifest(self):
        return {
            "key": self.key,
            "display": self.display,
            "icon": self.icon,
            "accent": self.accent,
            "desc": self.desc,
            "settingsShape": self.settings_shape.value,
        }
