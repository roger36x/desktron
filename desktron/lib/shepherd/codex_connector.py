"""Persistent Codex materialization in a Shepherd-private engine home."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import time

from durable_json import locked
from .codex_codec import CODEC_VERSIONS, encode_round, normalize_call_ids
from .image_content import restore_native_images, model_patch_limited
from .source_codec import codex_public, seal_incomplete_tools
from .source_receipts import SourceReceipts
from .store import Conflict, InvalidState, valid_id

_MARKER = re.compile(r'shepherd:v1:(\d+):(\d+):(\d+):([0-9a-f]{64})')


class CodexFamilyConnector:
    writer_kind = 'process'
    writer_notifications = True
    supports_interrupted_recovery = True

    def __init__(self, binding, ownership_nonce, *, store_root, binary=None, ownership=None):
        self.binding = dict(binding or {})
        self.engine = self.binding.get('engine')
        self.binding_id = self.binding.get('binding_id')
        self.native_session_id = self.binding.get('native_session_id')
        self.native_cwd = self.binding.get('native_cwd')
        self.ownership_nonce = ownership_nonce
        self.connector_version = CODEC_VERSIONS.get(self.engine, '')
        self.binary = binary or shutil.which(self.engine) or ''
        self.store_root = os.path.abspath(os.fspath(store_root))
        self.root = os.path.join(self.store_root, 'native', str(self.engine), str(self.binding_id))
        self.home = os.path.join(self.root, 'home')
        self.receipts = SourceReceipts(self.store_root)
        self.model_image_limit = False
        if ownership is None:
            from .ownership import Ownership
            ownership = Ownership(os.path.join(self.store_root, 'ownership'))
        self.ownership = ownership
        self._check_binding()
        self.ownership.verify(self.binding, self.ownership_nonce)

    @classmethod
    def availability(cls, engine, binary=None):
        if engine not in CODEC_VERSIONS:
            return {'available': False, 'reason': 'unsupported_engine',
                    'message': '不支持的 Codex-family 引擎；消息未派发。'}
        from codex_protocol import availability
        return availability(binary, engine=engine)

    @classmethod
    def create_native(cls, engine, native_cwd, *, store_root, binding_id,
                      binary=None, model='', on_writer=None):
        """Create a private empty thread before Coordinator.bind signs ownership."""
        version = CODEC_VERSIONS.get(engine)
        if not version:
            raise Conflict('Codex-family engine 不受支持')
        binary = binary or shutil.which(engine) or ''
        status = cls.availability(engine, binary)
        if not status['available']:
            raise Conflict('Codex-family Connector 不可用：' + status.get('message', status['reason']))
        binding_id = valid_id(binding_id, 'binding_id')
        native_cwd = os.path.realpath(str(native_cwd or ''))
        if not os.path.isabs(native_cwd) or not os.path.isdir(native_cwd):
            raise InvalidState('Codex-family native_cwd 无效')
        store_root = os.path.abspath(os.fspath(store_root))
        root = os.path.join(store_root, 'native', engine, binding_id)
        home = os.path.join(root, 'home')
        cls._make_private(home, store_root)
        cls._seed_private_config(engine, home)
        conn = None
        try:
            conn = cls._connection(native_cwd, cls._env(engine, home), binary,
                                   on_writer=on_writer)
            conn.initialize(overall=30)
            params = {'cwd': native_cwd, 'approvalPolicy': 'never',
                      'sandbox': 'read-only', 'experimentalRawEvents': True}
            if model:
                params['model'] = str(model)
            result = conn.request('thread/start', params, overall=30)
            thread = result.get('thread') or {}
            thread_id = str(thread.get('id') or '')
            if not re.fullmatch(r'[0-9a-f-]{36}', thread_id, re.I):
                raise Conflict('Codex-family Thread ID 不是 UUID')
            # An empty thread is not persisted. Materialize a non-semantic
            # assistant boundary; it has no text, tool or user instruction.
            conn.request('thread/inject_items', {'threadId': thread_id, 'items': [{
                'type': 'message', 'role': 'assistant', 'phase': 'commentary', 'content': [],
                'id': 'msg_' + hashlib.sha256(('bootstrap:' + binding_id).encode()).hexdigest()[:24],
                'internal_chat_message_metadata_passthrough': {
                    'turn_id': 'shepherd-bootstrap:v1:' + binding_id}}]}, overall=30)
            thread = (conn.request('thread/read', {'threadId': thread_id,
                      'includeTurns': True}, overall=30).get('thread') or thread)
            path = cls._private_rollout(thread, home, thread_id)
            return {'native_session_id': thread_id,
                    'native_revision': cls._revision(path),
                    'connector_version': version}
        finally:
            if conn:
                try: conn.close()
                except Exception: pass
            if callable(on_writer):
                on_writer(conn.proc if conn else None, 'stopped')

    def recovery_boundary(self):
        self._available(); self._safe_home()
        self.ownership.verify(self.binding, self.ownership_nonce)
        conn = None
        try:
            conn = self._open()
            path = self._path(self._read(conn))
            lines = self._raw_lines(path)
            return {'start_seq': len(lines), 'projection_version':2,
                    'native_revision': hashlib.sha256(b''.join(lines)).hexdigest()}
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    def recover_suffix(self, boundary, native_locator, trusted_public=None):
        """Read persisted turn items without requiring a native terminal event."""
        self._available(); self._safe_home()
        self.ownership.verify(self.binding, self.ownership_nonce)
        turn_id = str((native_locator or {}).get('turn_id') or '')
        start = boundary.get('start_seq')
        revision = str(boundary.get('native_revision') or '')
        if not isinstance(start, int) or start < 0:
            raise Conflict('Codex-family 中断来源定位无效')
        conn = None
        try:
            conn = self._open()
            path = self._path(self._read(conn))
            lines = self._raw_lines(path)
            if len(lines) < start or hashlib.sha256(b''.join(lines[:start])).hexdigest() != revision:
                raise Conflict('Codex-family 中断来源起点不匹配')
            if not turn_id and len(lines) != start:
                raise Conflict('Codex-family 新增记录缺少 Turn ID，无法安全归属')
            items = self._turn_items(path, turn_id) if turn_id else []
            projected = restore_native_images(items, trusted_public)
            sealed = seal_incomplete_tools(codex_public(
                projected, version=boundary.get('projection_version', 1),
                codec=self.connector_version))
            locator = {'turn_id':turn_id, 'start_seq':start, 'end_seq':len(lines),
                       'terminal_state':'unknown',
                       'projection_version':boundary.get('projection_version', 1),
                       'native_revision':self._revision(path),
                       'unfinished_tool_ids':sealed['unfinished_tool_ids']}
            return {'locator':locator,
                    'source':{'codec':self.connector_version, 'turn_id':turn_id,
                              'items':items},
                    'public':sealed['public'],
                    'unfinished_tool_ids':sealed['unfinished_tool_ids']}
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    def interrupted_locator(self, _round_id, boundary, native_locator):
        recovered = self.recover_suffix(boundary, native_locator)
        terminal = None
        conn = None
        try:
            conn = self._open()
            path = self._path(self._read(conn))
            lines = self._raw_lines(path)
            terminal = self._turn_terminal(lines[boundary.get('start_seq'):],
                                           str(native_locator.get('turn_id') or ''))
        finally:
            if conn:
                try: conn.close()
                except Exception: pass
        if terminal not in ('task_complete', 'turn_aborted'):
            raise Conflict('Codex-family 中断来源缺少原生终态')
        if recovered['unfinished_tool_ids']:
            raise Conflict('Codex-family 中断来源工具调用未完整配对')
        locator = recovered['locator']
        locator.pop('unfinished_tool_ids', None)
        locator['terminal_state'] = 'interrupted_confirmed'
        return locator

    def configure_target(self, options):
        self.model_image_limit = model_patch_limited(
            self.engine, options.get('model'), options.get('provider'))

    def _encode(self, row):
        return encode_round(row, model_image_limit=self.model_image_limit)

    def preflight(self, rounds, *, checkpoint=None, checkpoint_meta=None):
        """Pure encoding check; never opens or mutates the native thread."""
        if checkpoint is not None:
            self._checkpoint_items(checkpoint, checkpoint_meta)
        for row in rounds:
            self._encode(row)

    def _checkpoint_items(self, public, checkpoint):
        if self.engine != 'codex':
            raise Conflict('该引擎不支持 Portable Checkpoint 摘要类型，未写入')
        if not isinstance(checkpoint,dict):
            raise InvalidState('Codex-family Portable Checkpoint 缺少元数据')
        from .portable_checkpoint import SUPPORTED_CODECS, codex_items
        if isinstance(public,dict) and public.get('codec') in SUPPORTED_CODECS:
            if (public.get('checkpoint_id')!=checkpoint.get('checkpoint_id')
                    or public.get('through_seq')!=checkpoint.get('covers_through_seq')):
                raise InvalidState('Codex-family Portable Checkpoint 身份不匹配')
            return normalize_call_ids(codex_items(public))
        raise InvalidState('Codex-family 只接受 Portable Working Context v2/v3')

    def load_checkpoint(self, public, expected_revision='', checkpoint=None, on_writer=None):
        """Idempotently inject inert portable history into an empty private thread."""
        self._available();self._safe_home()
        self.ownership.verify(self.binding,self.ownership_nonce)
        expected=self._checkpoint_items(public,checkpoint)
        marker=((expected[0].get('internal_chat_message_metadata_passthrough') or {}).get('turn_id')
                if expected else '')
        if not marker:raise InvalidState('Codex-family Portable Checkpoint 标记缺失')
        with locked(os.path.join(self.root,'thread.coord')):
            conn=None
            try:
                conn=self._open(on_writer=on_writer)
                path=self._path(self._read(conn));current=self._revision(path)
                authoritative=self._authoritative_items(path)
                present=self._checkpoint_batch(path,marker,items=authoritative)
                if present:
                    if present!=[self._normalized_checkpoint_item(item) for item in expected]:
                        raise Conflict('Codex-family Portable Checkpoint 部分写入或内容冲突')
                    return {'native_revision':current}
                # thread/start installs current initial context before Shepherd
                # adds its empty bootstrap. Verify the exact bound structure and
                # revision; marker prefixes alone are not ownership proof.
                if (current!=str(expected_revision or '')
                        or not self._empty_binding_items(authoritative,self.binding_id)):
                    raise Conflict('Codex-family Portable Checkpoint 只能载入空白 Binding')
                conn.request('thread/inject_items',{'threadId':self.native_session_id,
                             'items':expected},overall=30)
                path=self._path(self._read(conn))
                present=self._checkpoint_batch(path,marker)
                if present!=[self._normalized_checkpoint_item(item) for item in expected]:
                    raise Conflict('Codex-family Portable Checkpoint 注入回读不完整')
                return {'native_revision':self._revision(path)}
            finally:
                if conn:
                    try:conn.close()
                    except Exception:pass
                if callable(on_writer):on_writer(conn.proc if conn else None,'stopped')

    def append(self, rounds, expected_revision='', on_writer=None):
        self._available()
        self._safe_home()
        self.ownership.verify(self.binding, self.ownership_nonce)
        encoded = {str(row['offset']): (row, self._encode(row)) for row in rounds}
        with locked(os.path.join(self.root, 'thread.coord')):
            conn = None
            try:
                conn = self._open(on_writer=on_writer)
                path = self._path(self._read(conn))
                current = self._revision(path)
                present = self._markers(path)
                missing = []
                for offset, (row, items) in encoded.items():
                    status = self._round_status(present, offset, row['entry_hash'], items)
                    if status == 'partial':
                        raise Conflict('Codex-family 存在部分注入的 Shepherd Round')
                    if status == 'missing':
                        missing.append((row, items))
                if current != str(expected_revision or '') and missing:
                    raise Conflict('Codex-family Thread Revision 冲突')
                if missing:
                    payload = [item for _row, items in missing for item in items]
                    conn.request('thread/inject_items', {
                        'threadId': self.native_session_id, 'items': payload}, overall=30)
                    path = self._path(self._read(conn))
                    present = self._markers(path)
                    for row, items in missing:
                        if self._round_status(present, str(row['offset']),
                                              row['entry_hash'], items) != 'complete':
                            raise Conflict('Codex-family 注入回读不完整')
                return {'native_revision': self._revision(path)}
            finally:
                if conn:
                    try: conn.close()
                    except Exception: pass
                if callable(on_writer):
                    on_writer(conn.proc if conn else None, 'stopped')

    def resolve_source(self, source_ref, trusted_public=None):
        """Read committed history without resuming the native thread."""
        return self.resolve_sources([source_ref], [trusted_public])[0]

    def resolve_sources(self, source_refs, trusted_publics=None):
        """One verified, read-only rollout snapshot per bounded source batch."""
        self._safe_home()
        self.ownership.verify(self.binding, self.ownership_nonce)
        for ref in source_refs:
            self._check_source_ref(ref)
        trusted_publics = ([None] * len(source_refs) if trusted_publics is None else trusted_publics)
        if len(trusted_publics) != len(source_refs):
            raise Conflict('Codex-family 来源批次输入长度不匹配')
        if not source_refs:
            return []
        receipts = [self.receipts.read(self.binding, ref['entry_id'], required=False)
                    for ref in source_refs]
        path, lines = self._source_snapshot()
        items = self._authoritative_items(path, lines=lines)
        markers = self._markers(path, items=items) if any(row is None for row in receipts) else None
        return [self._project_source(ref, receipt, path, trusted, raw_items=items,
                                     markers=markers, raw_lines=lines)
                for ref, receipt, trusted in zip(source_refs, receipts, trusted_publics)]

    def _source_snapshot(self):
        from .legacy_rollout import read_rollout
        return read_rollout(self.home, engine=self.engine, thread_id=self.native_session_id,
                            cwd=self.native_cwd)

    def _project_source(self, source_ref, receipt, path, trusted_public, *, raw_items=None,
                        markers=None, raw_lines=None):
        entry_id = source_ref['entry_id']
        if receipt is not None:
            locator = receipt.get('locator') or {}
            turn_id = str(locator.get('turn_id') or '')
            if turn_id:
                items = (self._turn_items(path, turn_id) if raw_items is None else
                         self._turn_items(path, turn_id, items=raw_items))
            elif locator.get('terminal_state') == 'unknown':
                lines = self._raw_lines(path) if raw_lines is None else raw_lines
                start, end = locator.get('start_seq'), locator.get('end_seq')
                revision = (self._revision(path) if raw_items is None else
                            self._items_revision(raw_items))
                if (not isinstance(start, int) or end != start or len(lines) != end
                        or revision != locator.get('native_revision')):
                    raise Conflict('Codex-family 空 Turn 来源边界不匹配')
                items = []
            else:
                raise Conflict('Codex-family native turn locator 无效')
            if not items and locator.get('terminal_state') != 'unknown':
                raise Conflict('Codex-family native turn 来源不存在')
            source = {'codec': self.connector_version, 'turn_id': turn_id,
                      'items': items}
        else:
            try: offset = str(int(str(entry_id).rsplit(':', 1)[-1]))
            except (TypeError, ValueError):
                raise Conflict('Codex-family Shepherd entry_id 无效') from None
            marker = (self._markers(path) if markers is None else markers).get(offset)
            if not marker or len(marker['items']) != marker['count']:
                raise Conflict('Codex-family Shepherd 来源不存在或不完整')
            items = [copy for _index, copy in sorted(marker['items'].items())]
            source = {'codec': self.connector_version, 'entry_hash': marker['entry_hash'],
                      'items': items}
        locator = (receipt or {}).get('locator') or {}
        projected = restore_native_images(items, trusted_public)
        public = codex_public(projected, version=locator.get('projection_version', 1),
                              codec=self.connector_version)
        if locator.get('terminal_state') == 'unknown':
            sealed = seal_incomplete_tools(public)
            if sealed['unfinished_tool_ids'] != locator.get('unfinished_tool_ids', []):
                raise Conflict('Codex-family Shepherd 未完成工具清单不匹配')
            public = sealed['public']
        return {'source': source, 'public': public}

    def verify(self):
        self._available()
        self._safe_home()
        self.ownership.verify(self.binding, self.ownership_nonce)
        conn = None
        try:
            conn = self._open()
            path = self._path(self._read(conn))
            markers = self._markers(path)
            return {'thread_id': self.native_session_id, 'path': path,
                    'native_revision': self._revision(path),
                    'markers': {offset: {'entry_hash': row['entry_hash'],
                                         'count': row['count'],
                                         'indexes': sorted(row['items'])}
                                for offset, row in markers.items()}}
        finally:
            if conn:
                try: conn.close()
                except Exception: pass

    def _open(self, on_writer=None):
        conn = self._connection(self.native_cwd, self._env(self.engine, self.home),
                                self.binary, on_writer=on_writer)
        try:
            conn.initialize(overall=30)
            conn.request('thread/resume', {'threadId': self.native_session_id,
                                          'cwd': self.native_cwd}, overall=30)
            return conn
        except Exception:
            try:
                conn.close()
            finally:
                if callable(on_writer):
                    try: on_writer(conn.proc, 'stopped')
                    except Exception: pass
            raise

    def _read(self, conn):
        result = conn.request('thread/read', {
            'threadId': self.native_session_id, 'includeTurns': True}, overall=30)
        thread = result.get('thread') or {}
        if str(thread.get('id') or '') != self.native_session_id:
            raise Conflict('Codex-family 回读 Thread ID 不匹配')
        return thread

    def _path(self, thread):
        return self._private_rollout(thread, self.home, self.native_session_id)

    @classmethod
    def _private_rollout(cls, thread, home, thread_id):
        if not re.fullmatch(r'[0-9a-f-]{36}', str(thread_id or ''), re.I):
            raise Conflict('Codex-family Thread ID 不是 UUID')
        path = os.path.realpath(str(thread.get('path') or ''))
        private_home = os.path.realpath(home)
        if not path or os.path.commonpath((private_home, path)) != private_home:
            raise Conflict('Codex-family Rollout 不在 Shepherd 私有 HOME')
        # thread/start may return before the writer flushes session_meta. Binding
        # ownership cannot be issued until the private source file is durable.
        deadline = time.monotonic() + 3
        while not os.path.isfile(path) and time.monotonic() < deadline:
            time.sleep(.02)
        if not os.path.isfile(path) or os.path.islink(path):
            raise Conflict('Codex-family Rollout 尚未持久化')
        info = os.stat(path)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise Conflict('Codex-family Rollout 所有权无效')
        try: os.chmod(path, 0o600)
        except OSError: raise Conflict('Codex-family Rollout 权限无法收紧') from None
        return path

    @staticmethod
    def _raw_lines(path):
        with open(path, 'rb') as stream:
            return list(stream)

    @classmethod
    def _turn_terminal(cls, lines, turn_id):
        found = []
        for line in lines:
            try:
                record = json.loads(line); payload = record.get('payload') or {}
            except (ValueError, AttributeError, UnicodeDecodeError):
                raise Conflict('Codex-family 中断来源记录无效') from None
            if record.get('type') != 'event_msg' or not isinstance(payload, dict):
                continue
            if str(payload.get('turn_id') or payload.get('turnId') or '') != turn_id:
                continue
            if payload.get('type') in ('task_complete', 'turn_aborted'):
                found.append(payload.get('type'))
        if len(found) != 1:
            raise Conflict('Codex-family 中断来源终态缺失或冲突')
        return found[0]

    @classmethod
    def _authoritative_items(cls, path, *, lines=None):
        """Yield original items while ignoring lossy compact projections.

        An operation=replace record may regenerate IDs, repeat selected
        Shepherd markers and omit most items from a round. Original append
        records remain durable authority, so replace must neither supersede nor
        duplicate them during source verification.
        """
        if lines is None:
            lines = cls._raw_lines(path)
        current = []
        for line in lines:
            try:
                record = json.loads(line); payload = record.get('payload') or {}
            except (ValueError, AttributeError):
                continue
            if record.get('type') == 'response_item':
                if isinstance(payload, dict): current.append(payload)
                continue
            if record.get('type') != 'history_mutation':
                continue
            items = payload.get('items')
            if not isinstance(items, list):
                raise Conflict('Codex-family history mutation items 无效')
            operation = payload.get('operation') or 'append'
            if operation == 'replace':
                continue
            if operation != 'append':
                raise Conflict('Codex-family history mutation operation 不受支持')
            current.extend(items)
        return current

    @classmethod
    def _turn_items(cls, path, turn_id, *, items=None):
        out = []
        for item in (cls._authoritative_items(path) if items is None else items):
            if not isinstance(item, dict): continue
            marker = ((item.get('internal_chat_message_metadata_passthrough') or {})
                      .get('turn_id') or '')
            if str(marker) == turn_id:
                out.append(cls._normalized_item(item))
        return out

    @classmethod
    def _empty_binding_items(cls, items, binding_id):
        if not isinstance(items,list) or len(items)!=3:
            return False
        expected=(('developer',('host_skills.instructions','permissions.instructions')),
                  ('user',('environments.environment_context',)))
        for item,(role,kinds) in zip(items[:2],expected):
            metadata=item.get('internal_chat_message_metadata_passthrough') or {}
            if (item.get('type')!='message' or item.get('role')!=role
                    or metadata.get('turn_id')!='auto-compact-0'
                    or tuple(metadata.get('content_item_kinds') or ())!=kinds):
                return False
        boot=items[2];metadata=boot.get('internal_chat_message_metadata_passthrough') or {}
        return (boot.get('type')=='message' and boot.get('role')=='assistant'
                and boot.get('phase')=='commentary' and boot.get('content')==[]
                and metadata.get('turn_id')=='shepherd-bootstrap:v1:'+str(binding_id)
                and tuple(metadata.get('content_item_kinds') or ())==())

    @classmethod
    def _checkpoint_batch(cls, path, marker, *, items=None):
        out=[]
        for item in (cls._authoritative_items(path) if items is None else items):
            if not isinstance(item,dict):continue
            value=str(((item.get('internal_chat_message_metadata_passthrough') or {})
                       .get('turn_id') or ''))
            if value==marker:
                out.append(cls._normalized_checkpoint_item(item))
        return out

    @classmethod
    def _markers(cls, path, *, items=None):
        """Read immutable injected batches, not compaction's partial projection.

        operation=replace carries selected copies of old markers, often only one
        item from a much larger round.  The original append remains durable and
        authoritative; accepting replace as a new batch would either invent a
        duplicate or make a complete historical round look partial.
        """
        found = {}
        for item in (cls._authoritative_items(path) if items is None else items):
            if not isinstance(item, dict):
                continue
            raw = ((item.get('internal_chat_message_metadata_passthrough') or {})
                   .get('turn_id') or '')
            match = _MARKER.fullmatch(str(raw))
            if not match:
                continue
            offset, index, count, entry_hash = match.groups()
            index, count = int(index), int(count)
            if count <= 0 or index >= count:
                raise Conflict('Codex-family Shepherd marker 无效')
            row = found.setdefault(offset, {'entry_hash': entry_hash,
                                            'count': count, 'items': {}})
            if row['entry_hash'] != entry_hash or row['count'] != count or index in row['items']:
                raise Conflict('Codex-family Shepherd marker 冲突或重复')
            row['items'][index] = cls._normalized_item(item)
        return found

    @classmethod
    def _round_status(cls, markers, offset, entry_hash, items):
        row = markers.get(str(offset))
        if row is None:
            return 'missing'
        expected = {index: cls._normalized_item(item) for index, item in enumerate(items)}
        if row['entry_hash'] != entry_hash or row['count'] != len(items) or row['items'] != expected:
            return 'partial'
        return 'complete'

    @staticmethod
    def _normalized_checkpoint_item(item):
        item=dict(item)
        metadata=item.get('internal_chat_message_metadata_passthrough')
        if isinstance(metadata,dict):
            normalized={'turn_id':metadata.get('turn_id')}
            kinds=metadata.get('content_item_kinds')
            # Codex annotates unclassified imported text as ``unknown``. That is
            # deterministic non-semantic decoration; the first summary's native
            # compaction.summary classification remains part of the proof.
            if kinds not in (None,[],['unknown']):normalized['content_item_kinds']=list(kinds)
            item['internal_chat_message_metadata_passthrough']=normalized
        return item

    @staticmethod
    def _normalized_item(item):
        # Internal read-only view: only metadata changes. Encoding and parsing
        # the entire item needlessly duplicates text/image bytes and containers.
        item = dict(item)
        metadata = item.get('internal_chat_message_metadata_passthrough')
        if isinstance(metadata, dict):
            item['internal_chat_message_metadata_passthrough'] = {'turn_id': metadata.get('turn_id')}
        return item

    @classmethod
    def _revision(cls, path):
        return cls._items_revision(cls._authoritative_items(path))

    @classmethod
    def _items_revision(cls, items):
        value = []
        for item in items:
            if not isinstance(item, dict): continue
            turn_id = ((item.get('internal_chat_message_metadata_passthrough') or {})
                       .get('turn_id') or '')
            if turn_id:
                value.append(cls._normalized_item(item))
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()

    @staticmethod
    def _connection(cwd, env, binary, on_writer=None):
        # Keep Shepherd status/import independent from the UI adapter bootstrap.
        from codex_appserver import _Connection
        return _Connection(cwd=cwd, env=env, binary=binary, on_process=on_writer)

    @staticmethod
    def _env(engine, home):
        env = dict(os.environ)
        env['CODEX_HOME'] = home
        return env

    @classmethod
    def _seed_private_config(cls, engine, home):
        """Copy only login/model configuration; never import ordinary sessions."""
        source = os.path.expanduser('~/.codex')
        target = home
        names = ('auth.json', 'config.toml', 'models_cache.json')
        os.makedirs(target, mode=0o700, exist_ok=True)
        try: os.chmod(target, 0o700)
        except OSError: pass
        for name in names:
            src, dst = os.path.join(source, name), os.path.join(target, name)
            if not os.path.isfile(src) or os.path.islink(src):
                continue
            temporary = dst + '.tmp.' + str(os.getpid())
            try:
                shutil.copyfile(src, temporary)
                os.chmod(temporary, 0o600)
                os.replace(temporary, dst)
            finally:
                try: os.unlink(temporary)
                except FileNotFoundError: pass

    @staticmethod
    def _make_private(path, store_root):
        root = os.path.realpath(store_root)
        target = os.path.realpath(path)
        if os.path.commonpath((root, target)) != root:
            raise Conflict('Codex-family HOME 越过 Shepherd 目录')
        current = root
        for part in os.path.relpath(target, root).split(os.sep):
            current = os.path.join(current, part)
            if os.path.lexists(current) and os.path.islink(current):
                raise Conflict('Codex-family HOME 不能经过符号链接')
            os.makedirs(current, mode=0o700, exist_ok=True)
            try: os.chmod(current, 0o700)
            except OSError: pass

    def _safe_home(self):
        expected = os.path.abspath(os.path.join(
            self.store_root, 'native', self.engine, str(self.binding_id), 'home'))
        current = self.store_root
        for part in os.path.relpath(expected, self.store_root).split(os.sep):
            current = os.path.join(current, part)
            if not os.path.isdir(current) or os.path.islink(current):
                raise Conflict('Codex-family HOME 不在 Shepherd 私有目录')
        if os.path.realpath(expected) != os.path.realpath(self.home):
            raise Conflict('Codex-family Binding HOME 不匹配')

    def _available(self):
        status = self.availability(self.engine, self.binary)
        if not status['available']:
            raise Conflict('Codex-family Connector 不可用：' + status.get('message', status['reason']))

    def _check_source_ref(self, source_ref):
        if not isinstance(source_ref, dict):
            raise Conflict('Codex-family Shepherd 来源引用不匹配')
        expected_key = f'{self.engine}:{self.native_session_id}:{source_ref.get("entry_id", "")}'
        if (source_ref.get('kind') != 'native_round'
                or source_ref.get('engine') != self.engine
                or source_ref.get('session_id') != self.native_session_id
                or source_ref.get('source_key') != expected_key):
            raise Conflict('Codex-family Shepherd 来源引用不匹配')

    def _check_binding(self):
        if self.engine not in CODEC_VERSIONS or not self.connector_version:
            raise Conflict('Codex-family Binding 引擎无效')
        if (self.binding.get('binding_id') != self.binding_id
                or self.binding.get('native_session_id') != self.native_session_id
                or self.binding.get('connector_version') != self.connector_version
                or os.path.realpath(str(self.binding.get('native_cwd') or '')) != self.native_cwd):
            raise Conflict('Codex-family Connector 与 Binding 不匹配')
        if not re.fullmatch(r'[0-9a-f-]{36}', str(self.native_session_id or ''), re.I):
            raise Conflict('Codex-family Thread ID 不是 UUID')
