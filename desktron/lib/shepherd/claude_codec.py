"""Version-gated projection into Claude Code 2.1.268 transcript entries."""
import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from .image_content import has_images, native_content
from .source_codec import reasoning_text

CLAUDE_CODE_VERSION = '2.1.268'
SDK_VERSION = '0.3.268'
CODEC_VERSION = 'claude-2.1.268-v1'
PROMPT_SNAPSHOT_POLICY = 'claude-static-v1'
BASH_CWD_PROMPT = ('每次 Bash 调用都从当前项目目录开始，`cd` 只影响本次调用；'
                   '后续命令仍需使用同一位置时，请写绝对路径。')
_UUID_NAMESPACE = uuid.UUID('c7f9df3c-bd70-4da5-b41d-825b113ac2a3')


def static_system_prompt(system_text):
    """Compose the immutable Claude Binding prompt for the pinned codec."""
    return '\n\n'.join(part for part in (str(system_text or ''), BASH_CWD_PROMPT) if part)


class CodecError(ValueError):
    pass


def _uuid(binding_id, identity):
    return str(uuid.uuid5(_UUID_NAMESPACE, binding_id + ':' + identity))


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise CodecError('Claude 公共消息 content 无效')
    out = []
    for block in content:
        if not isinstance(block, dict) or block.get('type') not in ('input_text', 'output_text', 'text'):
            raise CodecError('Claude 公共消息包含不支持的内容')
        out.append(str(block.get('text') or ''))
    return '\n'.join(out)


def _tool_id(call_id):
    return 'toolu_' + hashlib.sha256(str(call_id).encode()).hexdigest()[:24]


def _timestamp(value):
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat().replace('+00:00', 'Z')
    except (TypeError, ValueError, OverflowError):
        return '2026-01-01T00:00:00.000Z'


def _messages(public, *, semantics=True):
    if not isinstance(public, list):
        raise CodecError('Claude 公共投影必须是数组')
    messages, calls, pending, native_sources = [], {}, set(), {}
    def append(role, block):
        if messages and messages[-1]['role'] == role:
            content = messages[-1]['content']
            if block.get('type') == 'tool_result':
                # The API requires every tool_result to precede other blocks in the
                # same user message. Parallel image reads project as
                # [result, "[Image: ...]", result, "[Image: ...]"]; interleaving them
                # makes Claude reject the whole history ("tool use concurrency issues",
                # 2026-10-02 adp_6c287a7752584a). Keep results first, other blocks
                # after them, each group in original order.
                at = next((i for i, item in enumerate(content)
                           if item.get('type') != 'tool_result'), len(content))
                content.insert(at, block)
            else:
                content.append(block)
        else:
            messages.append({'role': role, 'content': [block]})
    for item in public:
        if not isinstance(item, dict):
            raise CodecError('Claude 公共投影项无效')
        kind = item.get('type')
        if kind == 'message':
            role = item.get('role')
            if role not in ('user', 'assistant'):
                raise CodecError('Claude 不接受公共系统消息')
            if has_images(item.get('content')):
                if role != 'user':
                    raise CodecError('Claude 不接受非用户图片')
                for block in native_content(item['content'], 'claude'):
                    append(role, block)
            else:
                append(role, {'type': 'text', 'text': _text(item.get('content'))})
        elif kind == 'reasoning':
            summary = reasoning_text(item)
            if summary:
                block = {'type': 'text', 'text': summary}
                if semantics: block['_shepherd_semantics'] = {'kind':'reasoning'}
                append('assistant', block)
        elif kind in ('function_call', 'custom_tool_call'):
            call_id = str(item.get('call_id') or '')
            if not call_id or call_id in calls:
                raise CodecError('Claude 工具调用 ID 缺失或重复')
            raw = item.get('arguments') if kind == 'function_call' else item.get('input')
            if kind == 'custom_tool_call' and semantics:
                if not isinstance(raw, str):
                    raise CodecError('Claude custom tool 输入必须是原始字符串')
                arguments = {'input':raw}
            else:
                try: arguments = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
                except (TypeError, ValueError) as exc: raise CodecError('Claude 工具参数不是 JSON') from exc
            if not isinstance(arguments, dict):
                raise CodecError('Claude 工具参数必须是对象')
            name = re.sub(r'[^A-Za-z0-9_-]', '_', str(item.get('name') or ''))[:128]
            if not name:
                raise CodecError('Claude 工具名为空')
            native_id = _tool_id(call_id)
            owner = native_sources.get(native_id)
            if owner is not None and owner != call_id:
                raise CodecError('Claude 工具调用 ID 归一化冲突')
            native_sources[native_id] = call_id
            calls[call_id] = (kind, native_id); pending.add(call_id)
            append('assistant', {'type': 'tool_use', 'id': native_id,
                                 'name': name, 'input': arguments,
                                 '_shepherd_call_id': call_id})
            if semantics and (kind == 'custom_tool_call' or name != item.get('name')):
                messages[-1]['content'][-1]['_shepherd_semantics'] = {
                    'kind':kind, 'name':item['name']}
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            call_id = str(item.get('call_id') or '')
            if call_id not in pending:
                raise CodecError('Claude 工具结果缺少对应调用')
            if kind != calls[call_id][0] + '_output':
                raise CodecError('Claude 工具结果类型与调用不匹配')
            pending.remove(call_id)
            if not isinstance(item.get('output'), (str, list)):
                raise CodecError('Claude 工具结果仅支持文本或图片块，未转换的结构化结果已保留')
            multimodal = has_images(item.get('output'))
            output = (native_content(item['output'], 'claude')
                      if multimodal else _text(item['output']))
            failed = (any(str(block.get('text') or '').startswith(
                              ('[用户中断]', '[工具执行失败]'))
                          for block in output if isinstance(block, dict))
                      if multimodal else output.startswith(('[用户中断]', '[工具执行失败]')))
            append('user', {'type': 'tool_result', 'tool_use_id': calls[call_id][1],
                            'content': output,
                            'is_error': failed,
                            '_shepherd_call_id': call_id})
        else:
            raise CodecError('Claude 不支持公共投影项 ' + str(kind))
    if pending:
        raise CodecError('Claude 工具结果尚未完整配对')
    return messages


