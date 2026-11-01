"""Decode Shepherd-marked native records into one canonical public round.

These decoders are deliberately narrow: they only accept shapes emitted by the
version-gated Shepherd connectors. Unmarked user sessions are never inferred or
adopted here.
"""
import copy
import hashlib
import json

from .store import InvalidState
from .image_content import has_images, public_content


def _text(content, *, version=1):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise InvalidState('Shepherd native text content 无效')
    if any(not isinstance(block, dict) or block.get('type') not in
           ('text', 'input_text', 'output_text', 'summary_text') for block in content):
        raise InvalidState('Shepherd native text 包含未支持的内容，不能静默省略')
    return '\n'.join(str(block.get('text') or '') for block in content
                     if isinstance(block, dict)
                     and (block.get('type') in ('text', 'input_text', 'output_text')
                          or version >= 2 and block.get('type') == 'summary_text'))


def _error_text(text, failed, version):
    if version >= 2 and failed and not text.startswith(('[工具执行失败]', '[用户中断]')):
        return '[工具执行失败] ' + text
    return text


def _tool_output(content, engine, *, failed=False, version=1):
    """Preserve image bytes in tool results without drifting text-only hashes."""
    if engine == 'claude' and version >= 2 and isinstance(content, list):
        # ToolSearch returns references, not tool calls or tool definitions.
        # Keep their names as inert, portable result text. Original typed blocks
        # remain in the immutable source; importing history must not activate tools.
        converted = None
        for index, block in enumerate(content):
            if not isinstance(block, dict) or block.get('type') != 'tool_reference':
                continue
            if (not {'type', 'tool_name'}.issubset(block)
                    or not isinstance(block.get('tool_name'), str)
                    or not block['tool_name'].strip()):
                raise InvalidState('Claude tool_reference 格式无效')
            if converted is None:
                converted = list(content)
            converted[index] = {'type': 'text', 'text': '[Claude tool_reference] ' +
                                json.dumps(block, ensure_ascii=False, sort_keys=True,
                                           separators=(',', ':'))}
        if converted is not None:
            content = converted
    native_image = 'image'
    if has_images(content, native_image):
        output = public_content(content, engine)
        if version >= 2 and failed:
            text = next((str(block.get('text') or '') for block in output
                         if block.get('type') == 'input_text'), '')
            if not text.startswith(('[工具执行失败]', '[用户中断]')):
                output.insert(0, {'type':'input_text', 'text':'[工具执行失败]'})
        return output
    return _error_text(_text(content, version=version), failed, version)


def reasoning_text(item):
    summary = item.get('summary') or []
    if not isinstance(summary, list) or any(not isinstance(part, dict)
            or part.get('type') not in ('summary_text', 'text')
            or not isinstance(part.get('text'), str) for part in summary):
        raise InvalidState('Shepherd reasoning 内容不可无损编码')
    return '\n'.join(part['text'] for part in summary if part['text'])


def _message(role, text):
    if not text:
        return None
    block = 'input_text' if role == 'user' else 'output_text'
    return {'type': 'message', 'role': role,
            'content': [{'type': block, 'text': text}]}


