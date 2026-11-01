"""Shepherd's UI event boundary; never owns native execution or source storage.

Only enumerated presentation notifications may degrade to a visible warning.
Permission, terminal, raw-source and unknown events retain exception propagation.
The caller archives raw events independently; normalization never edits them.
"""
import logging
import os
import re
import traceback


class EventContractError(ValueError):
    def __init__(self, path, expected, value):
        self.path = path
        super().__init__(f'{path}: expected {expected}, got {type(value).__name__}')


def _field(value, key, types, *, required=False, path='event'):
    item = value.get(key)
    if item is None and not required:
        return
    if not isinstance(item, types):
        names = types if isinstance(types, tuple) else (types,)
        raise EventContractError(path + '.' + key, '|'.join(t.__name__ for t in names), item)


def _content(message, path):
    _field(message, 'content', list, path=path)
    for block in message.get('content') or []:
        if not isinstance(block, dict):
            raise EventContractError(path + '.content[]', 'dict', block)
        _field(block, 'type', str, required=True, path=path + '.content[]')
        for key in ('text', 'thinking', 'id', 'tool_use_id', 'name'):
            _field(block, key, str, path=path + '.content[]')


def normalize(engine, args):
    """Validate consumed containers, preserving optional/unknown native fields."""
    family = engine == 'codex'
    if family:
        method, event = args
        if not isinstance(method, str):
            raise EventContractError('method', 'str', method)
    else:
        event, = args
    if not isinstance(event, dict):
        raise EventContractError('event', 'dict', event)
    if not family:
        _field(event, 'type', str, required=True)
        method = event['type']
    if engine == 'claude':
        if method in ('assistant', 'user'):
            _field(event, 'message', dict, required=True)
            message = event['message']
            if isinstance(message.get('content'), str):
                message = {**message, 'content': [{'type': 'text', 'text': message['content']}]}
                event = {**event, 'message': message}
            _content(message, 'event.message')
        elif method == 'stream_event':
            _field(event, 'event', dict, required=True)
            stream = event['event']
            _field(stream, 'type', str, required=True, path='event.event')
            for key in ('content_block', 'delta', 'message'):
                _field(stream, key, dict, path='event.event')
            if isinstance(stream.get('delta'), dict):
                for key in ('text', 'thinking', 'partial_json'):
                    _field(stream['delta'], key, str, path='event.event.delta')
        if method == 'result':
            _field(event, 'usage', dict)
        _field(event, 'origin', dict)
    elif engine == 'pi':
        if method in ('message_start', 'message_end', 'turn_end'):
            _field(event, 'message', dict, required=True)
            message = event['message']
            _field(message, 'role', str, required=True, path='event.message')
            # Pi 0.86 emits system transcript messages before generation.
            # User/custom messages also permit string content. Normalize only
            # the UI view; native source and assistant/tool contracts stay intact.
            if message['role'] in ('system', 'user', 'custom') and isinstance(message.get('content'), str):
                message = {**message, 'content': [{'type': 'text', 'text': message['content']}]}
                event = {**event, 'message': message}
            _content(message, 'event.message')
        if method == 'message_update':
            _field(event, 'assistantMessageEvent', dict, required=True)
            stream = event['assistantMessageEvent']
            _field(stream, 'type', str, required=True, path='event.assistantMessageEvent')
            if stream.get('type') in ('text_delta', 'thinking_delta', 'toolcall_delta'):
                _field(stream, 'delta', str, path='event.assistantMessageEvent')
        if method.startswith('tool_execution_'):
            _field(event, 'toolCallId', str, required=True)
            for key in ('result', 'partialResult'):
                _field(event, key, dict)
                if isinstance(event.get(key), dict):
                    _content(event[key], 'event.' + key)
        if method == 'queue_update':
            for key in ('steering', 'followUp'):
                _field(event, key, list)
    elif family:
        if method in ('item/started', 'item/completed', 'rawResponseItem/completed'):
            _field(event, 'item', dict, required=True)
            _field(event['item'], 'type', str, required=True, path='event.item')
        if method in ('turn/started', 'turn/completed'):
            _field(event, 'turn', dict, required=True)
            _field(event['turn'], 'id', str, required=True, path='event.turn')
            if method == 'turn/completed':
                _field(event['turn'], 'status', str, required=True, path='event.turn')
        if method.endswith(('delta', 'Delta')):
            _field(event, 'delta', str)
        for key in ('tokenUsage', 'token_usage'):
            _field(event, key, dict)
    else:
        raise ValueError('Unsupported Shepherd engine')
    return (method, event) if family else (event,)


