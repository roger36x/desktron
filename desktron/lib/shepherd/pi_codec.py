"""Version-gated projection into Pi 0.85.1 AgentMessage values."""
import copy
import json
from .image_content import has_images, native_content
from .source_codec import reasoning_text
from .tool_call_ids import (ToolCallIdError, call_id_mapping,
                            history_projection_version)

PI_VERSION = '0.85.1'
CODEC_VERSION = 'pi-0.85.1-v1'
HISTORY_PROJECTION_VERSION = history_projection_version('pi')
# OpenAI Chat is the narrowest ordinary Pi provider boundary. Mistral requires
# nine characters but already performs collision-checked hashing of every ID.
MAX_IMPORTED_CALL_ID_LENGTH = 40


class CodecError(ValueError):
    pass


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        raise CodecError('Pi 公共消息 content 无效')
    values = []
    for block in content:
        if not isinstance(block, dict) or block.get('type') not in ('input_text', 'output_text', 'text'):
            raise CodecError('Pi 公共消息包含不支持的内容')
        values.append(str(block.get('text') or ''))
    return '\n'.join(values)


def _usage():
    return {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0,
            'totalTokens': 0,
            'cost': {'input': 0, 'output': 0, 'cacheRead': 0,
                     'cacheWrite': 0, 'total': 0}}


def encode_round(round_row, *, model_image_limit=False):
    public = round_row.get('public')
    if not isinstance(public, list):
        raise CodecError('Pi 公共投影必须是数组')
    try:
        call_ids = call_id_mapping(public, max_length=MAX_IMPORTED_CALL_ID_LENGTH)
    except ToolCallIdError as exc:
        raise CodecError('Pi ' + str(exc)) from exc
    aliases = [{'native_id': native, 'public_id': source}
               for source, native in call_ids.items() if native != source]
    messages, names, pending, kinds = [], {}, set(), {}
    if aliases:
        # SessionManager excludes custom entries from model context. The marker
        # lets source verification restore the public IDs without exposing an
        # overlong or provider-specific value to any Pi target provider.
        messages.append({'type':'custom', 'customType':'shepherd-tool-call-ids-v1',
                         'data':{'ids':aliases}})
    def assistant(content):
        return {'role': 'assistant', 'content': content, 'api': 'shepherd-import',
                'provider': 'shepherd', 'model': 'history', 'timestamp': 0,
                'stopReason': ('toolUse' if any(part['type'] == 'toolCall' for part in content) else 'stop'),
                'usage': _usage()}
    for item in public:
        if not isinstance(item, dict):
            raise CodecError('Pi 公共投影项无效')
        kind = item.get('type')
        if kind == 'message':
            role = item.get('role')
            if role not in ('user', 'assistant'):
                raise CodecError('Pi 不接受公共系统消息')
            if has_images(item.get('content')):
                if role != 'user':
                    raise CodecError('Pi 不接受非用户图片')
                content = native_content(item['content'], 'pi', model_image_limit=model_image_limit)
            else:
                content = [{'type': 'text', 'text': _text(item.get('content'))}]
            messages.append({'role': 'user', 'content': content, 'timestamp': 0}
                            if role == 'user' else assistant(content))
        elif kind == 'reasoning':
            summary = reasoning_text(item)
            if summary:
                messages.append({'type':'custom', 'customType':'shepherd-reasoning-v2', 'data':{}})
                messages.append(assistant([{'type': 'text', 'text': summary}]))
        elif kind in ('function_call', 'custom_tool_call'):
            source_call_id = str(item.get('call_id') or '')
            call_id = call_ids.get(source_call_id, source_call_id)
            if not call_id or call_id in names:
                raise CodecError('Pi 工具调用 ID 缺失或重复')
            if kind == 'custom_tool_call':
                raw = item.get('input')
                if not isinstance(raw, str):
                    raise CodecError('Pi custom tool 输入必须是原始字符串')
                arguments = {'input': raw}
            else:
                raw = item.get('arguments')
                try: arguments = json.loads(raw) if isinstance(raw, str) else copy.deepcopy(raw)
                except (TypeError, ValueError) as exc: raise CodecError('Pi 工具参数不是 JSON') from exc
            if not isinstance(arguments, dict):
                raise CodecError('Pi 工具参数必须是对象')
            names[call_id] = str(item.get('name') or '')
            if not names[call_id]:
                raise CodecError('Pi 工具名为空')
            pending.add(call_id)
            kinds[call_id] = kind
            if kind == 'custom_tool_call':
                # SessionManager custom entries stay out of buildSessionContext.
                # Do not add private properties to an AgentMessage/toolCall.
                messages.append({'type':'custom', 'customType':'shepherd-custom-tool-v1',
                                 'data':{'call_id':call_id, 'name':names[call_id]}})
            block = {'type': 'toolCall', 'id': call_id, 'name': names[call_id], 'arguments': arguments}
            if messages and messages[-1].get('role') == 'assistant' and messages[-1].get('stopReason') == 'toolUse':
                messages[-1]['content'].append(block)
            else:
                messages.append(assistant([block]))
        elif kind in ('function_call_output', 'custom_tool_call_output'):
            source_call_id = str(item.get('call_id') or '')
            call_id = call_ids.get(source_call_id, source_call_id)
            if call_id not in pending:
                raise CodecError('Pi 工具结果缺少对应调用')
            if kind != kinds[call_id] + '_output':
                raise CodecError('Pi 工具调用与结果类型不匹配')
            pending.remove(call_id)
            if not isinstance(item.get('output'), (str, list)):
                raise CodecError('Pi 工具结果仅支持文本或图片块，未转换的结构化结果已保留')
            multimodal = has_images(item.get('output'))
            output = (native_content(item['output'], 'pi', model_image_limit=model_image_limit)
                      if multimodal else _text(item['output']))
            failed = (any(str(block.get('text') or '').startswith(
                              ('[用户中断]', '[工具执行失败]'))
                          for block in output if isinstance(block, dict))
                      if multimodal else output.startswith(('[用户中断]', '[工具执行失败]')))
            messages.append({'role': 'toolResult', 'toolCallId': call_id,
                             'toolName': names[call_id], 'isError': failed,
                             'timestamp': 0, 'content': (output if multimodal else
                                                        [{'type': 'text', 'text': output}])})
        else:
            raise CodecError('Pi 不支持公共投影项 ' + str(kind))
    if pending:
        raise CodecError('Pi 工具结果尚未完整配对')
    # Pi defers creating a persistent file until an assistant message exists.
    # An acknowledged interruption can legitimately contain only user-side
    # context, so add an empty aborted assistant boundary before a trailing user.
    if messages and not any(message.get('role') == 'assistant' for message in messages):
        messages.insert(1, {**assistant([]), 'stopReason': 'aborted'})
    return messages
