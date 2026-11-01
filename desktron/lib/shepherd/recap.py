"""Temporary native recap branches. Never run through ExecutionHarness/Manifest."""
from contextlib import closing, contextmanager
import copy
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile

from .projector import ReadProjector
from .source_resolver import SourceResolver
from .runner_process import NativeProcess


class _WorkerView:
    """Override private resume lookup without mutating a shared protocol worker."""
    def __init__(self, worker, path):
        self.worker, self.path = worker, path

    def __getattr__(self, name):
        return getattr(self.worker, name)

    def _pi_path(self, _sid):
        return self.path


def generate(ad, sid, run, snap):
    # Peer execution already runs outside the public turn lock while remaining
    # tracked in `_peer_runs`; keep projection synchronous so deletion cannot
    # outlive an escaped worker that still holds private source material.
    return _generate(ad, sid, run, snap)


def _generate(ad, sid, run, snap):
    from engine_adapters.recap import RECAP_TIMEOUT, _engine_env, recap_prompt
    active = snap['_shepherd_source']
    state, binding = active['state'], active['binding']
    connector = ad._connector(binding)
    if getattr(connector, 'unified', False) and binding['engine'] == 'pi':
        return _generate_unified_pi(ad, connector, binding, sid, run, snap)
    if getattr(connector, 'unified', False) and binding['engine'] == 'claude':
        return _generate_unified_claude(ad, connector, binding, sid, run, snap)
    # Verify original source hashes before reading the native resume state. The
    # projection is not saved or sent as a replacement transcript.
    sources = set()
    for row in state.get('rounds') or []:
        source = row.get('source') or row.get('source_ref')
        if isinstance(source, dict) and source.get('engine') and source.get('session_id'):
            sources.add((source['engine'], source['session_id']))
    connectors = [ad._connector(row) for row in state['bindings'].values()
                  if (row['engine'], row['native_session_id']) in sources]
    rounds = ReadProjector(ad.service.store, SourceResolver(connectors)).project(
        sid, cancelled=run.cancelled.is_set)
    if run.cancelled.is_set() or not ad._recap_context_ready(sid, snap):
        return ''
    connector = ad._connector(binding)
    worker = ad._worker(binding['engine'])
    opts = dict(snap.get('opts') or {})
    env = _engine_env(worker, opts)
    if binding['engine'] == 'claude':
        import claude_token
        env = claude_token.apply(env, log=claude_token.desk_log)
    with _recap_connector(connector, rounds, opts,
                          disposable=snap.get('source') == getattr(worker, 'SRC_PEER', 'peer')) as derived:
        # `_recap_connector` may copy/append a large verified projection.  A
        # cancelled peer worker is allowed to finish that disposable local I/O,
        # but must never proceed to a model call after its caller released the
        # public turn lock.
        if run.cancelled.is_set():
            return ''
        if snap.get('source') == getattr(worker, 'SRC_PEER', 'peer'):
            run.mark_prepared()
        return _generate_native(derived, worker, binding, sid, run, snap, opts, env)