def presentation_only(engine, args):
    if engine == 'codex':
        return isinstance(args[0], str) and args[0] in {
            'item/started', 'item/completed', 'item/agentMessage/delta',
            'item/reasoning/summaryTextDelta', 'item/reasoning/textDelta',
            'item/commandExecution/outputDelta', 'item/fileChange/outputDelta',
            'thread/tokenUsage/updated', 'model/selected', 'model/rerouted',
        }
    event = args[0]
    if not isinstance(event, dict):
        return False
    kind = event.get('type')
    if not isinstance(kind, str):
        return False
    if engine == 'claude':
        return kind in ('assistant', 'user', 'stream_event') or (
            kind == 'system' and event.get('subtype') in ('status', 'compact_boundary'))
    return engine == 'pi' and kind in {
        'message_start', 'message_update', 'tool_execution_start',
        'tool_execution_update', 'tool_execution_end', 'turn_end',
        'compaction_start', 'compaction_end', 'queue_update',
    }


def diagnostic(engine, args, error):
    """Metadata only: no payload text, argument values, credentials or locals."""
    event = args[-1]
    name = args[0] if engine == 'codex' else (
        event.get('type') if isinstance(event, dict) else 'invalid')
    name = re.sub(r'[^a-zA-Z0-9_/.:-]', '?', str(name))[:80]
    fields = ('message', 'content', 'event', 'assistantMessageEvent', 'item',
              'turn', 'delta', 'result', 'usage', 'tokenUsage')
    shape = {key: type(event[key]).__name__ for key in fields if isinstance(event, dict) and key in event}
    if isinstance(event, dict) and isinstance(event.get('message'), dict):
        shape['message.content'] = type(event['message'].get('content')).__name__
    stack = [f'{os.path.basename(f.filename)}:{f.lineno}:{f.name}'
             for f in traceback.extract_tb(error.__traceback__)[-8:]]
    return {'engine': engine, 'event': name, 'exception': type(error).__name__,
            'field': error.path if isinstance(error, EventContractError) else '',
            'shape': shape, 'stack': stack}


class EventProjection:
    def __init__(self, engine, worker, ctx, warn):
        self.engine, self.worker, self.ctx, self.warn = engine, worker, ctx, warn
        self.failures = 0
        self.reported = set()

    def __call__(self, *args):
        from .lifecycle import _current
        if not _current(self.worker, self.ctx):
            return
        try:
            normalized = normalize(self.engine, args)
            if self.engine == 'codex':
                self.worker._app_event(*normalized, self.ctx)
            else:
                self.worker.handle_event(*normalized, self.ctx)
        except (AttributeError, TypeError, KeyError, IndexError, ValueError) as error:
            if type(error) not in (AttributeError, TypeError, KeyError, IndexError,
                                   ValueError, EventContractError):
                raise
            detail = diagnostic(self.engine, args, error)
            degradable = presentation_only(self.engine, args)
            key = (detail['event'], detail['exception'], detail['field'])
            # Bound repeated bad deltas; first occurrence always includes a stack.
            if key not in self.reported and len(self.reported) < 8:
                self.reported.add(key)
                logging.getLogger(__name__).error('Shepherd event contract: %s', detail)
            if not degradable:
                raise
            self.failures += 1
            self.ctx['_projection_failures'] = self.failures
            if self.failures == 1:
                self.warn(detail)