def claude_public(entries, *, version=1, grounding_uuid='', grounding_sha256=''):
    out = []
    call_ids = {}
    tool_hints = {}
    for entry in entries:
        # Connector validation owns the version-gated internal-record allowlist.
        # Public projection must not inspect those records as messages: Claude
        # informational metadata can carry a scalar `message` field.
        if not isinstance(entry, dict) or entry.get('type') not in ('user', 'assistant'):
            continue
        mapping = (entry.get('shepherd') or {}).get('call_ids') or {}
        if not isinstance(mapping, dict):
            raise InvalidState('Claude source call_id 映射无效')
        for native_id, public_id in mapping.items():
            native_id, public_id = str(native_id or ''), str(public_id or '')
            if not native_id or not public_id or (native_id in call_ids and call_ids[native_id] != public_id):
                raise InvalidState('Claude source call_id 映射冲突')
            call_ids[native_id] = public_id
        hints = (entry.get('shepherd') or {}).get('semantics_v2') or {}
        for index, block in enumerate((entry.get('message') or {}).get('content') or []):
            if isinstance(block, dict) and block.get('type') == 'tool_use' and str(index) in hints:
                tool_hints[block['id']] = hints[str(index)]
    for entry in entries:
        if not isinstance(entry, dict) or entry.get('type') not in ('user', 'assistant'):
            continue
        # Claude's native compaction summary is context replacement metadata,
        # not a new user request. Its bytes stay in the hashed source but never
        # enter the canonical public conversation as a fake user turn.
        if entry.get('type') == 'user' and entry.get('isCompactSummary') is True:
            continue
        body = entry.get('message') or {}
        role = body.get('role')
        if role not in ('user', 'assistant'):
            raise InvalidState('Claude source role 无效')
        content = body.get('content')
        blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}]
        if grounding_uuid and entry.get('uuid') == grounding_uuid:
            first = blocks[0] if blocks else None
            text = first.get('text') if isinstance(first, dict) else None
            if (not grounding_sha256 or not isinstance(text, str)
                    or hashlib.sha256(text.encode()).hexdigest() != grounding_sha256):
                raise InvalidState('Claude Grounding 投影认证无效')
            blocks = blocks[1:]
        if has_images(blocks, 'image'):
            if role != 'user':
                raise InvalidState('Claude 非用户图片不可投影')
            out.append({'type': 'message', 'role': role,
                        'content': public_content(blocks, 'claude')})
            continue
        hints = (entry.get('shepherd') or {}).get('semantics_v2') or {}
        for block_index, block in enumerate(blocks):
            if not isinstance(block, dict):
                raise InvalidState('Claude source block 无效')
            kind = block.get('type')
            if kind == 'text':
                if (hints.get(str(block_index)) or {}).get('kind') == 'reasoning':
                    out.append({'type':'reasoning', 'summary':[{'type':'summary_text', 'text':str(block.get('text') or '')}]})
                    continue
                item = _message(role, str(block.get('text') or ''))
                if item: out.append(item)
            elif kind == 'thinking':
                if block.get('thinking'):
                    out.append({'type': 'reasoning', 'summary': [
                        {'type': 'summary_text', 'text': str(block['thinking'])}]})
            elif kind == 'redacted_thinking' and role == 'assistant' and version >= 2:
                if not isinstance(block.get('data'), str):
                    raise InvalidState('Claude redacted_thinking data invalid')
                out.append({'type': 'reasoning', 'summary': [
                    {'type': 'summary_text', 'text': '[原生思考内容不可见；原始记录已保留]'}]})
            elif kind == 'tool_use' and role == 'assistant':
                arguments = block.get('input') or {}
                if not isinstance(arguments, dict):
                    raise InvalidState('Claude source tool input 无效')
                native_id = str(block.get('id') or '')
                hint = tool_hints.get(native_id) or {}
                if hint.get('kind') == 'custom_tool_call':
                    if set(arguments) != {'input'} or not isinstance(arguments['input'], str):
                        raise InvalidState('Claude custom tool 原始输入缺失')
                    out.append({'type':'custom_tool_call', 'call_id':str(call_ids.get(native_id) or native_id),
                                'name':hint['name'], 'input':arguments['input']})
                    continue
                out.append({'type': 'function_call',
                            'call_id': str(call_ids.get(native_id) or native_id),
                            'name': str(hint.get('name') or block.get('name') or ''),
                            'arguments': json.dumps(arguments, ensure_ascii=False,
                                                    sort_keys=True, separators=(',', ':'))})
            elif kind == 'tool_result' and role == 'user':
                native_id = str(block.get('tool_use_id') or '')
                out.append({'type': 'custom_tool_call_output' if (tool_hints.get(native_id) or {}).get('kind') == 'custom_tool_call' else 'function_call_output',
                            'call_id': str(call_ids.get(native_id) or native_id),
                            'output': _tool_output(block.get('content'), 'claude',
                                                   failed=block.get('is_error'), version=version)})
            else:
                raise InvalidState('Claude source block 不可投影')
    return out


PI_PROJECTION_VERSION = 4