def _generate_unified_pi(ad, connector, binding, sid, run, snap):
    """Use the same verified checkpoint + admitted tail as a normal Pi turn.

    The planner is read-only: no new checkpoints, cursors or archive entries in
    the target. Never invoke a UnifiedRunner/ExecutionHarness for a sidecar.
    """
    from engine_adapters.recap import RECAP_TIMEOUT, _engine_env, recap_prompt
    from .context_budget import estimate_context_tokens
    from .context_fallback import native_items, text_receipt
    from .execution_budget import runner_timeouts
    from .pi_connector import pi_sdk_root
    worker = ad._worker('pi')
    opts = dict(snap.get('opts') or {})
    try:
        window = runner_timeouts(opts, worker.options_catalog())['context_window']
    except Exception:
        window = 0
    import prompt_locale
    locale = prompt_locale.of(opts)
    context_notice = prompt_locale.text(_RECAP_CONTEXT_NOTICE, locale)
    system = '\n\n'.join(str(x) for x in (worker.system_body(opts), opts.get('_grounding'),
                         prompt_locale.text(worker.PI_SAFETY, locale), context_notice) if x)
    _, checkpoint, rounds = connector.context({**opts,
        '_context_window': window, '_context_admission': True,
        '_require_context_budget': window > 0,
        '_current_tokens': estimate_context_tokens({'system': system, 'input': recap_prompt(snap)}),
        '_history_hint': context_notice,
    }, read_only=True, cancelled=run.cancelled.is_set)
    if run.cancelled.is_set() or not ad._recap_context_ready(sid, snap):
        return ''
    receipt = text_receipt(checkpoint)
    messages = [item for row in rounds for item in connector.encode(row)]
    if receipt:
        messages = native_items(checkpoint) + messages
    payload = {'id': binding['native_session_id'], 'messages': messages,
               'entries': None if receipt else (checkpoint or {}).get('payload', {}).get('native'),
               'fallbackReceipt': receipt}
    if snap.get('source') != getattr(worker, 'SRC_PEER', 'peer'):
        return _generate_unified_pi_affine(ad, connector, worker, sid, run, snap, opts, payload)
    env = _engine_env(worker, opts)
    with tempfile.TemporaryDirectory(prefix='shepherd-pi-peer-context-') as directory:
        source = os.path.join(directory, 'source.jsonl')
        request = os.path.join(directory, 'context.json')
        with open(request, 'x', encoding='utf-8') as stream:
            os.chmod(request, 0o600)
            json.dump(payload, stream, ensure_ascii=False)
        if run.cancelled.is_set():
            return ''
        process = NativeProcess([shutil.which('node'),
            str(Path(__file__).with_name('pi_recap_context.mjs')), pi_sdk_root(), request, source],
            cwd=connector.native_cwd, env=env, timeout=RECAP_TIMEOUT, hard_timeout=RECAP_TIMEOUT,
            cancel_frame={'type': 'cancel'}, on_process=run.bind,
            on_timeout=lambda: run.mark_expired(
                f'Pi 上下文准备超时 {RECAP_TIMEOUT}s', code='preparation_timeout'))
        prepared = False
        try:
            for event in process.events():
                if event.get('type') == 'prepared' and event.get('fallbackReceipt') == receipt:
                    prepared = True
        except Exception:
            if process.timed_out:
                run.expire(f'Pi 上下文准备超时 {RECAP_TIMEOUT}s', code='preparation_timeout')
                run.check_cancelled()
            raise
        finally:
            process.close()
            run.unbind(process.proc)
        if run.cancelled.is_set() or not ad._recap_context_ready(sid, snap):
            return ''
        if not prepared:
            raise RuntimeError('隔离问询工作上下文回读未确认')
        run.mark_prepared()
        private = {**snap, 'engine_sid': binding['native_session_id'],
                   'opts': {**opts, '_grounding': '\n\n'.join(
                       x for x in (opts.get('_grounding', ''), context_notice) if x)}}
        return type(worker).recap_generate(_WorkerView(worker, source), sid, run, private)


_RECAP_CONTEXT_NOTICE = ('此临时支线禁止工具。历史可能已压缩或省略；若所问精确内容不在当前'
                         '上下文中，明确说明无法确认，不猜测、不声称已经检索原始历史。')
# Below ClaudeNativeStore's per-transaction cap; a disposable view may be
# written in several bounded batches, an archive commit never is.
_VIEW_BATCH_BYTES = 8 * 1024 * 1024


