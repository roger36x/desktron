"""引擎注册表(第 1 步骨架)。

新增引擎 = 加一个 engines/<key>.py 描述符 + 在此注册一行。设置页(settings._settings_get)、
引擎切换菜单(system/api.py)、会话归属判定(term._resolve_uuid_engine)一律遍历 ENGINES,
不另立第二张手工同步的表——漏一处的表现是「图标出来了、菜单里却切不过去」。
"""
from engines.base import EngineSpec, SessionStore, SettingsShape
from engines.claude import SPEC as CLAUDE
from engines.codex import SPEC as CODEX
from engines.pi import SPEC as PI

# cursor-agent 于 2026-08-05 退役:只是个 TUI 壳,维护面(隔离 HOME、准入闸、transcript 解析、
# 设置页凭证卡)与收益不成正比,实际也没人用。
# pi 于 2026-08-20 摘掉过旧的一次性 JSON 接入；2026-08-28 按官方持久 RPC、权限回调、
# 会话树与 compact/clone 生命周期重新接入。
ENGINES = {s.key: s for s in (CLAUDE, CODEX, PI)}


def get(key):
    """按 key 取引擎描述符(无则 None)。"""
    return ENGINES.get(key)


def manifest():
    """全引擎前端元数据,供 /workbench/engines 下发(第 4 步前端切此数据源)。"""
    return {k: s.manifest() for k, s in ENGINES.items()}


def _selfcheck():
    """骨架自检:key 唯一且与注册键一致、凭证卡形态是枚举、manifest 投影可序列化。导入即调。"""
    seen = set()
    for k, s in ENGINES.items():
        assert s.key == k, f"key 不一致: {k} != {s.key}"
        assert k not in seen, f"重复 key: {k}"
        seen.add(k)
        assert isinstance(s.settings_shape, SettingsShape), k
        m = s.manifest()
        assert m["key"] == k and "settingsShape" in m, k


_selfcheck()