def validate_pi_system_message(message):
    """Pi 0.86 runtime instructions remain in source, not public chat history.

    This adds a previously rejected role without changing any successful old
    projection. Desk regenerates instructions for the destination engine.
    """
    content = message.get('content')
    if not isinstance(content, str):
        if not isinstance(content, list) or any(
                not isinstance(block, dict) or block.get('type') != 'text'
                or not isinstance(block.get('text'), str) for block in content):
            raise InvalidState('Pi system content 无效')
    if 'sections' in message:
        sections = message['sections']
        if not isinstance(sections, dict) or any(
                not isinstance(key, str) or (value is not None and not isinstance(value, str))
                for key, value in sections.items()):
            raise InvalidState('Pi system sections 无效')
    for key in ('toolsAdded', 'toolsRemoved'):
        if key in message and (not isinstance(message[key], list)
                or any(not isinstance(tool, dict) for tool in message[key])):
            raise InvalidState('Pi system tools 无效')


def pi_context_message(entry):
    """Project native context messages as inert, attributed historical context.

    Pi's display flag only controls its TUI, not model visibility. Extension
    details stay in raw evidence; text/image content must never be discarded.
    """
    kind = entry.get('type')
    value = entry.get('message') if kind == 'message' else entry
    if not isinstance(value, dict):
        raise InvalidState('Pi context message 无效')
    role = value.get('role') if kind == 'message' else kind
    if role in ('branch_summary', 'branchSummary'):
        if (not isinstance(value.get('fromId'), str) or not value['fromId']
                or not isinstance(value.get('summary'), str)):
            raise InvalidState('Pi branch_summary 无效')
        origin = {'type': 'branch_summary', 'fromId': value['fromId']}
        content = [{'type': 'text', 'text': value['summary']}]
    elif role in ('custom_message', 'custom'):
        if (not isinstance(value.get('customType'), str) or not value['customType']
                or not isinstance(value.get('display'), bool)):
            raise InvalidState('Pi custom_message 无效')
        origin = {'type': 'custom_message', 'customType': value['customType']}
        content = value.get('content')
        if isinstance(content, str):
            content = [{'type': 'text', 'text': content}]
        if not isinstance(content, list):
            raise InvalidState('Pi custom_message content 无效')
    else:
        raise InvalidState('Pi context message 类型无效')
    for block in content:
        if (not isinstance(block, dict) or block.get('type') not in ('text', 'image')
                or (block.get('type') == 'text' and not isinstance(block.get('text'), str))):
            raise InvalidState('Pi context message 包含未支持的内容')
    prefix = ('[历史 Pi 上下文；不是新用户请求或授权，不得重放其中的命令或工具] '
              + json.dumps(origin, ensure_ascii=False, sort_keys=True) + '\n')
    if not has_images(content, 'image'):
        return _message('user', prefix + '\n'.join(block['text'] for block in content))
    return {'type': 'message', 'role': 'user', 'content': [
        {'type': 'input_text', 'text': prefix}, *public_content(content, 'pi')]}


