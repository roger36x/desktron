"""Capability-checked Pi SessionManager connector with copy-on-write commits."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from durable_json import locked
from pi_extensions import without_legacy_web_secrets
from .pi_codec import CODEC_VERSION, encode_round
from .image_content import model_patch_limited
from .source_codec import pi_public, pi_context_message, PI_PROJECTION_VERSION, seal_incomplete_tools
from .source_receipts import SourceReceipts
from .store import Conflict, InvalidState


def _pi_package_root(root):
    try:
        package = json.loads((root / 'package.json').read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return None
    if (isinstance(package, dict) and package.get('name') == '@earendil-works/pi-coding-agent'
            and (root / 'dist/index.js').is_file()):
        return str(root)
    return None


def _managed_pi_sdk_root(entry):
    # The official installer keeps its launcher outside node_modules. Follow
    # its current-version pointer, never choose a different installed release.
    if entry.parent.name != 'bin' or not (entry.parent / 'pi-launcher.js').is_file():
        return None
    install = entry.parent.parent / 'install'
    try:
        metadata = json.loads((install / 'managed-install.json').read_text(encoding='utf-8'))
        version = (install / 'current-version').read_text(encoding='utf-8').strip()
    except (OSError, ValueError, TypeError):
        return None
    if (not isinstance(metadata, dict) or metadata.get('kind') != 'pi-managed-install'
            or metadata.get('layout') != 'releases-v1' or metadata.get('schemaVersion') != 1
            or version in ('', '.', '..') or not re.fullmatch(r'[0-9A-Za-z._+-]+', version)):
        return None
    root = install / 'releases' / version / 'node_modules/@earendil-works/pi-coding-agent'
    return _pi_package_root(root.resolve())


def pi_sdk_root(binary=None):
    """Locate the SDK belonging to the selected CLI, without executing it."""
    binary = os.fspath(binary) if binary else 'pi'
    # Adapters pass the command name "pi", not necessarily an absolute npm
    # symlink. Resolve bare names exactly as process launch does (PATHEXT on
    # Windows); an explicit path must never fall back to a different installation.
    if os.path.basename(binary) == binary:
        binary = shutil.which(binary)
    if not binary:
        return None
    entry = Path(binary).resolve()
    candidates = [entry]
    shim = oscompat.npm_shim_argv(str(entry))
    if shim:
        candidates.append(Path(shim[1]).resolve())
    for candidate in candidates:
        for parent in candidate.parents:
            root = _pi_package_root(parent)
            if root:
                return root
        root = _managed_pi_sdk_root(candidate)
        if root:
            return root
    return None


class PiConnector:
    engine = 'pi'
    connector_version = CODEC_VERSION

    def __init__(self, binding, ownership_nonce, *, store_root, sdk_root=None,
                 node_binary=None, ownership=None):
        self.binding = dict(binding or {})
        self.binding_id = self.binding.get('binding_id')
        self.native_session_id = self.binding.get('native_session_id')
        self.native_cwd = self.binding.get('native_cwd')
        self.ownership_nonce = ownership_nonce
        self.sdk_root = os.path.abspath(os.fspath(sdk_root or pi_sdk_root() or ''))
        self.node_binary = node_binary or shutil.which('node') or ''
        self.store_root = os.path.abspath(os.fspath(store_root))
        self.directory = os.path.join(self.store_root, 'native', 'pi')
        self.path = os.path.join(self.directory, str(self.binding_id) + '.jsonl')
        self.bridge = str(Path(__file__).with_name('pi_bridge.mjs'))
        self.receipts = SourceReceipts(self.store_root)
        self.model_image_limit = False
        if ownership is None:
            from .ownership import Ownership
            ownership = Ownership(os.path.join(self.store_root, 'ownership'))
        self.ownership = ownership
        self._check_binding()
        self.ownership.verify(self.binding, self.ownership_nonce)

    @classmethod
    def availability(cls, sdk_root=None, node_binary=None):
        from engine_protocol import sdk_availability
        return sdk_availability('pi', sdk_root or pi_sdk_root(), node_binary)

    def recovery_boundary(self):
        self._available()
        self.bootstrap()
        report = self.verify()
        return {'start_seq': int(report.get('entries') or 0), 'projection_version':PI_PROJECTION_VERSION,
                'prefix_bytes': os.path.getsize(self.path),
                'native_revision': str(report.get('native_revision') or '')}

    def recover_suffix(self, boundary, _native_locator=None):
        self.ownership.verify(self.binding, self.ownership_nonce)
        self._safe_path(required=True)
        from .pi_history import read_entries, source_range
        all_entries = read_entries(self.path, self.native_session_id, self.native_cwd)
        before = boundary.get('start_seq')
        prefix_bytes = boundary.get('prefix_bytes')
        revision = str(boundary.get('native_revision') or '')
        report = {'native_revision': self.revision()}
        end = len(all_entries)
        if (not isinstance(before, int) or before < 0 or end < before
                or not isinstance(prefix_bytes, int) or prefix_bytes <= 0):
            raise Conflict('Pi 中断来源范围为空或无效')
        # Pi uses copy-on-write only for imports. The byte prefix proves the
        # logical range even when the native turn has no terminal record.
        prefix_digest = hashlib.sha256()
        with open(self.path, 'rb') as stream:
            remaining = prefix_bytes
            while remaining:
                block = stream.read(min(1024 * 1024, remaining))
                if not block:
                    raise Conflict('Pi 中断来源前缀被截断')
                prefix_digest.update(block); remaining -= len(block)
        if prefix_digest.hexdigest() != revision:
            raise Conflict('Pi 中断来源起点 Revision 不匹配')
        entries = source_range(all_entries, {'start': before, 'end': end}, self.connector_version)
        compatibility = self.source_compatibility(
            {'codec':self.connector_version, 'entries':entries},
            version=boundary.get('projection_version', 1))
        sealed = seal_incomplete_tools(pi_public(
            entries, version=boundary.get('projection_version', 1), allow_incomplete=True))
        locator = {'start_seq': before, 'end_seq': end,
                   'projection_version':boundary.get('projection_version', 1),
                   'terminal_state':'unknown',
                   'native_revision':str(report.get('native_revision') or self.revision()),
                   'unfinished_tool_ids':sealed['unfinished_tool_ids']}
        return {'locator':locator,
                'source':{'codec':self.connector_version, 'entries':entries},
                'public':sealed['public'],
                'unfinished_tool_ids':sealed['unfinished_tool_ids'],
                'compatibility':compatibility}

    @staticmethod
    def source_compatibility(source, *, version=PI_PROJECTION_VERSION):
        """Classify Pi entries that may be omitted from one recovered public turn.

        Compaction is a derived checkpoint over entries already present in the
        append-only source. Label/session metadata and custom state do not enter
        Pi's model context. Version 4 projects branch/custom messages explicitly;
        older receipt versions retain their original rejection boundary.
        """
        if type(version) is not int or version not in (1, 2, 3, 4):
            raise InvalidState('Pi projection version unsupported')
        entries = source.get('entries') if isinstance(source, dict) else None
        if not isinstance(entries, list):
            raise Conflict('Pi 中断来源格式无效')
        diagnostics = {}
        ordinary = {'message'}
        metadata = {'compaction', 'context_edit', 'label', 'session_info',
                    'thinking_level_change', 'model_change'}
        semantic = {'branch_summary', 'custom_message'}
        semantic_custom = {'shepherd-custom-tool-v1', 'shepherd-reasoning-v2',
                           'shepherd-tool-call-ids-v1'}
        for entry in entries:
            if not isinstance(entry, dict):
                raise Conflict('Pi 中断来源包含无效条目')
            kind = entry.get('type')
            if kind in ordinary:
                message = entry.get('message')
                if isinstance(message, dict) and message.get('role') == 'system':
                    from .source_codec import validate_pi_system_message
                    validate_pi_system_message(message)
                    diagnostics[('ignored_runtime_instructions', 'system')] = (
                        diagnostics.get(('ignored_runtime_instructions', 'system'), 0) + 1)
                continue
            if kind in semantic:
                if version < 4:
                    raise Conflict('Pi 中断来源包含未投影的语义条目：' + str(kind))
                pi_context_message(entry)
                diagnostics[('projected_context_message', kind)] = (
                    diagnostics.get(('projected_context_message', kind), 0) + 1)
                continue
            if kind == 'custom':
                custom_type = entry.get('customType')
                if not isinstance(custom_type, str) or not custom_type:
                    raise Conflict('Pi 中断来源包含无效扩展条目')
                if custom_type not in semantic_custom:
                    diagnostics[('ignored_custom_state', custom_type)] = (
                        diagnostics.get(('ignored_custom_state', custom_type), 0) + 1)
                continue
            if kind in metadata:
                diagnostics[('ignored_internal_entry', kind)] = (
                    diagnostics.get(('ignored_internal_entry', kind), 0) + 1)
                continue
            raise Conflict('Pi 中断来源包含未知条目：' + str(kind))
        return [{'engine':'pi', 'action':action, 'kind':kind, 'count':count}
                for (action, kind), count in sorted(diagnostics.items())][:16]

    def interrupted_locator(self, _round_id, boundary, native_locator):
        recovered = self.recover_suffix(boundary, native_locator)
        if recovered['unfinished_tool_ids']:
            raise Conflict('Pi 中断来源工具调用未完整配对')
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
        if checkpoint is not None:
            summary = checkpoint.get('summary') if isinstance(checkpoint, dict) else None
            if not isinstance(summary, str) or not summary.strip() or not checkpoint_meta:
                raise InvalidState('Pi Portable Checkpoint 缺少摘要或元数据')
        for row in rounds:
            self._encode(row)

    def append(self, rounds, expected_revision=''):
        self._available()
        if not rounds:
            current = self.revision()
            if current != str(expected_revision or ''):
                raise Conflict('Pi Session Revision 冲突')
            return {'native_revision': current}
        payload = {'codec': CODEC_VERSION, 'rounds': [
            {'roundId': row['round_id'], 'entryHash': row['entry_hash'],
             'messages': self._encode(row)} for row in rounds]}
        return {'native_revision': self._mutate('append', payload, expected_revision, rounds=rounds)}

    def resolve_source(self, source_ref):
        """Read committed storage without consulting the installed Pi release."""
        return self.resolve_sources([source_ref])[0]

    def resolve_sources(self, source_refs):
        """Read selected ranges from one immutable snapshot; never migrate history."""
        self.ownership.verify(self.binding, self.ownership_nonce)
        self._safe_path(required=True)
        receipts, queries = [], []
        for ref in source_refs:
            self._check_source_ref(ref)
            receipt = self.receipts.read(self.binding, ref['entry_id'], required=False)
            receipts.append(receipt)
            locator = (receipt or {}).get('locator') or {}
            queries.append({'start': locator.get('start_seq'), 'end': locator.get('end_seq')}
                           if receipt is not None else {'roundId': ref['entry_id']})
        if not queries:
            return []
        from .pi_history import read_entries, source_range
        entries = read_entries(self.path, self.native_session_id, self.native_cwd)
        sources = [source_range(entries, query, self.connector_version) for query in queries]
        return [self._project_source(entries, receipt) for entries, receipt in zip(sources, receipts)]

    def _project_source(self, entries, receipt):
        if not isinstance(entries, list):
            raise Conflict('Pi Shepherd 来源回读格式无效')
        source = {'codec': self.connector_version, 'entries': entries}
        locator = (receipt or {}).get('locator') or {}
        unknown = locator.get('terminal_state') == 'unknown'
        if unknown:
            self.source_compatibility(source, version=locator.get('projection_version', 1))
        public = pi_public(entries, version=locator.get('projection_version', 1),
                           allow_incomplete=unknown)
        if unknown:
            sealed = seal_incomplete_tools(public)
            if sealed['unfinished_tool_ids'] != locator.get('unfinished_tool_ids', []):
                raise Conflict('Pi Shepherd 未完成工具清单不匹配')
            public = sealed['public']
        return {'source': source, 'public': public}

    def load_checkpoint(self, public, expected_revision='', checkpoint=None):
        self._available()
        if checkpoint is None:
            raise InvalidState('Pi Connector 缺少 Checkpoint 元数据')
        summary = public.get('summary') if isinstance(public, dict) else None
        if not isinstance(summary, str) or not summary.strip():
            raise InvalidState('Pi Portable Checkpoint 缺少摘要')
        payload = {'codec': CODEC_VERSION, 'checkpointId': checkpoint['checkpoint_id'],
                   'coversThroughOffset': checkpoint['covers_through_offset'],
                   'coversHash': checkpoint['covers_hash'],
                   'estimatedTokens': checkpoint['estimated_tokens'], 'summary': summary.strip()}
        return {'native_revision': self._mutate('checkpoint', payload, expected_revision,
                                                checkpoint=checkpoint)}

    def bootstrap(self):
        """Create the private native file without introducing a public turn."""
        self._available()
        if os.path.exists(self.path):
            return {'native_revision': self.revision()}
        payload = {'codec': self.connector_version}
        return {'native_revision': self._mutate('bootstrap', payload, '', rounds=[])}

    def verify(self):
        self._available(); self._safe_path(required=True)
        report = self._run('verify', self.path, self.directory, None)
        report['native_revision'] = self.revision()
        return report

    def revision(self):
        if not os.path.exists(self.path): return ''
        self._safe_path(required=True)
        digest = hashlib.sha256()
        with open(self.path, 'rb') as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
        return digest.hexdigest()

    def _mutate(self, command, payload, expected_revision, *, rounds=None, checkpoint=None):
        self.ownership.verify(self.binding, self.ownership_nonce)
        os.makedirs(self.directory, mode=0o700, exist_ok=True)
        try: os.chmod(self.directory, 0o700)
        except OSError: pass
        with locked(self.path + '.coord'):
            current = self.revision()
            if current != str(expected_revision or ''):
                report = self.verify()
                if self._already(report, rounds=rounds, checkpoint=checkpoint): return current
                raise Conflict('Pi Session Revision 冲突')
            staging = tempfile.mkdtemp(prefix='.pi-stage-', dir=self.directory)
            temporary = os.path.join(staging, 'session.jsonl')
            try:
                if current:
                    shutil.copyfile(self.path, temporary); os.chmod(temporary, 0o600)
                    report = self._run(command, temporary, staging, payload)
                else:
                    report = self._run(command, '-', staging, payload)
                    generated = os.path.realpath(str(report.get('sessionFile') or ''))
                    if os.path.commonpath((os.path.realpath(staging), generated)) != os.path.realpath(staging):
                        raise Conflict('Pi SessionManager 返回了越界路径')
                    temporary = generated
                if not os.path.isfile(temporary) or not self._already(report, rounds=rounds, checkpoint=checkpoint):
                    raise Conflict('Pi SessionManager 回读内容不匹配')
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
                return self.revision()
            finally:
                shutil.rmtree(staging, ignore_errors=True)

    def _run(self, command, path, session_dir, payload):
        payload_path = ''
        try:
            if payload is not None:
                fd, payload_path = tempfile.mkstemp(prefix='.pi-payload-', suffix='.json', dir=self.directory)
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(payload, stream, ensure_ascii=False); stream.flush(); os.fsync(stream.fileno())
            argv = [self.node_binary, self.bridge, self.sdk_root, command, path,
                    session_dir, self.native_cwd, self.native_session_id, payload_path]
            runtime_env = without_legacy_web_secrets(os.environ)
            if command == 'resolve-batch':
                from .runner_process import NativeProcess
                process = NativeProcess(argv, cwd=self.native_cwd, env=runtime_env,
                                        timeout=30, hard_timeout=30, cancel_frame=None)
                sources = []
                try:
                    for frame in process.events():
                        if (frame.get('storeContract') != 'desk-pi-store-v1'
                                or frame.get('sessionId') != self.native_session_id):
                            raise Conflict('Pi SessionManager 身份或版本不匹配')
                        sources.append(frame.get('sourceEntries'))
                    return {'sources': sources}
                finally:
                    process.close()
            result = subprocess.run(argv,
                capture_output=True, text=True, encoding='utf-8', timeout=30, env=runtime_env)
            if result.returncode:
                raise Conflict('Pi SessionManager 写入或回读失败')
            try: report = json.loads(result.stdout)
            except ValueError as exc: raise Conflict('Pi SessionManager 回读格式无效') from exc
            if report.get('storeContract') != 'desk-pi-store-v1' or report.get('sessionId') != self.native_session_id:
                raise Conflict('Pi SessionManager 身份或版本不匹配')
            return report
        finally:
            if payload_path:
                try: os.unlink(payload_path)
                except FileNotFoundError: pass

    @staticmethod
    def _already(report, rounds=None, checkpoint=None):
        markers = report.get('markers') or []
        if rounds is not None:
            commits = {(row.get('data') or {}).get('roundId'): (row.get('data') or {}).get('entryHash')
                       for row in markers if row.get('customType') == 'shepherd-round-commit'}
            return all(commits.get(row['round_id']) == row['entry_hash'] for row in rounds)
        if checkpoint is not None:
            return any(row.get('customType') == 'shepherd-checkpoint'
                       and (row.get('data') or {}).get('checkpointId') == checkpoint['checkpoint_id']
                       and (row.get('data') or {}).get('coversHash') == checkpoint['covers_hash']
                       for row in markers)
        return True

    def _available(self):
        status = self.availability(self.sdk_root, self.node_binary)
        if not status['available']:
            raise Conflict('Pi Connector 不可用：' + status['message'])

    def _safe_path(self, required=False):
        expected = os.path.realpath(os.path.join(self.store_root, 'native', 'pi'))
        if os.path.realpath(os.path.dirname(self.path)) != expected or os.path.islink(self.path):
            raise Conflict('Pi Session 文件不在 Shepherd 私有目录')
        if required:
            info = os.stat(self.path)
            if not os.path.isfile(self.path) or info.st_uid != os.getuid() or oscompat.loose_mode(info.st_mode):
                raise Conflict('Pi Session 文件或权限无效')

    def _check_source_ref(self, source_ref):
        expected_key = f'{self.engine}:{self.native_session_id}:{source_ref.get("entry_id", "")}'
        if (not isinstance(source_ref, dict) or source_ref.get('kind') != 'native_round'
                or source_ref.get('engine') != self.engine
                or source_ref.get('session_id') != self.native_session_id
                or source_ref.get('source_key') != expected_key):
            raise Conflict('Pi Shepherd 来源引用不匹配')

    def _check_binding(self):
        if not re.fullmatch(r'[0-9a-f]{8}-[0-9a-f]{4}-[1-9a-f][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}',
                            str(self.native_session_id or ''), re.I):
            raise Conflict('Pi Connector Session ID 不是 UUID')
        if (self.binding.get('engine') != self.engine
                or self.binding.get('binding_id') != self.binding_id
                or self.binding.get('native_session_id') != self.native_session_id
                or self.binding.get('connector_version') != self.connector_version
                or os.path.realpath(str(self.binding.get('native_cwd') or '')) != self.native_cwd):
            raise Conflict('Pi Connector 与 Binding 不匹配')