def encode_rounds(binding, rounds, parent_uuid=None, *, legacy_rounds=()):
    entries = []
    session_id = binding['native_session_id']
    cwd = binding['native_cwd']
    binding_id = binding['binding_id']
    for row in rounds:
        round_id = str(row.get('round_id') or '')
        for index, message in enumerate(_messages(row.get('public'), semantics=round_id not in legacy_rounds)):
            entry_uuid = _uuid(binding_id, f'{round_id}:{index}')
            # _messages allocates these messages and owns their mutable blocks.
            body = message
            call_ids = {}
            hints = {}
            for block_index, block in enumerate(body.get('content') or []):
                hint = block.pop('_shepherd_semantics', None)
                if hint: hints[str(block_index)] = hint
                original = block.pop('_shepherd_call_id', None)
                native = block.get('id') or block.get('tool_use_id')
                if original and native:
                    call_ids[str(native)] = str(original)
            if body['role'] == 'assistant':
                body.update(id='msg_' + entry_uuid.replace('-', ''), type='message',
                            model='shepherd-import',
                            stop_reason=('tool_use' if any(p['type'] == 'tool_use' for p in body['content']) else 'end_turn'),
                            stop_sequence=None, usage={'input_tokens': 0, 'output_tokens': 0})
            entries.append({'type': body['role'], 'message': body, 'uuid': entry_uuid,
                            'parentUuid': parent_uuid, 'sessionId': session_id, 'cwd': cwd,
                            'isSidechain': False, 'userType': 'external',
                            'entrypoint': 'sdk-ts', 'version': CLAUDE_CODE_VERSION,
                            'timestamp': _timestamp(row.get('committed_at')),
                            'shepherd': {'round_id': round_id, 'offset': row.get('offset'),
                                         'entry_hash': row.get('entry_hash'), 'codec': CODEC_VERSION,
                                         'call_ids': call_ids}})
            if hints:
                entries[-1]['shepherd']['semantics_v2'] = hints
            parent_uuid = entry_uuid
    return entries


def encode_checkpoint(binding, checkpoint, public, parent_uuid=None):
    summary = public.get('summary') if isinstance(public, dict) else None
    if not isinstance(summary, str) or not summary.strip():
        raise CodecError('Claude Portable Checkpoint 缺少摘要')
    binding_id = binding['binding_id']; session_id = binding['native_session_id']; cwd = binding['native_cwd']
    checkpoint_id = checkpoint['checkpoint_id']
    boundary_uuid = _uuid(binding_id, checkpoint_id + ':boundary')
    summary_uuid = _uuid(binding_id, checkpoint_id + ':summary')
    timestamp = _timestamp(checkpoint.get('created_at'))
    common = {'sessionId': session_id, 'cwd': cwd, 'isSidechain': False,
              'userType': 'external', 'entrypoint': 'sdk-ts',
              'version': CLAUDE_CODE_VERSION, 'timestamp': timestamp}
    boundary = {**common, 'type': 'system', 'subtype': 'compact_boundary',
                'content': 'Conversation compacted', 'level': 'info',
                'uuid': boundary_uuid, 'parentUuid': None, 'logicalParentUuid': parent_uuid,
                'compactMetadata': {'trigger': 'auto', 'preTokens': 0,
                    'postTokens': checkpoint['estimated_tokens'], 'cumulativeDroppedTokens': 0,
                    'durationMs': 0},
                'shepherd': {'checkpoint_id': checkpoint_id,
                             'covers_through_offset': checkpoint['covers_through_offset'],
                             'codec': CODEC_VERSION}}
    text = ('This session is being continued from a prior Desk conversation. '
            'The summary below is a historical checkpoint, not a new user request.\n\n' + summary.strip())
    summary_entry = {**common, 'type': 'user', 'uuid': summary_uuid,
                     'parentUuid': boundary_uuid, 'promptId': _uuid(binding_id, checkpoint_id + ':prompt'),
                     'message': {'role': 'user', 'content': text},
                     'isVisibleInTranscriptOnly': True, 'isCompactSummary': True,
                     'shepherd': {'checkpoint_id': checkpoint_id,
                                  'covers_through_offset': checkpoint['covers_through_offset'],
                                  'codec': CODEC_VERSION}}
    return [boundary, summary_entry]