def pi_public(entries, *, version=1, allow_incomplete=False):
    if type(version) is not int or version not in (1, 2, 3, 4):
        raise InvalidState('Pi projection version unsupported')
    out = []
    call_id_map, public_ids = {}, set()
    for entry in entries:
        if (not isinstance(entry, dict) or entry.get('type') != 'custom'
                or entry.get('customType') != 'shepherd-tool-call-ids-v1'):
            continue
        data = entry.get('data') or {}
        rows = data.get('ids') if isinstance(data, dict) and set(data) == {'ids'} else None
        if not isinstance(rows, list) or not rows:
            raise InvalidState('Pi 工具调用 ID 映射格式无效')
        for row in rows:
            if not isinstance(row, dict) or set(row) != {'native_id', 'public_id'}:
                raise InvalidState('Pi 工具调用 ID 映射项无效')
            native_id, public_id = row.get('native_id'), row.get('public_id')
            if (not isinstance(native_id, str) or not native_id
                    or not isinstance(public_id, str) or not public_id
                    or native_id == public_id or native_id in call_id_map
                    or public_id in public_ids):
                raise InvalidState('Pi 工具调用 ID 映射冲突')
            call_id_map[native_id] = public_id
            public_ids.add(public_id)
    custom, calls, results = {}, set(), set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict) or entry.get('type') != 'custom' or entry.get('customType') != 'shepherd-custom-tool-v1':
            continue
        data = entry.get('data') or {}
        if not isinstance(data, dict):
            raise InvalidState('Pi custom tool 来源标记格式无效')
        call_id, name = data.get('call_id'), data.get('name')
        following = entries[index + 1] if index + 1 < len(entries) else {}
        if not isinstance(following, dict):
            raise InvalidState('Pi custom tool 后继消息无效')
        message = following.get('message') or {}
        if not isinstance(message, dict) or not isinstance(message.get('content'), list):
            raise InvalidState('Pi custom tool 后继内容无效')
        matches = [b for b in message.get('content') or [] if isinstance(b, dict)
                   and b.get('type') == 'toolCall' and b.get('id') == call_id]
        if (not isinstance(call_id, str) or not call_id or call_id in custom
                or not isinstance(name, str) or not name or following.get('type') != 'message'
                or message.get('role') != 'assistant' or len(matches) != 1
                or matches[0].get('name') != name):
            raise InvalidState('Pi custom tool 来源标记无效')
        args = matches[0].get('arguments')
        if not isinstance(args, dict) or set(args) != {'input'} or not isinstance(args['input'], str):
            raise InvalidState('Pi custom tool 原始输入缺失')
        custom[call_id] = name
    reasoning_next = False
    native_calls, native_results, projected_calls = set(), set(), set()
    undispatched = set()
    for entry in entries:
        if (isinstance(entry, dict) and entry.get('customType') == 'shepherd-reasoning-v2'
                and (version < 4 or entry.get('type') == 'custom')):
            if reasoning_next: raise InvalidState('Pi reasoning 来源标记重复')
            reasoning_next = True
            continue
        if version >= 4 and isinstance(entry, dict) and (
                entry.get('type') in ('branch_summary', 'custom_message')
                or (entry.get('type') == 'message' and isinstance(entry.get('message'), dict)
                    and entry['message'].get('role') in ('branchSummary', 'custom'))):
            if reasoning_next:
                raise InvalidState('Pi reasoning 来源标记不匹配')
            out.append(pi_context_message(entry))
            continue
        if not isinstance(entry, dict) or entry.get('type') != 'message':
            continue
        message = entry.get('message') or {}
        role = message.get('role')
        if reasoning_next:
            if role != 'assistant' or any(b.get('type') != 'text' for b in message.get('content') or []):
                raise InvalidState('Pi reasoning 来源标记不匹配')
            out.append({'type':'reasoning','summary':[{'type':'summary_text','text':_text(message.get('content'))}]})
            reasoning_next = False
            continue
        if role == 'system':
            validate_pi_system_message(message)
            continue
        if role == 'toolResult':
            native_id = str(message.get('toolCallId') or '')
            if not native_id or native_id in native_results:
                raise InvalidState('Pi 工具结果 ID 缺失或重复')
            native_results.add(native_id)
            if native_id in custom:
                if native_id not in calls or message.get('toolName') != custom[native_id]:
                    raise InvalidState('Pi custom tool 结果标记不匹配')
                results.add(native_id)
            out.append({'type': ('custom_tool_call_output' if native_id in custom
                                 else 'function_call_output'),
                        'call_id': call_id_map.get(native_id, native_id),
                        'output': _tool_output(message.get('content'), 'pi',
                                               failed=message.get('isError'), version=version)})
            continue
        if role == 'bashExecution' and version >= 2:
            if (not isinstance(message.get('command'), str)
                    or not isinstance(message.get('output'), str)
                    or not isinstance(message.get('cancelled'), bool)
                    or not isinstance(message.get('truncated'), bool)
                    or ('excludeFromContext' in message and not isinstance(message['excludeFromContext'], bool))
                    or (message.get('exitCode') is not None and
                        (not isinstance(message['exitCode'], int) or isinstance(message['exitCode'], bool)))):
                raise InvalidState('Pi bashExecution payload invalid')
            if message.get('excludeFromContext') is not True:
                # It already ran. Do not fabricate a tool call, read a referenced
                # output file, or treat this as a fresh user command.
                evidence = {key: message[key] for key in (
                    'command', 'output', 'exitCode', 'cancelled', 'truncated', 'fullOutputPath')
                    if key in message}
                out.append(_message('assistant', '[历史 Bash 执行记录；不得自动重跑]\n' +
                                    json.dumps(evidence, ensure_ascii=False, sort_keys=True)))
            continue
        if role not in ('user', 'assistant'):
            raise InvalidState('Pi source role 无效')
        content = message.get('content')
        blocks = content if isinstance(content, list) else [{'type': 'text', 'text': content}]
        if has_images(blocks, 'image'):
            if role != 'user':
                raise InvalidState('Pi 非用户图片不可投影')
            out.append({'type': 'message', 'role': role,
                        'content': public_content(blocks, 'pi')})
            continue
        text = _text([block for block in blocks if isinstance(block, dict) and block.get('type') == 'text'])
        item = _message(role, text)
        if item: out.append(item)
        for block in blocks:
            if not isinstance(block, dict):
                raise InvalidState('Pi source block 无效')
            if block.get('type') == 'thinking' and block.get('thinking'):
                out.append({'type': 'reasoning', 'summary': [
                    {'type': 'summary_text', 'text': str(block['thinking'])}]})
            elif block.get('type') == 'toolCall':
                arguments = block.get('arguments') or {}
                if not isinstance(arguments, dict):
                    raise InvalidState('Pi source tool input 无效')
                native_id = str(block.get('id') or '')
                public_id = call_id_map.get(native_id, native_id)
                if (not native_id or native_id in native_calls
                        or not public_id or public_id in projected_calls):
                    raise InvalidState('Pi source tool 调用 ID 缺失、重复或映射冲突')
                native_calls.add(native_id); projected_calls.add(public_id)
                # Pi 0.85.1 exits agent-loop before tool dispatch when streaming
                # ends in error/aborted. Partial toolCall blocks are generation
                # evidence, not executed calls. V2 remains immutable for receipts.
                if version >= 3 and role == 'assistant' and message.get('stopReason') in ('error', 'aborted'):
                    undispatched.add(native_id)
                    if native_id in custom:
                        calls.add(native_id)
                    out.append(_message('assistant',
                        '[Pi 工具调用生成未完成，未派发；历史记录，不得自动重试] ' +
                        json.dumps({'stopReason': message['stopReason'], 'toolCall': block},
                                   ensure_ascii=False, sort_keys=True, separators=(',', ':'))))
                    continue
                if native_id in custom:
                    if native_id in calls or block.get('name') != custom[native_id]:
                        raise InvalidState('Pi custom tool 调用重复或不匹配')
                    calls.add(native_id)
                    out.append({'type':'custom_tool_call', 'call_id':public_id,
                                'name':custom[native_id], 'input':arguments['input']})
                    continue
                out.append({'type': 'function_call', 'call_id': public_id,
                            'name': str(block.get('name') or ''),
                            'arguments': json.dumps(arguments, ensure_ascii=False,
                                                    sort_keys=True, separators=(',', ':'))})
            elif block.get('type') not in ('text', 'thinking'):
                raise InvalidState('Pi source block 不可投影')
    if undispatched & native_results:
        raise InvalidState('Pi 未派发工具却存在执行结果')
    if calls != set(custom) or (not allow_incomplete and results != calls - undispatched):
        raise InvalidState('Pi custom tool 来源未完整配对')
    mapped_ids=set(call_id_map)
    if (not mapped_ids.issubset(native_calls)
            or (not allow_incomplete and not (mapped_ids - undispatched).issubset(native_results))):
        raise InvalidState('Pi 工具调用 ID 映射未完整配对')
    if reasoning_next: raise InvalidState('Pi reasoning 来源缺少内容')
    return out


