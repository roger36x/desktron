"""Portable checkpoint registry and token-budget resume planning."""
import copy
import time
import uuid

from .coordinator import valid_hash
from .store import Conflict, InvalidState, valid_id


class Checkpoints:
    def __init__(self, store):
        self.store = store

    def register(self, conversation_id, covers_through_offset, *, source, source_hash,
                 public_hash, estimated_tokens, projection_codec_version,
                 portability='portable', checkpoint_id=None):
        """Register a checkpoint over serial turns.

        The public argument name remains compatible while callers migrate; the
        persisted shape uses ``covers_through_seq`` and ``covers_turn_hash``.
        """
        if portability not in ('local', 'portable'):
            raise InvalidState('Shepherd Checkpoint portability 无效')
        if not isinstance(estimated_tokens, int) or estimated_tokens < 0:
            raise InvalidState('Shepherd Checkpoint Token 估算无效')
        source = self._source(source)
        checkpoint_id = valid_id(checkpoint_id or ('checkpoint_' + uuid.uuid4().hex), 'checkpoint_id')
        codec = valid_id(projection_codec_version, 'projection_codec_version')
        with self.store.change(conversation_id) as state:
            count = len(state['turns'])
            if (not isinstance(covers_through_offset, int)
                    or not 0 <= covers_through_offset <= count):
                raise InvalidState('Shepherd Checkpoint 覆盖范围无效')
            covers_hash = ('' if covers_through_offset == 0
                           else state['turns'][covers_through_offset - 1]['turn_hash'])
            value = {
                'checkpoint_id':checkpoint_id,
                'covers_through_seq':covers_through_offset,
                'covers_turn_hash':covers_hash,
                'source':source,
                'source_hash':valid_hash(source_hash, 'source_hash'),
                'public_hash':valid_hash(public_hash, 'public_hash'),
                'estimated_tokens':estimated_tokens,
                'projection_codec_version':codec,
                'portability':portability,
                'created_at':time.time(),
            }
            old = state['checkpoints'].get(checkpoint_id)
            if old is not None:
                stable = {key:item for key,item in value.items() if key != 'created_at'}
                old_stable = {key:item for key,item in old.items() if key != 'created_at'}
                if old_stable == stable:
                    return self._legacy(old)
                raise Conflict('Shepherd Checkpoint ID 已绑定其他内容')
            state['checkpoints'][checkpoint_id] = value
            return self._legacy(value)

    def plan(self, conversation_id, applied_offset, *, working_limit, reserve_tokens=0,
             current_tokens=0, projection_codec_version=None):
        state = self.store.load_raw(conversation_id)
        if state is None:
            raise InvalidState('Shepherd Conversation 不存在')
        values = (applied_offset, working_limit, reserve_tokens, current_tokens)
        count = len(state['turns'])
        if (not all(isinstance(value, int) for value in values) or applied_offset < 0
                or applied_offset > count or working_limit <= 0
                or reserve_tokens < 0 or current_tokens < 0):
            raise InvalidState('Shepherd Resume 预算无效')
        tail = lambda after: sum(row.get('estimated_tokens', 0) for row in state['turns'][after:])
        direct_cost = current_tokens + tail(applied_offset) + reserve_tokens
        if direct_cost <= working_limit:
            return self._legacy_plan({'strategy':'incremental','from_seq':applied_offset + 1,
                    'through_seq':count,'estimated_tokens':direct_cost})
        candidates = [row for row in state['checkpoints'].values()
                      if row['portability'] == 'portable'
                      and row['covers_through_seq'] >= applied_offset
                      and (projection_codec_version is None
                           or row['projection_codec_version'] == projection_codec_version)]
        candidates.sort(key=lambda row:(row['covers_through_seq'],row['created_at']), reverse=True)
        for checkpoint in candidates:
            cost = checkpoint['estimated_tokens'] + tail(checkpoint['covers_through_seq']) + reserve_tokens
            if cost <= working_limit:
                return self._legacy_plan({'strategy':'checkpoint','checkpoint':copy.deepcopy(checkpoint),
                        'from_seq':checkpoint['covers_through_seq'] + 1,
                        'through_seq':count,'estimated_tokens':cost})
        return self._legacy_plan({'strategy':'blocked','reason':'目标工作水位不足且没有可用的 Portable Checkpoint',
                'from_seq':applied_offset + 1,'through_seq':count,
                'estimated_tokens':direct_cost})

    @classmethod
    def _legacy_plan(cls, plan):
        out = copy.deepcopy(plan)
        out['from_offset'] = out['from_seq']
        out['through_offset'] = out['through_seq']
        if isinstance(out.get('checkpoint'), dict):
            out['checkpoint'] = cls._legacy(out['checkpoint'])
        return out

    @staticmethod
    def _legacy(row):
        out = copy.deepcopy(row)
        out['covers_through_offset'] = out['covers_through_seq']
        out['covers_hash'] = out['covers_turn_hash']
        return out

    @staticmethod
    def _source(source):
        if not isinstance(source, dict):
            raise InvalidState('Shepherd Checkpoint 来源无效')
        allowed = {'kind','engine','session_id','entry_id','source_key'}
        if set(source) - allowed:
            raise InvalidState('Shepherd Checkpoint 来源含正文或未知字段')
        return {key:valid_id(source.get(key),'checkpoint.source.' + key) for key in allowed}