def _generate_unified_claude(ad, connector, binding, sid, run, snap):
    """Recap from the same verified checkpoint + admitted tail as a normal turn.

    Projecting the whole archive into the disposable store grew without bound
    and failed with “批次过大” on long conversations. Read-only: no checkpoint,
    cursor or archive entry is written for the target conversation.
    """
    from engine_adapters.recap import _engine_env, recap_prompt
    from .context_budget import estimate_context_tokens
    from .execution_budget import runner_timeouts
    import prompt_locale
    worker = ad._worker('claude')
    opts = dict(snap.get('opts') or {})
    try:
        window = runner_timeouts(opts, worker.options_catalog())['context_window']
    except Exception:
        window = 0
    system = '\n\n'.join(str(x) for x in (worker.system_body(opts), opts.get('_grounding')) if x)
    _, checkpoint, rounds = connector.context({**opts,
        '_context_window': window, '_context_admission': True,
        '_require_context_budget': window > 0,
        '_current_tokens': estimate_context_tokens({'system': system, 'input': recap_prompt(snap)}),
        '_history_hint': prompt_locale.text(_RECAP_CONTEXT_NOTICE, prompt_locale.of(opts)),
    }, read_only=True, cancelled=run.cancelled.is_set)
    if run.cancelled.is_set() or not ad._recap_context_ready(sid, snap):
        return ''
    env = _engine_env(worker, opts)
    import claude_token
    env = claude_token.apply(env, log=claude_token.desk_log)
    with _admitted_claude_view(connector, checkpoint, rounds, opts) as derived:
        if run.cancelled.is_set():
            return ''
        if snap.get('source') == getattr(worker, 'SRC_PEER', 'peer'):
            run.mark_prepared()
        return _generate_native(derived, worker, binding, sid, run, snap, opts, env)


def _admitted_view_entries(derived, checkpoint, rounds):
    """Mirror UnifiedRunner's Claude execution view for a disposable binding."""
    from .claude_codec import encode_rounds
    from .context_fallback import native_items
    entries = [entry for entry in copy.deepcopy(native_items(checkpoint))
               if not (entry.get('type') == 'attachment'
                       and (entry.get('attachment') or {}).get('type') == 'prompt_snapshot')]
    for entry in entries:
        if 'sessionId' in entry:
            entry['sessionId'] = derived.native_session_id
    for row in rounds:
        entries.extend(encode_rounds(derived.binding, [row]))
    parent = None
    for entry in entries:
        if entry.get('type') != 'system' or entry.get('subtype') != 'compact_boundary':
            entry['parentUuid'] = parent
        parent = entry.get('uuid', parent)
    return entries


def _append_bounded(native, entries, limit=_VIEW_BATCH_BYTES):
    from .claude_store import canonical
    revision, batch, size = native.revision(), [], 0
    for entry in entries:
        cost = len(canonical(entry).encode('utf-8'))
        if batch and size + cost > limit:
            revision = native.append(batch, revision)
            batch, size = [], 0
        batch.append(entry)
        size += cost
    if batch:
        revision = native.append(batch, revision)
    return revision


@contextmanager
def _admitted_claude_view(connector, checkpoint, rounds, opts):
    from .service import Service
    with tempfile.TemporaryDirectory(prefix='shepherd-recap-view-') as root:
        temporary = Service(root)
        temporary.create('recap', connector.engine, connector.native_cwd)
        derived = temporary.connector(temporary.active('recap')['binding'])
        if hasattr(derived, 'configure_target'):
            derived.configure_target(opts)
        entries = _admitted_view_entries(derived, checkpoint, rounds)
        if not entries:
            raise RuntimeError('Recap 准入上下文为空')
        _append_bounded(derived.native, entries)
        derived._verify(entries)
        yield derived


def _unified_pi_args(worker, connector, opts):
    """与 Shepherd Pi 主回合相同的启动参数（engine_adapters/shepherd.py 的
    _turn_dispatch 与 UnifiedRunner._pi 的组合）：同一套扩展、工具定义和系统提示。"""
    from pi_extensions import append_system_prompt_if_missing, enforce_prompt_isolation
    from .history_prompt import history_prompt
    grounding = str(opts.get('_grounding') or '').strip()
    system = '\n\n'.join(x for x in (worker.system_body(opts), grounding) if x)
    args = enforce_prompt_isolation(list(worker.build_cmd('', None, opts)[1:]))
    args = append_system_prompt_if_missing(args, system)
    import prompt_locale
    return append_system_prompt_if_missing(args, history_prompt(connector, prompt_locale.of(opts)))


