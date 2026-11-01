"""codex 引擎描述符:身份 / 可用性 / 凭证。会话记录是 ~/.codex/sessions 下的 rollout jsonl。

启动相关的东西(argv、CODEX_HOME 隔离与准入闸 provision)2026-08-20 随 TUI 一起搬走:
GUI 是唯一入口,隔离在 CodexAdapter.spawn_env 里做(仍经 codexgate.ensure_codex_home)。
"""
import shutil

from engines.base import EngineSpec, SettingsShape
from engines.stores import RolloutGlobStore


class CodexSpec(EngineSpec):
    key = "codex"
    bin = "codex"
    display = "Codex"
    icon = "code"
    accent = "rgba(16,163,127,.75)"
    desc = "本机登录（codex login）,OpenAI 模型"

    settings_shape = SettingsShape.GATEWAY       # 登录 / API key + OpenAI 兼容网关
    def available(self):
        return bool(shutil.which(self.bin))

    def credentials(self):
        import settings
        return settings._gateway_section(("logged_in", settings._codex_logged_in),
                                         "OPENAI_API_KEY", "OPENAI_BASE_URL")


SPEC = CodexSpec()
SPEC.store = RolloutGlobStore(engine="codex", rollout_dir="~/.codex/sessions")
