"""Versioned canonical projection for Codex thread.inject_items."""
import copy
import hashlib
import json
from .image_content import has_images, native_content
from .source_codec import reasoning_text
from .tool_call_ids import (ToolCallIdError, history_projection_version,
                            protocol_call_id)

# Persisted format identities, not executable admission policy. Never relabel
# existing evidence because the CLI version changes. Decoders validate content.
CODEC_VERSIONS = {'codex': 'codex-0.154.0-v1'}
# Part of the disposable working-context identity: resident threads encoded
# before a target-boundary fix must be rebuilt instead of silently reused.
HISTORY_PROJECTION_VERSION = history_projection_version('codex')
MAX_CALL_ID_LENGTH = 64


class CodecError(ValueError):
    pass


def _text(content):
    if isinstance(content, str): return content
    if not isinstance(content, list): raise CodecError('Codex 公共消息 content 无效')
    values = []
    for block in content:
        if not isinstance(block, dict) or block.get('type') not in ('input_text','output_text','text'):
            raise CodecError('Codex 公共消息包含不支持的内容')
        values.append(str(block.get('text') or ''))
    return '\n'.join(values)


def marker(round_row, index, count):
    return 'shepherd:v1:%s:%s:%s:%s' % (
        round_row['offset'], index, count, round_row['entry_hash'])


def _native_call_id(call_id):
    # Responses input is bounded to 64 ASCII identifier characters. Keep valid
    # IDs unchanged; hash overlong, non-ASCII, or punctuation-bearing sources.
    try:
        return protocol_call_id(call_id, max_length=MAX_CALL_ID_LENGTH)
    except ToolCallIdError as exc:
        raise CodecError('Codex ' + str(exc)) from exc


def normalize_call_ids(items):
    """Return protocol-safe items without changing the verified public IDs."""
    out, sources = [], {}
    for item in items:
        if not isinstance(item, dict):
            raise CodecError('Codex 历史项无效')
        if 'call_id' not in item:
            out.append(item)
            continue
        call_id = str(item.get('call_id') or '')
        if not call_id:
            raise CodecError('Codex 工具调用 ID 缺失')
        native_id = _native_call_id(call_id)
        previous = sources.get(native_id)
        if previous is not None and previous != call_id:
            raise CodecError('Codex 工具调用 ID 归一化冲突')
        sources[native_id] = call_id
        out.append(item if native_id == call_id else {**item, 'call_id': native_id})
    return out


def encode_round(round_row, *, model_image_limit=False):
    public = round_row.get('public')
    if not isinstance(public, list): raise CodecError('Codex 公共投影必须是数组')
    out, pending, calls = [], set(), {}
    for item in public:
        if not isinstance(item, dict): raise CodecError('Codex 公共投影项无效')
        kind = item.get('type')
        if kind == 'message':
            role = item.get('role')
            if role not in ('user','assistant'): raise CodecError('Codex 不接受公共系统消息')
            if has_images(item.get('content')):
                if role != 'user':
                    raise CodecError('Codex 不接受非用户图片')
                content = native_content(item['content'], 'codex', model_image_limit=model_image_limit)
            else:
                content = [{'type':'input_text' if role == 'user' else 'output_text',
                            'text':_text(item.get('content'))}]
            value = {'type':'message','role':role,'content':content}
            if role == 'assistant': value['phase'] = item.get('phase') or 'final_answer'
        elif kind in ('function_call','custom_tool_call'):
            call_id = str(item.get('call_id') or '')
            if not call_id or call_id in calls: raise CodecError('Codex 工具调用 ID 缺失或重复')
            calls[call_id] = kind
            pending.add(call_id)
            if kind == 'function_call':
                raw = item.get('arguments')
                try: args = json.loads(raw) if isinstance(raw,str) else copy.deepcopy(raw)
                except (TypeError,ValueError) as exc: raise CodecError('Codex 工具参数不是 JSON') from exc
                if not isinstance(args,dict): raise CodecError('Codex 工具参数必须是对象')
                value={'type':'function_call','call_id':call_id,'name':str(item.get('name') or ''),
                       'arguments':json.dumps(args,ensure_ascii=False,separators=(',',':'))}
            else:
                if not isinstance(item.get('input'), str):
                    raise CodecError('Codex custom tool 输入必须是原始字符串')
                value={'type':'custom_tool_call','call_id':call_id,'name':str(item.get('name') or ''),
                       'input':str(item.get('input') or '')}
            if not value['name']: raise CodecError('Codex 工具名为空')
        elif kind in ('function_call_output','custom_tool_call_output'):
            call_id=str(item.get('call_id') or '')
            if call_id not in pending: raise CodecError('Codex 工具结果缺少对应调用')
            if kind != calls[call_id] + '_output':
                raise CodecError('Codex 工具结果类型与调用不匹配')
            pending.remove(call_id)
            output = item.get('output') or ''
            value={'type':kind,'call_id':call_id,
                   'output':(native_content(output, 'codex', model_image_limit=model_image_limit)
                             if has_images(output) else copy.deepcopy(output))}
        elif kind == 'reasoning':
            summary=reasoning_text(item)
            if not summary: continue
            value={'type':'message','role':'assistant','phase':'commentary',
                   'content':[{'type':'output_text','text':summary}]}
        else: raise CodecError('Codex 不支持公共投影项 '+str(kind))
        out.append(value)
    if pending: raise CodecError('Codex 工具结果尚未完整配对')
    count=len(out)
    prefixes={'message':'msg_','function_call':'fc_','function_call_output':'fco_',
              'custom_tool_call':'ctc_','custom_tool_call_output':'ctco_','reasoning':'rs_'}
    for index,value in enumerate(out):
        prefix=prefixes.get(value.get('type'))
        if not prefix: raise CodecError('Codex 注入项缺少原生 ID 前缀')
        value['id']=prefix+hashlib.sha256((round_row['round_id']+':'+str(index)).encode()).hexdigest()[:24]
        value['internal_chat_message_metadata_passthrough']={'turn_id':marker(round_row,index,count)}
    return normalize_call_ids(out)
