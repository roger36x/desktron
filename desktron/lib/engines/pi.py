"""Pi Coding Agent 描述符（@earendil-works/pi-coding-agent，MIT）。"""
import shutil

from engines.base import EngineSpec, SettingsShape
from engines.stores import PiSessionStore, pi_sessions_dir_name


# 保留可直接测试/复用的名字；编码规则的正身在 stores.py，列会话与适配器共用。
sessions_dir_name = pi_sessions_dir_name


class PiSpec(EngineSpec):
    key = "pi"
    bin = "pi"
    display = "Pi Coding"
    icon = "terminal"
    accent = "rgba(232,232,234,.55)"
    desc = "开源多 provider（MIT），支持本机与 API 凭证"

    settings_shape = SettingsShape.MODEL_LIST

    def available(self):
        return bool(shutil.which(self.bin))

    def credentials(self):
        import settings
        return settings._pi_credentials()


SPEC = PiSpec()
SPEC.store = PiSessionStore()