UNKNOWN_TOOL_OUTPUT = '[执行状态未知] 本次执行意外终止，无法确认该工具是否完成；不得自动重试或假定成功/失败。'


def seal_incomplete_tools(public):
    """Close dangling calls for model history without claiming tool success.

    The native projection remains unchanged.  Synthetic outputs exist only in
    the canonical history projection and are reproduced from receipt metadata.
    """
    if not isinstance(public, list):
        raise InvalidState('Shepherd 公共投影必须是数组')
    calls, finished, order = {}, set(), []
    for item in public:
        if not isinstance(item, dict):
            raise InvalidState('Shepherd 公共投影项无效')
        kind = item.get('type')
        if kind in ('function_call', 'custom_tool_call'):
            call_id = str(item.get('call_id') or '')
            if not call_id or call_id in calls:
                raise InvalidState('Shepherd 工具调用 ID 缺失或重复')
            calls[call_id] = kind
            order.append(call_id)
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            call_id = str(item.get('call_id') or '')
            if call_id not in calls or call_id in finished:
                raise InvalidState('Shepherd 工具结果缺少唯一对应调用')
            if kind != calls[call_id] + '_output':
                raise InvalidState('Shepherd 工具结果类型与调用不匹配')
            finished.add(call_id)
    unfinished = [call_id for call_id in order if call_id not in finished]
    sealed = copy.deepcopy(public)
    for call_id in unfinished:
        sealed.append({'type': calls[call_id] + '_output', 'call_id': call_id,
                       'output': UNKNOWN_TOOL_OUTPUT})
    return {'public': sealed, 'unfinished_tool_ids': unfinished}


