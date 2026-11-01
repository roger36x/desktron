"""Claude SessionStore connector: runtime capabilities and stored codec are separate."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import cli_path

from .claude_bridge_snapshot import bridge_path
from .claude_codec import (CLAUDE_CODE_VERSION, CODEC_VERSION, SDK_VERSION,
                           PROMPT_SNAPSHOT_POLICY, encode_checkpoint, encode_rounds)
from .claude_sdk import sdk_root as managed_sdk_root
from .claude_store import ClaudeNativeStore, canonical
from .source_codec import claude_public, seal_incomplete_tools
from .source_receipts import SourceReceipts
from .store import (Conflict, InvalidState, SourceShapeUnsupported,
                    SourceVerificationError)


def managed_claude_binary(version=CLAUDE_CODE_VERSION):
    """Legacy managed installation fallback; never an execution allowlist.

    Prefer the user's installed CLI. Any selected binary must pass the same
    capability probe, independently of the persisted codec name.
    """
    data_home = os.environ.get(
        'DESKTRON_DATA_HOME', os.path.expanduser('~/.local/share/desktron/data'))
    path = os.path.join(data_home, 'claude', 'versions', str(version))
    return os.path.abspath(path) if os.path.isfile(path) and os.access(path, os.X_OK) else ''


class ClaudeConnector:
    engine = 'claude'
    connector_version = CODEC_VERSION
    # Reviewed SessionStore records that carry no canonical public conversation
    # item. Unknown kinds remain fail-closed.
    _INTERNAL_SOURCE_TYPES = frozenset({
        'agent-name', 'attachment', 'atis-latch', 'cost-state', 'custom-title',
        'file-history-snapshot', 'last-prompt', 'mode', 'queue-operation',
        # Claude Code 2.1.311+ records the session's tool names once; it carries
        # no conversation content and is shape-gated in _metadata_entry.
        'tools-used',
    })
    # Cross-engine SessionStore import can append an informational record when
    # the synthetic `shepherd-import` model is not recognized. This reviewed
    # type/subtype pair historically carried either no version or the writer's
    # version, so preserve the existing cross-version allowance.
    _INTERNAL_SOURCE_SHAPES = frozenset({('system', 'informational')})
    # Reviewed structures, independent of the writer release label. Compaction
    # still requires the exact semantic boundary/summary checks below.
    _STRUCTURED_INTERNAL_SOURCE_SHAPES = frozenset({
        ('system', 'api_error'), ('system', 'compact_boundary'),
        ('system', 'local_command'), ('system', 'stop_hook_summary')})

    def __init__(self, binding, ownership_nonce, *, store_root, sdk_root=None,
                 claude_binary=None, node_binary=None, ownership=None):
        self.binding = dict(binding or {})
        self.binding_id = self.binding.get('binding_id')
        self.native_session_id = self.binding.get('native_session_id')
        self.native_cwd = self.binding.get('native_cwd')
        self.ownership_nonce = ownership_nonce
        self.sdk_root = os.path.abspath(os.fspath(sdk_root or self.default_sdk_root()))
        selected_claude = claude_binary if claude_binary is not None else (
            cli_path.which('claude') or managed_claude_binary() or '')
        self.claude_binary = os.path.abspath(selected_claude) if selected_claude else ''
        self.node_binary = node_binary or shutil.which('node') or ''
        store_root = os.path.abspath(os.fspath(store_root))
        self.store_root = store_root
        self.receipts = SourceReceipts(store_root)
        self.native = ClaudeNativeStore(os.path.join(store_root, 'native', 'claude'),
                                        self.binding, ownership_nonce, ownership=ownership)
        self.bridge = bridge_path('claude_store_bridge.mjs')
        self.bootstrap_bridge = bridge_path('claude_bootstrap.mjs')
        self._check_binding()
        self.native.ownership.verify(self.binding, self.ownership_nonce)

    @staticmethod
    def default_sdk_root():
        return managed_sdk_root()

    @classmethod
    def availability(cls, sdk_root=None, claude_binary=None, node_binary=None):
        from engine_protocol import sdk_availability
        binary = (claude_binary if claude_binary is not None else
                  (cli_path.which('claude') or managed_claude_binary()))
        return sdk_availability('claude', sdk_root or cls.default_sdk_root(), node_binary, binary)

    def ensure_prompt_snapshot(self, system_prompt):
        """Create/verify the first-request prompt snapshot for policy-marked Bindings.

        Legacy Bindings have no policy field and retain their original snapshot
        byte-for-byte. New policy-marked Bindings fail closed if history was
        imported before bootstrap or if the immutable prompt changes.
        """
        policy = str(self.binding.get('prompt_snapshot_policy') or '')
        if not policy:
            return {'legacy_preserved': True, 'native_revision': self.native.revision()}
        if policy != PROMPT_SNAPSHOT_POLICY:
            raise Conflict('Claude 静态提示快照策略不受支持')
        expected = str(self.binding.get('prompt_snapshot_sha256') or '')
        prompt = str(system_prompt or '')
        if (len(expected) != 64 or any(char not in '0123456789abcdef' for char in expected)
                or prompt and hashlib.sha256(prompt.encode()).hexdigest() != expected):
            raise Conflict('Claude 静态系统提示与 Binding 不匹配')
        entries = self.native.load()
        if entries:
            snapshots = [entry for entry in entries
                         if isinstance(entry, dict) and entry.get('type') == 'attachment'
                         and isinstance(entry.get('attachment'), dict)
                         and entry['attachment'].get('type') == 'prompt_snapshot']
            complete = any(entry.get('type') == 'assistant'
                           and (entry.get('message') or {}).get('id') == 'msg_shepherd_bootstrap'
                           and (entry.get('message') or {}).get('role') == 'assistant'
                           and (entry.get('message') or {}).get('stop_reason') == 'end_turn'
                           for entry in entries)
            exact = all((entry['attachment'].get('systemPrompt') == [prompt])
                        for entry in snapshots)
            if snapshots and exact and complete:
                return {'legacy_preserved':False,
                        'native_revision':self.native.revision()}
            raise Conflict('Claude 静态提示快照缺失、未完成或不匹配')
        if not prompt:
            raise Conflict('Claude 静态系统提示为空')
        self._available(); self.native.initialize()
        payload = {'sdkRoot':self.sdk_root, 'dbPath':self.native.path,
                   'sessionId':self.native_session_id, 'cwd':self.native_cwd,
                   'claudeBinary':self.claude_binary, 'systemPrompt':prompt,
                   'promptSnapshotPolicy':policy, 'promptSnapshotSha256':expected}
        fd, path = tempfile.mkstemp(prefix='.claude-bootstrap-', suffix='.json',
                                    dir=self.native.root)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                json.dump(payload, stream, ensure_ascii=False)
                stream.flush(); os.fsync(stream.fileno())
            completed = subprocess.run([self.node_binary, self.bootstrap_bridge, path],
                cwd=self.native_cwd, capture_output=True, text=True, encoding='utf-8', timeout=120)
            if completed.returncode:
                raise Conflict('Claude 静态提示快照初始化失败: ' +
                               (completed.stderr.strip() or 'unknown error'))
            try:
                result = json.loads(completed.stdout.strip().splitlines()[-1])
            except (ValueError, IndexError):
                raise Conflict('Claude 静态提示快照回执无效') from None
            revision = str(result.get('nativeRevision') or '')
            if result.get('type') != 'bootstrap' or revision != self.native.revision():
                raise Conflict('Claude 静态提示快照 Revision 不匹配')
            if self.prompt_snapshot_text() != prompt:
                raise Conflict('Claude 静态提示快照回读不匹配')
            return {'legacy_preserved':False, 'native_revision':revision}
        finally:
            try: os.unlink(path)
            except FileNotFoundError: pass

    def prompt_snapshot_text(self):
        policy = str(self.binding.get('prompt_snapshot_policy') or '')
        if not policy:
            return ''
        expected = str(self.binding.get('prompt_snapshot_sha256') or '')
        snapshots = []
        for entry in self.native.load():
            attachment = entry.get('attachment') if isinstance(entry, dict) else None
            if isinstance(attachment, dict) and attachment.get('type') == 'prompt_snapshot':
                snapshots.append(attachment.get('systemPrompt'))
        matches = [prompt[0] for prompt in snapshots
                   if isinstance(prompt, list) and len(prompt) == 1
                   and isinstance(prompt[0], str)
                   and hashlib.sha256(prompt[0].encode()).hexdigest() == expected]
        complete = any(entry.get('type') == 'assistant'
                       and (entry.get('message') or {}).get('id') == 'msg_shepherd_bootstrap'
                       and (entry.get('message') or {}).get('role') == 'assistant'
                       and (entry.get('message') or {}).get('stop_reason') == 'end_turn'
                       for entry in self.native.load())
        if (not snapshots or len(matches) != len(snapshots) or not complete
                or any(value != matches[0] for value in matches)):
            raise Conflict('Claude 静态提示快照缺失、未完成或冲突')
        return matches[0]

    def recovery_boundary(self):
        self._readback_available()
        entries = self.native.load()
        return {'start_seq': len(entries), 'native_revision': self.native.revision(), 'projection_version':2}

    def recover_suffix(self, boundary, native_locator=None):
        """Read a stable append-only suffix; a native terminal is not required."""
        self._readback_available()
        entries = self.native.load()
        native_locator = native_locator or {}
        start = native_locator.get('start_seq', boundary.get('start_seq'))
        revision = str(native_locator.get('native_revision')
                       or boundary.get('native_revision') or '')
        if not isinstance(start, int) or start < 0 or start > len(entries):
            raise Conflict('Claude 中断来源范围无效')
        revisions=self.native.prefix_revisions({start,len(entries)})
        prefix=revisions[start]
        if prefix != revision:
            raise Conflict('Claude 中断来源起点 Revision 不匹配')
        suffix = entries[start:]
        self._validate_source_entries(suffix, production=True)
        grounding_uuid = str(native_locator.get('grounding_uuid') or '')
        grounding_sha256 = str(native_locator.get('grounding_sha256') or '')
        if grounding_uuid and suffix:
            matches = [row for row in suffix if row.get('uuid') == grounding_uuid]
            if len(matches) != 1 or not self._grounding_entry(
                    matches[0], grounding_uuid, grounding_sha256):
                raise SourceVerificationError('Claude Shepherd 动态 Grounding 标记无效')
        sealed = seal_incomplete_tools(claude_public(
            suffix, version=boundary.get('projection_version', 1),
            grounding_uuid=grounding_uuid, grounding_sha256=grounding_sha256))
        final_revision=revisions[len(entries)]
        locator = {'start_seq': start, 'end_seq': len(entries),
                   'projection_version':boundary.get('projection_version', 1),
                   **({'grounding_uuid':grounding_uuid,
                       'grounding_sha256':grounding_sha256} if grounding_uuid else {}),
                   'terminal_uuid': str(next((row.get('uuid') for row in reversed(suffix)
                                             if row.get('uuid')), '') or ''),
                   'terminal_state': 'unknown', 'native_revision': final_revision,
                   'unfinished_tool_ids': sealed['unfinished_tool_ids']}
        return {'locator': locator,
                'source': {'codec': self.connector_version, 'entries': suffix},
                'public': sealed['public'],
                'unfinished_tool_ids': sealed['unfinished_tool_ids']}

    def interrupted_locator(self, _round_id, boundary, native_locator):
        """Compatibility terminal seal for the v1 recovery state machine."""
        recovered = self.recover_suffix(boundary, native_locator)
        if recovered['unfinished_tool_ids']:
            raise Conflict('Claude 中断来源工具调用未完整配对')
        locator = recovered['locator']
        locator.pop('unfinished_tool_ids', None)
        locator['terminal_state'] = 'interrupted_confirmed'
        return locator

    def preflight(self, rounds, *, checkpoint=None, checkpoint_meta=None):
        if checkpoint is not None:
            encode_checkpoint(self.binding, checkpoint_meta, checkpoint)
        encode_rounds(self.binding, rounds)

    def append(self, rounds, expected_revision=''):
        self._available()
        existing = self.native.load()
        # Native metadata such as last-prompt/mode can follow the last UUID.
        parent = next((entry['uuid'] for entry in reversed(existing) if entry.get('uuid')), None)
        legacy = {(entry.get('shepherd') or {}).get('round_id') for entry in existing}
        legacy -= {(entry.get('shepherd') or {}).get('round_id') for entry in existing
                   if (entry.get('shepherd') or {}).get('semantics_v2')}
        entries = encode_rounds(self.binding, rounds, parent_uuid=parent, legacy_rounds=legacy)
        if entries:
            # A retry starts from the original parent, not the current tail.
            # UUID/content validation in the store still rejects altered bytes.
            first = next((item for item in existing if item.get('uuid') == entries[0]['uuid']), None)
            if first is not None:
                entries[0]['parentUuid'] = first.get('parentUuid')
        revision = self.native.append(entries, expected_revision)
        self._verify(entries)
        return {'native_revision': revision}

    def resolve_source(self, source_ref):
        """Read an already-persisted round without consulting the mutable CLI.

        Source recovery is a storage operation. Requiring today's `claude`
        version here can strand a completed receipt after the global symlink
        auto-updates, and must never turn commit-only recovery into a rerun.
        """
        self._readback_available()
        self._check_source_ref(source_ref)
        round_id = source_ref['entry_id']
        all_entries = self.native.load()
        receipt = self.receipts.read(self.binding, round_id, required=False)
        return self._project_source(round_id, all_entries, receipt)

    def resolve_sources(self, source_refs):
        """Load one native snapshot and hash its requested prefixes once."""
        self._readback_available()
        for ref in source_refs:
            self._check_source_ref(ref)
        if not source_refs:
            return []
        all_entries = self.native.load()
        receipts = [self.receipts.read(self.binding, ref['entry_id'], required=False)
                    for ref in source_refs]
        ends = set()
        for receipt in receipts:
            end = ((receipt or {}).get('locator') or {}).get('end_seq')
            if isinstance(end, int) and 0 <= end <= len(all_entries):
                ends.add(end)
        revisions=self.native.prefix_revisions(ends)
        return [self._project_source(ref['entry_id'], all_entries, receipt, revisions)
                for ref, receipt in zip(source_refs, receipts)]

    def _project_source(self, round_id, all_entries, receipt, revisions=None):
        production = receipt is not None
        if production:
            locator = receipt.get('locator') or {}
            start, end = locator.get('start_seq'), locator.get('end_seq')
            terminal_state = locator.get('terminal_state')
            empty_unknown = terminal_state == 'unknown' and end == start
            if (not isinstance(start, int) or not isinstance(end, int)
                    or start < 0 or (end < start if empty_unknown else end <= start)
                    or end > len(all_entries)):
                raise SourceVerificationError('Claude Shepherd 来源范围无效')
            entries = all_entries[start:end]
            if terminal_state not in ('completed', 'failed_recoverable',
                                      'interrupted_confirmed', 'unknown'):
                raise SourceVerificationError('Claude Shepherd 来源终态无效')
            terminal = str(locator.get('terminal_uuid') or '')
            terminal_entry = next((row for row in entries
                                   if str(row.get('uuid') or '') == terminal), None)
            if terminal and terminal_entry is None:
                raise SourceVerificationError('Claude Shepherd 终态锚点不在来源范围')
            if terminal and terminal_entry.get('type') != 'assistant':
                raise SourceVerificationError('Claude Shepherd 终态锚点不是父查询 Assistant')
            revision=(self.native.prefix_revisions({end})[end]
                      if revisions is None else revisions[end])
            if locator.get('native_revision') != revision:
                raise SourceVerificationError('Claude Shepherd 来源 Revision 不匹配')
        else:
            entries = [row for row in all_entries
                       if (row.get('shepherd') or {}).get('round_id') == round_id]
        locator = (receipt or {}).get('locator') or {}
        if not entries and locator.get('terminal_state') != 'unknown':
            raise SourceVerificationError('Claude Shepherd 来源不存在')
        self._validate_source_entries(entries, production=production)
        source = {'codec': self.connector_version, 'entries': entries}
        grounding_uuid = str(locator.get('grounding_uuid') or '')
        grounding_sha256 = str(locator.get('grounding_sha256') or '')
        if grounding_uuid and entries:
            matches = [row for row in entries if row.get('uuid') == grounding_uuid]
            if len(matches) != 1 or not self._grounding_entry(
                    matches[0], grounding_uuid, grounding_sha256):
                raise SourceVerificationError('Claude Shepherd 动态 Grounding 标记无效')
        try:
            public = claude_public(entries, version=locator.get('projection_version', 1),
                                   grounding_uuid=grounding_uuid,
                                   grounding_sha256=grounding_sha256)
        except InvalidState as exc:
            raise SourceVerificationError(str(exc)) from exc
        if locator.get('terminal_state') == 'unknown':
            try:
                sealed = seal_incomplete_tools(public)
            except InvalidState as exc:
                raise SourceVerificationError(str(exc)) from exc
            if sealed['unfinished_tool_ids'] != locator.get('unfinished_tool_ids', []):
                raise SourceVerificationError('Claude Shepherd 未完成工具清单不匹配')
            public = sealed['public']
        return {'source': source, 'public': public}

    @staticmethod
    def _grounding_entry(row, grounding_uuid, grounding_sha256):
        message = row.get('message') if isinstance(row, dict) else None
        content = message.get('content') if isinstance(message, dict) else None
        first = content[0] if isinstance(content, list) and content else None
        text = first.get('text') if isinstance(first, dict) else None
        prefix = f'<system-reminder data-desktron-grounding="{grounding_uuid}">\n'
        return (row.get('type') == 'user' and row.get('uuid') == grounding_uuid
                and (row.get('origin') or {}).get('kind') == 'human'
                and message.get('role') == 'user' and isinstance(content, list)
                and len(content) >= 2 and isinstance(first, dict)
                and first.get('type') == 'text' and isinstance(text, str)
                and text.startswith(prefix) and text.endswith('\n</system-reminder>')
                and len(grounding_sha256) == 64
                and hashlib.sha256(text.encode()).hexdigest() == grounding_sha256)

    @staticmethod
    def _compact_boundary(row):
        metadata = row.get('compactMetadata')
        required = ('trigger', 'preTokens', 'postTokens',
                    'cumulativeDroppedTokens', 'durationMs')
        return (row.get('type') == 'system'
                and row.get('subtype') == 'compact_boundary'
                and isinstance(row.get('version'), str) and bool(row['version'])
                and row.get('content') == 'Conversation compacted'
                and row.get('level') == 'info'
                and row.get('parentUuid') is None
                and isinstance(row.get('uuid'), str) and bool(row['uuid'])
                and isinstance(row.get('logicalParentUuid'), str)
                and isinstance(metadata, dict)
                and metadata.get('trigger') in ('auto', 'manual')
                and all(isinstance(metadata.get(key), int)
                        and not isinstance(metadata.get(key), bool)
                        and metadata[key] >= 0 for key in required[1:]))

    _COMPACT_ATTACHMENT_TYPES = frozenset({'instructions', 'session_context', 'date'})

    @staticmethod
    def _compact_attachment(row, previous, boundary):
        attachment = row.get('attachment') if isinstance(row, dict) else None
        kind = attachment.get('type') if isinstance(attachment, dict) else None
        payload_ok = (
            isinstance(attachment.get('files'), list) if kind == 'instructions' else
            isinstance(attachment.get('context'), dict) if kind == 'session_context' else
            isinstance(attachment.get('date'), str) and bool(attachment['date'])
            if kind == 'date' else False)
        return (isinstance(previous, dict)
                and ClaudeConnector._compact_boundary(boundary)
                and row.get('type') == 'attachment'
                and kind in ClaudeConnector._COMPACT_ATTACHMENT_TYPES
                and payload_ok
                and (row.get('rendered') is None or isinstance(row.get('rendered'), list))
                and isinstance(row.get('uuid'), str) and bool(row['uuid'])
                and row.get('parentUuid') == previous.get('uuid')
                and row.get('version') == boundary.get('version'))

    @staticmethod
    def _compact_summary(row, previous, boundary=None):
        boundary = boundary or previous
        metadata = boundary.get('compactMetadata') if isinstance(boundary, dict) else None
        preserved = metadata.get('preservedMessages') if isinstance(metadata, dict) else None
        anchor = preserved.get('anchorUuid') if isinstance(preserved, dict) else None
        return (isinstance(row, dict)
                and row.get('type') == 'user'
                and isinstance(row.get('version'), str) and bool(row['version'])
                and row.get('isCompactSummary') is True
                and row.get('isVisibleInTranscriptOnly') is True
                and (row.get('message') or {}).get('role') == 'user'
                and isinstance(row.get('uuid'), str) and bool(row['uuid'])
                and isinstance(previous, dict)
                and ClaudeConnector._compact_boundary(boundary)
                and row.get('version') == boundary.get('version')
                and row.get('parentUuid') == previous.get('uuid')
                and ((previous is boundary and anchor in (None, row.get('uuid')))
                     or (previous is not boundary and isinstance(anchor, str)
                         and bool(anchor) and row.get('uuid') == anchor))
                and isinstance((row.get('message') or {}).get('content'), str))

    @staticmethod
    def _compact_shape_error(boundary, detail):
        version = boundary.get('version') if isinstance(boundary, dict) else ''
        label = f'（Claude Code {version}）' if version else ''
        return SourceShapeUnsupported(f'Claude Shepherd 压缩形状不受支持{label}：{detail}')

    @classmethod
    def _compact_segments(cls, entries):
        """Validate semantic compaction chains, independent of physical adjacency.

        A boundary is followed by zero or more reviewed internal attachments and
        exactly one summary. Every edge must form an unbroken UUID parent chain.
        """
        summaries = set()
        attachments = set()
        for index, boundary in enumerate(entries):
            if not (isinstance(boundary, dict)
                    and (boundary.get('type'), boundary.get('subtype')) ==
                        ('system', 'compact_boundary')):
                continue
            if not cls._compact_boundary(boundary):
                raise SourceVerificationError('Claude Shepherd 压缩边界字段无效')
            previous = boundary
            cursor = index + 1
            while cursor < len(entries) and isinstance(entries[cursor], dict) \
                    and entries[cursor].get('type') == 'attachment':
                row = entries[cursor]
                attachment = row.get('attachment') or {}
                if attachment.get('type') not in cls._COMPACT_ATTACHMENT_TYPES:
                    raise cls._compact_shape_error(boundary, '包含未知内部附件类型')
                if not cls._compact_attachment(row, previous, boundary):
                    detail = {'kind':attachment.get('type'),
                              'keys':sorted(row.keys()),
                              'attachment_keys':sorted(attachment.keys()),
                              'parent_match':row.get('parentUuid') == previous.get('uuid'),
                              'version_match':row.get('version') == boundary.get('version'),
                              'rendered_list':isinstance(row.get('rendered'), list)}
                    raise SourceVerificationError('Claude Shepherd 压缩附件父链或字段无效：'
                                                  + json.dumps(detail, ensure_ascii=False))
                attachments.add(cursor)
                previous = row
                cursor += 1
            following = entries[cursor] if cursor < len(entries) else None
            if not cls._compact_summary(following, previous, boundary):
                if (isinstance(following, dict)
                        and following.get('type') not in ('user', 'assistant', 'system')):
                    raise cls._compact_shape_error(boundary, '边界与摘要之间包含未知内部记录')
                raise SourceVerificationError('Claude Shepherd 压缩边界缺少唯一且父链完整的摘要')
            summaries.add(cursor)
        for index, row in enumerate(entries):
            if isinstance(row, dict) and row.get('isCompactSummary') is True \
                    and index not in summaries:
                raise SourceVerificationError('Claude Shepherd 压缩摘要没有匹配的边界')
        return {'summaries': summaries, 'attachments': attachments}

    @staticmethod
    def _metadata_entry(row):
        kind = row.get('type')
        if kind == 'custom-title':
            return isinstance(row.get('customTitle'), str)
        if kind == 'agent-name':
            return isinstance(row.get('agentName'), str)
        if kind == 'cost-state':
            return (isinstance(row.get('sessionId'), str)
                    and isinstance(row.get('modelUsage'), dict)
                    and isinstance(row.get('totalCostUSD'), (int, float))
                    and not isinstance(row.get('totalCostUSD'), bool))
        if kind == 'tools-used':
            tools = row.get('tools')
            return (isinstance(tools, list)
                    and all(isinstance(name, str) for name in tools)
                    and set(row) <= {'type', 'tools', 'sessionId'})
        return True

    # Keys that carry conversation content or join the uuid parent chain. An
    # unregistered record holding any of them can change what the model saw,
    # so it stays fail-closed until a connector release reviews its shape.
    _CONTENT_BEARING_KEYS = frozenset({
        'message', 'content', 'text', 'prompt', 'summary', 'toolUseResult',
        'toolUseID', 'tool_use_id', 'attachment', 'data', 'payload', 'snapshot',
        'uuid', 'parentUuid', 'logicalParentUuid', 'leafUuid', 'isSidechain',
        'isMeta', 'isCompactSummary', 'compactMetadata',
    })
    _UNREVIEWED_METADATA_LIMIT = 4096

    def _unreviewed_metadata(self, row):
        return (isinstance(row, dict) and row.get('sessionId') == self.native_session_id
                and self._unreviewed_metadata_shape(row))

    @classmethod
    def _unreviewed_metadata_shape(cls, row):
        """Accept a new Claude Code bookkeeping record that cannot affect context.

        Claude Code adds such records between releases (e.g. `tools-used` in
        2.1.311). The record is tolerated only when it is bound to this session,
        is not a conversation/system row, holds no content-bearing or chain key,
        and every value is a short scalar or a flat list of short scalars. It is
        reported as `tolerated_unreviewed_entry` instead of being silent.
        """
        kind = row.get('type')
        if (not isinstance(kind, str) or not re.fullmatch(r'[a-z][a-z0-9_-]{0,63}', kind)
                or kind in ('user', 'assistant', 'system', 'summary')
                or set(row) & cls._CONTENT_BEARING_KEYS):
            return False
        scalar = lambda value: (value is None or isinstance(value, (bool, int, float))
                                or (isinstance(value, str) and len(value) <= 256))
        for value in row.values():
            if isinstance(value, list):
                if len(value) > 128 or not all(scalar(item) for item in value):
                    return False
            elif not scalar(value):
                return False
        try:
            size = len(json.dumps(row, ensure_ascii=False))
        except (TypeError, ValueError):
            return False
        return size <= cls._UNREVIEWED_METADATA_LIMIT

    @classmethod
    def _reviewed_kind(cls, row):
        kind = row.get('type')
        shape = (kind, row.get('subtype'))
        return (kind in ('user', 'assistant') or kind in cls._INTERNAL_SOURCE_TYPES
                or shape in cls._INTERNAL_SOURCE_SHAPES
                or shape in cls._STRUCTURED_INTERNAL_SOURCE_SHAPES)

    def source_compatibility(self, source):
        entries = source.get('entries') if isinstance(source, dict) else None
        if not isinstance(entries, list):
            return []
        compact = self._compact_segments(entries)
        counts = {}
        for index, row in enumerate(entries):
            kinds = []
            if isinstance(row, dict) and self._compact_boundary(row):
                kinds.append('system/compact_boundary')
            elif index in compact['summaries']:
                kinds.append('user/compact_summary')
            elif index in compact['attachments']:
                kinds.append('attachment/' + str((row.get('attachment') or {}).get('type') or 'unknown'))
            elif isinstance(row, dict) and row.get('type') in ('custom-title','agent-name','cost-state',
                                                              'tools-used'):
                kinds.append(row['type'])
            elif isinstance(row, dict) and (row.get('type'), row.get('subtype')) == ('system','informational'):
                kinds.append('system/informational')
            elif (isinstance(row, dict)
                    and (row.get('type'), row.get('subtype')) == ('system','stop_hook_summary')):
                kinds.append('system/stop_hook_summary')
            elif (isinstance(row, dict) and not self._reviewed_kind(row)
                    and self._unreviewed_metadata(row)):
                kinds.append('unreviewed/' + row['type'])
            if (isinstance(row, dict) and row.get('cwd')
                    and os.path.realpath(str(row.get('cwd'))) != self.native_cwd):
                kinds.append('runtime_cwd_changed')
            for kind in kinds:
                counts[kind] = counts.get(kind, 0) + 1
        action = lambda kind: ('ignored_environment_attribute' if kind == 'runtime_cwd_changed'
                               else 'tolerated_unreviewed_entry' if kind.startswith('unreviewed/')
                               else 'ignored_internal_entry')
        return [{'engine':'claude', 'action':action(kind),
                 'kind':kind,'count':count} for kind, count in sorted(counts.items())]

    def _validate_source_entries(self, entries, *, production):
        compact = self._compact_segments(entries)
        for index, row in enumerate(entries):
            if not isinstance(row, dict):
                raise SourceVerificationError('Claude Shepherd 来源条目无效')
            kind = row.get('type')
            internal_shape = (kind, row.get('subtype'))
            if internal_shape == ('system', 'local_command'):
                content = row.get('content')
                if (row.get('level') != 'info' or not isinstance(content, str)
                        or not any(content.startswith('<local-command-' + channel + '>')
                                   and content.endswith('</local-command-' + channel + '>')
                                   for channel in ('stdout', 'stderr'))):
                    raise SourceVerificationError('Claude Shepherd 本地命令结果形状无效')
            if (kind in ('custom-title','agent-name','cost-state','tools-used')
                    and not self._metadata_entry(row)):
                raise SourceVerificationError('Claude Shepherd 会话元数据形状无效')
            if (kind not in ('user', 'assistant')
                    and kind not in self._INTERNAL_SOURCE_TYPES
                    and internal_shape not in self._INTERNAL_SOURCE_SHAPES
                    and internal_shape not in self._STRUCTURED_INTERNAL_SOURCE_SHAPES
                    and not self._unreviewed_metadata(row)):
                # A recognisable-but-unsupported record is a format drift, not
                # tampering: classify it so isolation reports the real cause.
                raise SourceShapeUnsupported(
                    f'Claude Shepherd 来源包含未知内部条目: type={kind!r} subtype={row.get("subtype")!r}')
            marker = row.get('shepherd') or {}
            session_id = row.get('sessionId')
            if ((kind != 'file-history-snapshot' and session_id != self.native_session_id)
                    or (kind == 'file-history-snapshot' and session_id
                        and session_id != self.native_session_id)):
                raise SourceVerificationError('Claude Shepherd 来源会话身份污染')
            # Per-entry cwd is mutable Claude runtime state (Bash `cd` may
            # legitimately change it). Binding.native_cwd remains the trusted
            # launch boundary; source bytes and revisions still detect tampering.
            if not production and marker.get('codec') != self.connector_version:
                raise SourceVerificationError('Claude Shepherd 来源身份不匹配')

    def load_checkpoint(self, public, expected_revision='', checkpoint=None):
        self._available()
        if checkpoint is None:
            raise InvalidState('Claude Connector 缺少 Checkpoint 元数据')
        # The first attempt is restricted to an empty Binding by SwitchRunner.
        # On recovery the exact deterministic UUID/content batch may already be
        # present while Coordinator.advance was not committed. Reject any other
        # native tail before relying on NativeStore's idempotent UUID check.
        entries = encode_checkpoint(self.binding, checkpoint, public)
        existing = self.native.load()
        if self.binding.get('prompt_snapshot_policy'):
            self.prompt_snapshot_text()  # Verify the immutable bootstrap prefix.
            has_suffix = len(existing) >= len(entries) and existing[-len(entries):] == entries
            prefix = existing[:-len(entries)] if has_suffix else existing
            prefix_revision=self.native.prefix_revisions({len(prefix)})[len(prefix)]
            if prefix_revision != str(expected_revision or '') or (not has_suffix
                    and self.native.revision() != str(expected_revision or '')):
                raise Conflict('Claude Portable Checkpoint Binding 已含其他内容')
        elif existing and existing != entries:
            raise Conflict('Claude Portable Checkpoint Binding 已含其他内容')
        revision = self.native.append(entries, expected_revision)
        self._verify(entries)
        return {'native_revision': revision}

    def _verify(self, expected_entries):
        result = subprocess.run([self.node_binary, self.bridge, self.sdk_root, 'verify',
                                 self.native.path, self.native_session_id, self.native_cwd],
                                capture_output=True, text=True, encoding='utf-8', timeout=30)
        if result.returncode:
            raise Conflict('Claude SessionStore SDK 回读失败')
        try: report = json.loads(result.stdout)
        except ValueError as exc: raise Conflict('Claude SessionStore SDK 回读格式无效') from exc
        expected = [((entry.get('shepherd') or {}).get('round_id')
                    or (entry.get('shepherd') or {}).get('checkpoint_id'))
                    for entry in expected_entries]
        expected = [value for value in expected if value]
        seen = report.get('shepherdIds') or []
        if (report.get('storeContract') != 'desk-claude-store-v1'
                or report.get('revision') != self.native.revision()
                or any(value not in seen for value in expected)):
            raise Conflict('Claude SessionStore SDK 回读内容不匹配')
        return report

    def _available(self):
        status = self.availability(self.sdk_root, self.claude_binary, self.node_binary)
        if not status['available']:
            raise Conflict('Claude Connector 不可用：' + status['message'])
        self.native.initialize()

    def _readback_available(self):
        """Stored-source decoding needs neither the CLI nor the installed SDK."""
        self.native.ownership.verify(self.binding, self.ownership_nonce)
        self.native.initialize()

    def _check_source_ref(self, source_ref):
        expected_key = f'{self.engine}:{self.native_session_id}:{source_ref.get("entry_id", "")}'
        if (not isinstance(source_ref, dict) or source_ref.get('kind') != 'native_round'
                or source_ref.get('engine') != self.engine
                or source_ref.get('session_id') != self.native_session_id
                or source_ref.get('source_key') != expected_key):
            raise Conflict('Claude Shepherd 来源引用不匹配')

    def _check_binding(self):
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
                            str(self.native_session_id or ''), re.I):
            raise Conflict('Claude Connector Session ID 不是 UUID')
        if (self.binding.get('engine') != self.engine
                or self.binding.get('binding_id') != self.binding_id
                or self.binding.get('native_session_id') != self.native_session_id
                or self.binding.get('connector_version') != self.connector_version
                or os.path.realpath(str(self.binding.get('native_cwd') or '')) != self.native_cwd):
            raise Conflict('Claude Connector 与 Binding 不匹配')
