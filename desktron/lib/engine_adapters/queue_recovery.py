"""One-time evacuation of the retired backend queue. No enqueue or dispatcher.

Claim -> browser durable save -> acknowledge. Records/blobs remain recoverable;
unknown engine delivery is never exported as a new prompt.
"""
import hashlib
import json
import os
import re
import durable_json


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,160}', value):
        raise ValueError('恢复编号无效')
    return value


def _path(adapter, sid):
    return os.path.join(adapter.dir, 'message_queue', _id(sid), 'queue.json')


def request_status(adapter, sid, rid):
    """Preserve old upload identities, including delayed POSTs after rollback."""
    if not getattr(adapter, "dir", None):
        return None
    data = durable_json.read_value(_path(adapter, sid))
    for row in (data.get('items') or {}).values():
        if rid in (row.get('client_request_id'), row.get('request_id')) or rid in row.get('edit_requests', []):
            return {'request_id':rid, 'status':'cancelled' if row['state'] == 'cancelled' else 'finished'}
    if rid in data.get('cancelled_requests', []):
        return {'request_id':rid, 'status':'cancelled'}
    return None


def _receipt(adapter, sid, rid):
    store = getattr(adapter, 'requests', None) if adapter.name == 'shepherd' else getattr(adapter, 'delivery', None)
    return store.get(sid, rid) if store and rid else {'status':'unknown'}


def recover(adapter, sid, owner, ack=''):
    owner = _id(owner)
    path = _path(adapter, sid)
    if not os.path.isfile(path):
        return {'items':[], 'token':''}
    with durable_json.locked(path + '.transaction'):
        data = durable_json.read(path)
        if ack:
            if data.get('recovery_ack') == ack and data.get('recovery_owner') == owner:
                return {'ok':True}
            claim = data.get('recovery_claim') or {}
            if claim.get('owner') != owner or claim.get('token') != ack:
                raise ValueError('旧队列恢复凭据不匹配')
            for iid in claim['ids']:
                row = data['items'][iid]
                row.update(state='cancelled', reason='restored_to_browser', revision=row['revision'] + 1)
            data.update(recovery_ack=ack, recovery_claim=None)
            durable_json.write(path, data)
            return {'ok':True}
        previous = data.get('recovery_owner')
        if previous and previous != owner and data.get('recovery_claim'):
            raise ValueError('旧待发消息正在原浏览器恢复，请返回原浏览器完成')
        items, blocked = [], 0
        for row in sorted((data.get('items') or {}).values(), key=lambda r:r.get('position', 0)):
            state = row.get('state')
            safe = state in ('queued', 'editing', 'rejected')
            if state in ('dispatching', 'blocked'):
                receipt = _receipt(adapter, sid, row.get('request_id'))
                safe = receipt.get('status') in ('not_found', 'rejected', 'cancelled') or (
                    receipt.get('status') == 'finished' and receipt.get('reason') == 'failed_before_dispatch')
                if not safe and receipt.get('status') != 'finished':
                    blocked += 1
            if not safe:
                continue
            atts = []
            for a in row.get('attachments') or []:
                att = {k:v for k,v in a.items() if k not in ('sha256', 'size')}
                if a.get('attachment_id'):
                    att['url'] = f'/session/{sid}/queue-recovery/{row["item_id"]}/{a["attachment_id"]}'
                atts.append(att)
            items.append({'id':row['item_id'], 'text':row.get('text',''), 'atts':atts,
                          'request_id':row.get('client_request_id'), 'state':state})
        if not items:
            return {'items':[], 'token':'', 'blocked':blocked}
        token = hashlib.sha256(json.dumps(items, sort_keys=True).encode()).hexdigest()
        data.update(recovery_owner=owner, recovery_claim={'owner':owner, 'token':token, 'ids':[x['id'] for x in items]})
        durable_json.write(path, data)
        return {'items':items, 'token':token, 'blocked':blocked}


def attachment(adapter, sid, iid, aid):
    path = _path(adapter, sid)
    row = (durable_json.read_value(path).get('items') or {}).get(_id(iid), {})
    if not any(a.get('attachment_id') == _id(aid) for a in row.get('attachments', [])):
        return ''
    return os.path.join(os.path.dirname(path), 'blobs', aid + '.bin')


def available(adapter, sid):
    data = durable_json.read_value(_path(adapter, sid))
    for row in (data.get('items') or {}).values():
        state = row.get('state')
        if state in ('queued', 'editing', 'rejected'):
            return True
        if state in ('dispatching', 'blocked'):
            receipt = _receipt(adapter, sid, row.get('request_id'))
            safe = receipt.get('status') in ('not_found', 'rejected', 'cancelled') or (
                receipt.get('status') == 'finished'
                and receipt.get('reason') == 'failed_before_dispatch')
            if safe or receipt.get('status') != 'finished':
                return True
    return False