def _generate_unified_pi_affine(ad, connector, worker, sid, run, snap, opts, payload):
    """普通 recap：与主回合同一前缀和会话号，命中提示缓存；只拒绝执行工具。

    旧做法把上下文写成临时文件，再以 --fork、--no-tools 和改写过的系统提示生成。
    工具定义、系统提示和缓存键都与主回合不同，2026-10-03 实测 8 次 recap 共 60 万
    token 输入全部未命中缓存。现在启动与主回合相同的内存宿主，设置
    DESKTRON_PI_RECAP=1 让 pi_gate 拒绝全部工具、pi_autocompact 取消压缩，
    拿到第一条助手完成帧即结束。宿主只在内存中持有会话，不写归档。
    """
    from engine_adapters.recap import RECAP_TIMEOUT, _engine_env, recap_prompt
    from pi_extensions import without_legacy_web_secrets
    from .pi_connector import pi_sdk_root
    if not str(opts.get('_grounding') or '').strip():
        run.reason = 'Pi recap 缺少系统约定'
        return ''
    args = _unified_pi_args(worker, connector, opts)
    env = without_legacy_web_secrets({**_engine_env(worker, opts),
        'DESKTRON_ENGINE': 'pi', 'DESKTRON_PUBLIC_ENGINE': str(getattr(ad, 'name', '') or 'shepherd'),
        'DESKTRON_SESSION_ID': sid, 'DESKTRON_PI_RECAP': '1'})
    with tempfile.TemporaryDirectory(prefix='shepherd-pi-recap-') as directory:
        request = os.path.join(directory, 'payload.json')
        with open(request, 'x', encoding='utf-8') as stream:
            os.chmod(request, 0o600)
            json.dump({**payload, 'compact': False}, stream, ensure_ascii=False)
        if run.cancelled.is_set():
            return ''
        process = NativeProcess([shutil.which('node'),
            str(Path(__file__).with_name('pi_memory_rpc.mjs')), pi_sdk_root(), request, *args],
            cwd=connector.native_cwd, env=env, timeout=RECAP_TIMEOUT, hard_timeout=RECAP_TIMEOUT,
            cancel_frame={'type': 'abort'}, on_process=run.bind,
            on_timeout=lambda: run.mark_expired(f'Pi recap 超时 {RECAP_TIMEOUT}s', code='timeout'))
        message = None
        try:
            events = process.events()
            ready = next(events, {})
            if (ready.get('type') != 'protocol_ready'
                    or ready.get('sessionId') != payload['id']):
                raise RuntimeError('Pi recap 宿主初始化未确认')
            if payload.get('fallbackReceipt'):
                prepared = next(events, {})
                if (prepared.get('type') != 'fallback_prepared'
                        or prepared.get('fallbackReceipt') != payload['fallbackReceipt']):
                    raise RuntimeError('Pi recap 降级上下文回读未确认')
            if run.cancelled.is_set() or not ad._recap_context_ready(sid, snap):
                return ''
            process.send({'type': 'prompt', 'message': recap_prompt(snap)})
            for event in events:
                if (event.get('type') == 'message_end'
                        and (event.get('message') or {}).get('role') == 'assistant'):
                    message = event['message']
                    break
        finally:
            process.close()
            run.unbind(process.proc)
    if run.cancelled.is_set():
        return ''
    if message is None:
        run.reason = 'Pi 未返回助手完成帧'
        return ''
    usage = message.get('usage') or {}
    if isinstance(usage, dict):
        run.cache_usage = {key: usage[key] for key in ('input', 'output', 'cacheRead', 'cacheWrite')
                           if isinstance(usage.get(key), (int, float)) and not isinstance(usage[key], bool)}
    return worker._pi_recap_text(message, run)


