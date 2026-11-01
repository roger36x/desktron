"""claude 引擎描述符:身份 / 可用性 / 凭证。会话记录在 ~/.claude/projects/<编码 cwd>/<id>.jsonl。"""
import shutil

from engines.base import EngineSpec, SettingsShape
from engines.stores import JsonlDirStore


class ClaudeSpec(EngineSpec):
    key = "claude"
    bin = "claude"
    display = "Claude"
    icon = "layers"
    accent = "rgba(217,119,87,.7)"
    desc = "本机订阅（claude auth login）,Anthropic 模型"

    settings_shape = SettingsShape.GATEWAY       # 登录 / API key + Anthropic 兼容网关

    def available(self):
        return bool(shutil.which(self.bin))

    def credentials(self):
        import settings
        return settings._gateway_section(("subscription", settings._claude_subscription),
                                         "ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL")


SPEC = ClaudeSpec()
SPEC.store = JsonlDirStore(variant="claude")
