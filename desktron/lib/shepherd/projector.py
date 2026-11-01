"""Verified projection from serial Turn source references."""
import copy

from .coordinator import digest
from .source_codec import UNKNOWN_TOOL_OUTPUT
from .store import Conflict, InvalidState
from .turn_inputs import TurnInputs, has_native_user_input


class ReadProjector:
    # Bound temporary native results; estimated tokens include image payloads.
    # A single existing turn is indivisible and can exceed the estimate budget.
    BATCH_TURNS = 32
    BATCH_TOKENS = 262144

    def __init__(self, store, resolver):
        self.store = store
        self.resolver = resolver

    def project(self, conversation_id, start_offset=1, end_offset=None, cancelled=None):
        state = self.store.load_raw(conversation_id)
        if state is None:
            raise InvalidState('Shepherd Conversation 不存在')
        end = len(state['turns']) if end_offset is None else end_offset
        if (not isinstance(start_offset,int) or not isinstance(end,int)
                or start_offset < 1 or end < start_offset - 1 or end > len(state['turns'])):
            raise InvalidState('Shepherd 投影范围无效')
        output = []
        for row, resolved, trusted in self._read_rows(state['turns'], start_offset - 1, end, cancelled):
            if callable(cancelled) and cancelled():
                raise Conflict('Shepherd 引擎准备已被更新的选择取消')
            public = self._public(row, resolved=resolved, trusted_input=trusted)
            output.append({'seq':row['seq'],'offset':row['seq'],
                           'turn_id':row['turn_id'],'round_id':row['turn_id'],
                           'turn_hash':row['turn_hash'],'entry_hash':row['turn_hash'],
                           'kind':row['kind'],'outcome':row['outcome'],
                           'terminal_state':self._legacy_terminal(row['outcome']),
                           'history_state':row['history_state'],
                           'unfinished_tool_ids':copy.deepcopy(row.get('unfinished_tool_ids') or []),
                           'settled_at':row.get('settled_at',0),
                           'committed_at':row.get('settled_at',0),
                           'public':public})
            del resolved, trusted
        return output

    def _read_rows(self, rows, start, end, cancelled):
        read_batch = getattr(self.resolver, 'resolve_batch', None)
        while start < end:
            if callable(cancelled) and cancelled():
                raise Conflict('Shepherd 引擎准备已被更新的选择取消')
            row = rows[start]
            if row['history_state'] == 'omitted' or not callable(read_batch):
                yield row, None, None
                start += 1
                continue
            source = row['source_ref']
            key = (source['engine'], source['session_id'])
            stop, tokens = start, 0
            while stop < end and stop - start < self.BATCH_TURNS:
                candidate = rows[stop]
                ref = candidate.get('source_ref') or {}
                if candidate['history_state'] == 'omitted' or (ref.get('engine'), ref.get('session_id')) != key:
                    break
                size = candidate.get('estimated_tokens', 0)
                if stop > start and tokens + size > self.BATCH_TOKENS:
                    break
                tokens += size
                stop += 1
            requests = [(rows[i]['source_ref'], self._input(rows[i])) for i in range(start, stop)]
            results = read_batch(requests)
            if not isinstance(results, list) or len(results) != stop - start:
                raise InvalidState('Shepherd Connector 来源批次长度不匹配')
            for i, result in enumerate(results):
                yield rows[start + i], result, requests[i][1]
            # Do not retain the previous batch while allocating the next one.
            del results, requests, result
            start = stop

    def _public(self, row, *, resolved=None, trusted_input=None):
        if row['history_state'] == 'omitted':
            reason = str(row.get('recovery_reason') or 'native source unavailable')
            detail = ' '.join(str(row.get('recovery_detail') or '').split())[:200]
            if detail:
                # Name the concrete cause (e.g. the unknown record type) so a
                # repeated format drift is recognisable from the transcript.
                reason += '（' + detail + '）'
            public = self._input(row)
            public.append({'type':'message','role':'assistant','content':[{
                'type':'output_text',
                'text':'[本轮内容已隔离] 原生内容无法安全验证；执行结果未知，不得自动重试。原因：' + reason}]})
            return public
        owned = resolved is not None
        if not owned:
            trusted_input = self._input(row)
            resolve_with_input = getattr(self.resolver, 'resolve_with_input', None)
            resolved = (resolve_with_input(copy.deepcopy(row['source_ref']), trusted_input or None)
                        if callable(resolve_with_input)
                        else self.resolver.resolve(copy.deepcopy(row['source_ref'])))
        if not isinstance(resolved,dict) or set(resolved) != {'source','public'}:
            raise InvalidState('Shepherd Resolver 返回格式无效')
        if digest(resolved['source']) != row['source_hash']:
            raise Conflict('Shepherd 来源内容 Hash 不匹配')
        # Batch results transfer ownership. Only legacy resolvers need a copy;
        # synthetic closures below append new items without changing old ones.
        public = resolved['public'] if owned else copy.deepcopy(resolved['public'])
        if not isinstance(public, list):
            if digest(public) != row['public_hash']:
                raise Conflict('Shepherd 公共投影 Hash 不匹配')
            return public
        # Whether the durable TurnInputs record was prepended is a write-time
        # decision committed by public_hash. The detection rule has changed
        # across releases (and differs between writer paths), so replay must
        # not re-decide it: try the current rule first, then the only other
        # layout. Both candidates derive from the hash-verified source plus the
        # same durable input, and exactly the committed one is accepted.
        prefixed = not has_native_user_input(public, resolved['source'])
        layouts = [prefixed] + ([not prefixed] if trusted_input else [])
        for layout in layouts:
            candidate = self._closed((list(trusted_input) if layout and trusted_input else [])
                                     + public, row)
            if digest(candidate) == row['public_hash']:
                return candidate
        raise Conflict('Shepherd 公共投影 Hash 不匹配')

    @staticmethod
    def _closed(public, row):
        # Recovery receipts describe calls that were synthetically closed.  A
        # connector may return the raw native projection, so reproduce those
        # closures deterministically without changing the source hash.
        existing = {(item.get('type'),str(item.get('call_id') or ''))
                    for item in public if isinstance(item,dict)}
        call_kind = {str(item.get('call_id') or ''):item.get('type')
                    for item in public if isinstance(item,dict)
                    and item.get('type') in ('function_call','custom_tool_call')}
        for call_id in row.get('unfinished_tool_ids') or []:
            kind = call_kind.get(call_id)
            output_kind = kind + '_output' if kind else ''
            if not output_kind or (output_kind,call_id) in existing:
                continue
            public.append({'type':output_kind,'call_id':call_id,'output':UNKNOWN_TOOL_OUTPUT})
        return public

    def _input(self, row):
        reference = row.get('input_ref')
        if not reference:
            return []
        return TurnInputs(self.store.root).read(copy.deepcopy(reference))

    def checkpoint(self, checkpoint):
        source = checkpoint.get('source')
        resolved = self.resolver.resolve(copy.deepcopy(source))
        if not isinstance(resolved,dict) or set(resolved) != {'source','public'}:
            raise InvalidState('Shepherd Resolver 返回格式无效')
        if digest(resolved['source']) != checkpoint['source_hash']:
            raise Conflict('Shepherd Checkpoint 来源 Hash 不匹配')
        if digest(resolved['public']) != checkpoint['public_hash']:
            raise Conflict('Shepherd Checkpoint 投影 Hash 不匹配')
        return copy.deepcopy(resolved['public'])

    @staticmethod
    def _legacy_terminal(outcome):
        return {'completed':'completed','failed':'failed_recoverable',
                'interrupted':'interrupted_confirmed','unknown':'unknown'}[outcome]