@contextmanager
def _recap_connector(connector, rounds, opts, disposable=False):
    if not getattr(connector, 'unified', False) and not disposable:
        yield connector
        return
    # Disposable input for the existing no-tools recap protocols. These files
    # are never used as conversation authority and cannot affect the archive.
    from .service import Service
    with tempfile.TemporaryDirectory(prefix='shepherd-recap-view-') as root:
        temporary = Service(root)
        temporary.create('recap', connector.engine, connector.native_cwd)
        derived = temporary.connector(temporary.active('recap')['binding'])
        if hasattr(derived, 'configure_target'):
            derived.configure_target(opts)
        derived.append(rounds, derived.binding.get('native_revision', ''))
        yield derived


def _generate_native(connector, worker, binding, sid, run, snap, opts, env):
    from engine_adapters.recap import RECAP_TIMEOUT, recap_prompt
    binding = connector.binding
    if binding['engine'] == 'codex':
        from codex_appserver import recap_fork
        env['CODEX_HOME'] = connector.home
        peer = snap.get('source') == getattr(worker, 'SRC_PEER', 'peer')
        if run.cancelled.is_set():
            return ''
        return recap_fork(binding['native_session_id'], recap_prompt(snap),
                          cwd=connector.native_cwd, env=env, binary=connector.binary,
                          timeout=RECAP_TIMEOUT, cancel_run=run, read_only=True,
                          require_ephemeral=peer, no_tools=peer)
    with tempfile.TemporaryDirectory(prefix='shepherd-recap-') as directory:
        if binding['engine'] == 'pi':
            connector._safe_path(required=True)
            path = os.path.join(directory, 'source.jsonl')
            shutil.copyfile(connector.path, path)
            os.chmod(path, 0o600)
            private = {**snap, 'engine_sid':binding['native_session_id']}
            # The source copy can block after the public caller has yielded to a
            # real user turn.  Recheck at the final model boundary: a cancelled
            # preparation worker must never start a late provider process.
            if run.cancelled.is_set():
                return ''
            # Reuse Pi's existing no-tools fork implementation and provider setup.
            return type(worker).recap_generate(_WorkerView(worker, path), sid, run, private)
        connector._available()
        connector.native._safe_file()
        path = os.path.join(directory, 'source.sqlite3')
        with closing(sqlite3.connect(Path(connector.native.path).as_uri() + '?mode=ro', uri=True)) as source:
            with closing(sqlite3.connect(path)) as target:
                source.backup(target)
        os.chmod(path, 0o600)
        payload = {'sdkRoot':connector.sdk_root, 'dbPath':path,
                   'sessionId':binding['native_session_id'], 'cwd':connector.native_cwd,
                   'claudeBinary':connector.claude_binary, 'env':env,
                   'model':opts.get('model'), 'effort':opts.get('effort'),
                   'systemPrompt':'\n\n'.join(x for x in (
                       worker.system_body(opts), opts.get('_grounding')) if x),
                   'prompt':recap_prompt(snap)}
        input_path = os.path.join(directory, 'request.json')
        with open(input_path, 'x', encoding='utf-8') as stream:
            os.chmod(input_path, 0o600)
            json.dump(payload, stream, ensure_ascii=False)
        if run.cancelled.is_set():
            return ''
        process = NativeProcess([connector.node_binary,
            str(Path(__file__).with_name('claude_recap.mjs')), input_path],
            cwd=connector.native_cwd, env=env, timeout=RECAP_TIMEOUT,
            cancel_frame={'type':'cancel'}, on_process=run.bind)
        answer = ''
        try:
            if run.cancelled.is_set(): return ''
            for event in process.events():
                if event.get('type') == 'recap' and event.get('ok'):
                    answer = str(event.get('text') or '')
            return '' if run.cancelled.is_set() else answer
        finally:
            process.close()
            run.unbind(process.proc)
