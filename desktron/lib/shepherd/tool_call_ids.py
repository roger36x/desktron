"""Target-bound tool call identity normalization.

The verified public ledger keeps the source engine's identity. Native targets get
protocol-safe aliases only at their projection boundary; calls and outputs share
the same deterministic alias and distinct source IDs may never collapse.
"""
import hashlib
import re

_SAFE_ID = re.compile(r'^[A-Za-z0-9_-]+$')
HISTORY_PROJECTION_VERSIONS = {
    'pi': 'pi-history-v2',
    'codex': 'codex-family-history-v2',
}


class ToolCallIdError(ValueError):
    pass


def history_projection_version(engine):
    return HISTORY_PROJECTION_VERSIONS.get(str(engine or ''), '')


def protocol_call_id(call_id, *, max_length, prefix='call_'):
    """Return a stable ASCII alias when *call_id* violates a target contract."""
    value = str(call_id or '')
    if not value:
        raise ToolCallIdError('工具调用 ID 缺失')
    if len(value) <= max_length and _SAFE_ID.fullmatch(value):
        return value
    if not isinstance(max_length, int) or max_length <= len(prefix):
        raise ToolCallIdError('工具调用 ID 长度约束无效')
    return prefix + hashlib.sha256(value.encode('utf-8')).hexdigest()[:max_length-len(prefix)]


def call_id_mapping(items, *, max_length, prefix='call_'):
    """Build a collision-checked source-to-native mapping for item dictionaries."""
    mapping, sources = {}, {}
    for item in items:
        if not isinstance(item, dict):
            raise ToolCallIdError('历史项无效')
        if 'call_id' not in item:
            continue
        source = str(item.get('call_id') or '')
        native = protocol_call_id(source, max_length=max_length, prefix=prefix)
        previous = sources.get(native)
        if previous is not None and previous != source:
            raise ToolCallIdError('工具调用 ID 归一化冲突')
        sources[native] = source
        mapping[source] = native
    return mapping


def normalize_item_call_ids(items, *, max_length, prefix='call_'):
    """Copy only items whose call_id needs a protocol-safe alias."""
    mapping = call_id_mapping(items, max_length=max_length, prefix=prefix)
    return [({**item, 'call_id': mapping[str(item.get('call_id') or '')]}
             if 'call_id' in item and mapping[str(item.get('call_id') or '')] != str(item.get('call_id') or '')
             else item) for item in items]