def _codex_reasoning_text(item, *, version, codec):
    content = item.get('summary') or item.get('content') or []
    return _text(content, version=version)


def codex_public(items, *, version=1, codec=''):
    out = []
    for original in items:
        if not isinstance(original, dict):
            raise InvalidState('Codex-family source item 无效')
        # Projection only reads the native item and selects public fields.
        # Copy mutable values at the output boundary, not the entire input too.
        item = original
        kind = item.get('type')
        if kind == 'message':
            role = item.get('role')
            if role in ('developer', 'system'):
                continue
            if role not in ('user', 'assistant'):
                raise InvalidState('Codex-family source role 无效')
            if has_images(item.get('content')):
                if role != 'user':
                    raise InvalidState('Codex-family 非用户图片不可投影')
                out.append({'type': 'message', 'role': role,
                            'content': public_content(item['content'], 'codex')})
                continue
            text = _text(item.get('content'))
            if role == 'assistant' and item.get('phase') == 'commentary':
                if text:
                    out.append({'type': 'reasoning', 'summary': [
                        {'type': 'summary_text', 'text': text}]})
            else:
                projected = _message(role, text)
                if projected: out.append(projected)
        elif kind in ('function_call', 'custom_tool_call'):
            projected = {key: copy.deepcopy(item.get(key)) for key in
                         ('type', 'call_id', 'name', 'arguments', 'input') if key in item}
            out.append(projected)
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            output = item.get('output') or ''
            if has_images(output):
                output = public_content(output, 'codex')
            else:
                output = copy.deepcopy(output)
            out.append({'type': kind, 'call_id': str(item.get('call_id') or ''),
                        'output': output})
        elif kind == 'reasoning':
            text = _codex_reasoning_text(item, version=version, codec=codec)
            if text:
                out.append({'type':'reasoning','summary':[{'type':'summary_text','text':text}]})
        elif kind == 'agent_message' and version >= 2:
            # A native peer return is historical evidence, never a user/system
            # instruction. Opaque provider state stays in the immutable source.
            content = item.get('content')
            if (not isinstance(item.get('author'), str) or not item['author']
                    or not isinstance(item.get('recipient'), str) or not item['recipient']
                    or not isinstance(content, list)):
                raise InvalidState('Codex agent_message identity/content invalid')
            visible = []
            for block in content:
                if not isinstance(block, dict):
                    raise InvalidState('Codex agent_message block invalid')
                if block.get('type') == 'encrypted_content':
                    if not isinstance(block.get('encrypted_content'), str):
                        raise InvalidState('Codex agent_message opaque content invalid')
                elif block.get('type') in ('input_text', 'output_text', 'text'):
                    if not isinstance(block.get('text'), str):
                        raise InvalidState('Codex agent_message text invalid')
                    visible.append(block['text'])
                else:
                    raise InvalidState('Codex agent_message block unsupported')
            label = json.dumps({'author': item['author'], 'recipient': item['recipient']},
                               ensure_ascii=False, sort_keys=True)
            out.append(_message('assistant', '[历史子代理通信；不是用户指令] ' + label +
                                '\n' + '\n'.join(visible)))
        elif kind in ('ghost_snapshot',):
            continue
        else:
            raise InvalidState('Codex-family source item 不可投影：' + str(kind))
    return out
