"""Durable delivery receipts, not a second transcript or a replay queue."""
import hashlib
import json
import os
import time

import durable_json
from .recovery import BOOT_ID
from .store import Conflict, RequestBusy, RequestPending, valid_id


class Requests:
    def __init__(self, root, boot_id=BOOT_ID):
        self.root, self.boot_id = root, boot_id

    def path(self, sid):
        return os.path.join(self.root, 'requests', valid_id(sid), 'receipts.json')

    @staticmethod
    def public(row):
        return {key: row[key] for key in ('request_id', 'status', 'reason', 'retryable') if key in row}

    def get(self, sid, rid):
        from .physical_delete import is_deleted
        if is_deleted(self.root, sid):
            return {'request_id':rid, 'status':'rejected', 'reason':'conversation_deleted'}
        row = durable_json.read(self.path(sid)).get(valid_id(rid))
        return self.public(row) if row else {'request_id':rid, 'status':'not_found'}

    def blocking(self, sid):
        """Body-free admission fence shared by claim and public turn state."""
        rows = durable_json.read(self.path(sid))
        return next((self.public(row) for row in rows.values()
                     if isinstance(row, dict) and row.get('status') in
                     ('accepted', 'preparing', 'started', 'uncertain')), None)

    def claim(self, sid, rid, payload):
        valid_id(rid)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True,
            ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
        path = self.path(sid)
        from .physical_delete import deletion_guard, is_deleted
        with deletion_guard(self.root, sid):
            if is_deleted(self.root, sid):
                raise Conflict('Shepherd Conversation 已删除，消息未发送')
            with durable_json.locked(path + '.transaction'):
                rows = durable_json.read(path)
                if rows.get('_conversation_deleted'):
                    raise Conflict('Shepherd Conversation 已删除，消息未发送')
                old = rows.get(rid)
                if old:
                    if old.get('fingerprint') not in (None, fingerprint):
                        raise Conflict('同一请求 ID 的内容不一致，已拒绝重复发送')
                    return self.public(old), False
                open_states = {row.get('status') for row in rows.values() if isinstance(row, dict)
                               and row.get('status') in ('accepted','preparing','started','uncertain')}
                if 'uncertain' in open_states:
                    raise RequestBusy('目标会话上一条请求尚未收口，消息未接收；请等待确认后再发送')
                if open_states:
                    # Same refusal, but it settles on its own: the client may keep
                    # the message queued and resend after the next ready boundary.
                    raise RequestPending('目标会话上一条请求尚未收口，消息未接收；将在上一轮结束后重新发送')
                rows[rid] = {'request_id':rid, 'fingerprint':fingerprint, 'status':'accepted',
                             'boot_id':self.boot_id, 'updated_at':time.time()}
                durable_json.write(path, rows)
                return self.public(rows[rid]), True

    def update(self, sid, rid, status=None, **fields):
        from .physical_delete import deletion_guard, is_deleted
        path = self.path(sid)
        with deletion_guard(self.root, sid):
            if is_deleted(self.root, sid):
                return {'request_id':rid, 'status':'rejected', 'reason':'conversation_deleted'}
            with durable_json.locked(path + '.transaction'):
                rows = durable_json.read(path)
                row = rows[valid_id(rid)]
                if row['status'] == 'cancelled':
                    if status in ('preparing', 'started'):
                        raise Conflict('请求已在进入引擎前取消')
                    return self.public(row)
                if status: row['status'] = status
                row.update(fields, updated_at=time.time())
                durable_json.write(path, rows)
                return self.public(row)

    def cancel(self, sid, rid, *, only_before_start=False):
        from .physical_delete import deletion_guard, is_deleted
        path = self.path(sid)
        with deletion_guard(self.root, sid):
            if is_deleted(self.root, sid):
                return {'request_id':rid, 'status':'rejected', 'reason':'conversation_deleted'}
            with durable_json.locked(path + '.transaction'):
                rows = durable_json.read(path)
                if not rid:
                    rid = next((key for key,row in rows.items() if isinstance(row, dict)
                                and row.get('status') in ('accepted','preparing','started','uncertain')), '')
                if not rid: return None
                row = rows.setdefault(valid_id(rid), {'request_id':rid, 'boot_id':self.boot_id,
                                                      'status':'accepted'})
                retryable = row['status'] == 'accepted' or (row['status'] == 'rejected' and row.get('retryable') is True)
                if only_before_start and not retryable:
                    return self.public(row)
                if retryable:
                    row.update(status='cancelled', reason='用户在派发前取消')
                row['cancel_requested'] = True
                row.pop('retryable', None)
                durable_json.write(path, rows)
                return {**self.public(row), "retryable": bool(only_before_start)}

    def cancelled(self, sid, rid):
        from .physical_delete import is_deleted
        if is_deleted(self.root, sid):
            return True
        row = durable_json.read(self.path(sid)).get(rid) or {}
        return bool(row.get('cancel_requested'))

    def reject_deleted(self, sid):
        """Finalize every nonterminal delivery receipt for a deleted Conversation."""
        path = self.path(sid)
        with durable_json.locked(path + '.transaction'):
            rows = durable_json.read(path)
            changed = not rows.get('_conversation_deleted')
            rows['_conversation_deleted'] = {'deleted':True, 'updated_at':time.time()}
            for row in rows.values():
                if (isinstance(row, dict)
                        and row.get('status') in ('accepted','preparing','started','uncertain')):
                    row.update(status='rejected', reason='conversation_deleted',
                               updated_at=time.time())
                    changed = True
            if changed:
                durable_json.write(path, rows)
        return changed

    def settle(self, sid, rid, state, *, error='', crashed=False):
        from .physical_delete import deletion_guard, is_deleted
        path = self.path(sid)
        with deletion_guard(self.root, sid):
            if is_deleted(self.root, sid):
                return
            with durable_json.locked(path + '.transaction'):
                rows = durable_json.read(path)
                row = rows.get(rid)
                if not row or row['status'] not in ('accepted','preparing','started','uncertain'): return
                if (state or {}).get('tombstone'):
                    row.update(status='rejected', reason='conversation_deleted',
                               updated_at=time.time())
                    durable_json.write(path, rows)
                    return
                attempt = row.get('attempt_id')
                turns = (state or {}).get('turns')
                if not isinstance(turns, list):
                    turns = (state or {}).get('rounds', [])
                settled = next((r for r in turns
                                if attempt and r.get('attempt_id') == attempt), None)
                active = ((state or {}).get('active_turn')
                          or (state or {}).get('inflight') or {})
                events = (state or {}).get('audit_events')
                if not isinstance(events, list):
                    events = (state or {}).get('reconciliations', [])
                audited = next((r for r in events if attempt and (
                    r.get('attempt_id') == attempt
                    or (r.get('inflight') or {}).get('attempt_id') == attempt)), None)
                if settled:
                    outcome = settled.get('outcome') or settled.get('terminal_state') or 'completed'
                    row.update(status='finished', reason=outcome,
                               turn_id=settled.get('turn_id') or settled.get('round_id') or '',
                               history_state=settled.get('history_state') or 'available')
                elif attempt and active.get('attempt_id') == attempt:
                    row.update(status='uncertain', reason='原生结果待核验，禁止自动重跑')
                elif audited:
                    row.update(status='finished', reason=(audited.get('kind')
                                                          or audited.get('action')
                                                          or 'reconciled'))
                elif attempt and (state or {}).get('conversation_id') == sid:
                    # A validated, complete ledger has neither this reservation,
                    # a commit nor a quarantine record: admission stopped before
                    # begin_round. The harness cannot dispatch without that record.
                    # The public user bubble may already exist; do not duplicate it
                    # into a draft or claim that a native turn completed.
                    row.update(status='finished', reason='failed_before_dispatch')
                elif row['status'] == 'accepted':
                    row.update(status='rejected', reason=error or '进程退出前尚未进入回合，请重新发送',
                               retryable=bool(crashed and not error))
                elif crashed and not attempt:
                    row.update(status='rejected', reason='进程退出前尚未进入执行闸，请重新发送')
                elif crashed:
                    row.update(status='uncertain', reason='进程异常退出，回合结果待核验')
                else:
                    row.update(status='finished', reason=error or 'failed_recoverable')
                durable_json.write(path, rows)

    def recover(self, sid, state):
        rows = durable_json.read(self.path(sid))
        for rid, row in rows.items():
            if rid != '_conversation_deleted' and row.get('boot_id') != self.boot_id:
                self.settle(sid, rid, state, crashed=True)

    def reconcile(self, sid, state):
        """Settle only receipts already linked to durable Coordinator attempts.

        This is safe during the current boot: a freshly accepted request has no
        attempt_id and must not be mistaken for a pre-dispatch crash.
        """
        rows = durable_json.read(self.path(sid))
        for rid, row in rows.items():
            if (rid != '_conversation_deleted' and row.get('attempt_id') and row.get('status')
                    in ('preparing', 'started', 'uncertain')):
                self.settle(sid, rid, state)

    def finish_reconciliation(self, sid, attempt_id):
        """Compatibility release for an explicitly omitted unverifiable turn."""
        path = self.path(sid)
        with durable_json.locked(path + '.transaction'):
            rows = durable_json.read(path)
            for row in rows.values():
                if (isinstance(row, dict) and row.get('status') == 'uncertain'
                        and row.get('attempt_id') == attempt_id):
                    row.update(status='finished', reason='omitted_unknown')
            durable_json.write(path, rows)
