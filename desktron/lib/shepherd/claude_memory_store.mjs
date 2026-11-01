/** Native SessionStore semantics with durable acknowledgements from the host journal.
 *
 * Keep one compact serialized owner per entry.  The SDK receives fresh objects
 * from load(); the bridge uses size/slice/checkpointEntries and never clones the
 * whole history merely to learn a boundary or revision.
 */
import {createHash, randomUUID} from 'node:crypto';

function encode(entry) {
  const payload=JSON.stringify(entry);
  if (typeof payload!=='string') throw new Error('invalid memory SessionStore entry');
  return payload;
}
function decode(payload) { return JSON.parse(payload); }
function mirrorEquivalent(oldPayload,newPayload) {
  if(oldPayload===newPayload)return true;
  if(typeof oldPayload!=='string' || typeof newPayload!=='string')return false;
  const oldEntry=decode(oldPayload),newEntry=decode(newPayload);
  if(!oldEntry || !newEntry || typeof oldEntry!=='object' || typeof newEntry!=='object')return false;
  const oldNormalized={...oldEntry},newNormalized={...newEntry};
  delete oldNormalized.slug;delete newNormalized.slug;
  return JSON.stringify(oldNormalized)===JSON.stringify(newNormalized);
}
function conflictError(uuid, oldPayload, newPayload) {
  const digest=value=>createHash('sha256').update(value).digest('hex').slice(0,12);
  const oldEntry=decode(oldPayload),newEntry=decode(newPayload);
  const keys=[...new Set([...Object.keys(oldEntry || {}),...Object.keys(newEntry || {})])];
  const changed=keys.filter(key=>JSON.stringify(oldEntry?.[key])!==JSON.stringify(newEntry?.[key]));
  return new Error(`memory SessionStore UUID conflict: uuid=${uuid || '<missing>'} `
    +`kind=${String(oldEntry?.type || '')}/${String(oldEntry?.subtype || '')} `
    +`changed=${changed.join(',')} old=${oldPayload.length}:${digest(oldPayload)} `
    +`new=${newPayload.length}:${digest(newPayload)}`);
}
function payloadRevision(payloads) {
  const hash=createHash('sha256');hash.update('[');
  for(let i=0;i<payloads.length;i++){if(i)hash.update(',');hash.update(payloads[i]);}
  hash.update(']');return hash.digest('hex');
}

export function claudeEntriesRevision(entries) {
  return payloadRevision((entries || []).map(encode));
}

export function claudeStoreTransitionRevision(baseRevision, entryPayloads) {
  if(typeof baseRevision!=='string' || (baseRevision!=='' && !/^[0-9a-f]{64}$/.test(baseRevision)))
    throw new Error('Claude SessionStore base revision invalid');
  if(!Array.isArray(entryPayloads) || entryPayloads.some(payload=>typeof payload!=='string'))
    throw new Error('Claude SessionStore transition payloads invalid');
  const hash=createHash('sha256');
  hash.update('claude-store-transition-v1\0');hash.update(baseRevision);hash.update('\0');
  hash.update('[');
  for(let i=0;i<entryPayloads.length;i++){if(i)hash.update(',');hash.update(entryPayloads[i]);}
  hash.update(']');return hash.digest('hex');
}

export class ShepherdClaudeMemoryStore {
  constructor(entries, sessionId, input, {ackTimeoutMs=30000}={}) {
    this.sessionId=sessionId;this.pending=new Map();this.recording=false;this.sealed=false;
    this.predispatch=null;this.predispatchByUuid=null;
    this.ackTimeoutMs=Math.max(1,Number(ackTimeoutMs)||30000);this.poisoned=null;
    this.payloads=[];this.byUuid=new Map();this._revision=null;
    this.reset(entries || []);
    const poison=error=>{
      this.poisoned=error instanceof Error?error:new Error(String(error));
      for(const done of this.pending.values())done(this.poisoned);
      this.pending.clear();
    };
    this.poison=poison;
    input.on('line', line => {
      let value; try {value=JSON.parse(line);} catch {return;}
      if (value.type==='archive_ack' && this.pending.has(value.id)) {
        this.pending.get(value.id)(); this.pending.delete(value.id);
      } else if (value.type==='cancel' && this.pending.size) {
        poison(new Error('archive acknowledgement cancelled'));
      }
    });
    input.on('close', () => poison(new Error('archive host disconnected')));
    process.stdout.on?.('error', poison);
  }
  check(key) {
    if (key?.sessionId!==this.sessionId || key.subpath!==undefined)
      throw new Error('memory SessionStore identity mismatch');
  }
  reset(entries=[]) {
    this.payloads=[];this.byUuid=new Map();this._revision=null;
    for (const entry of entries) this._store(entry,false);
  }
  _store(entry, dedupe=true) {
    const payload=encode(entry),uuid=typeof entry?.uuid==='string'?entry.uuid:'';
    if (uuid && dedupe) {
      const old=this.byUuid.get(uuid);
      if (old!==undefined) {
        if (!mirrorEquivalent(old,payload)) throw conflictError(uuid,old,payload);
        return null;
      }
    }
    this.payloads.push(payload);if(uuid)this.byUuid.set(uuid,payload);
    this._revision=null;
    return payload;
  }
  size() { return this.payloads.length; }
  slice(start=0) {
    const out=[];
    for(let i=Math.max(0,start);i<this.payloads.length;i++)out.push(decode(this.payloads[i]));
    return out;
  }
  payloadSlice(start=0) { return this.payloads.slice(Math.max(0,start)); }
  lastUuid() {
    for(let i=this.payloads.length-1;i>=0;i--){const row=decode(this.payloads[i]);if(row?.uuid)return row.uuid;}
    return undefined;
  }
  promptSnapshot() {
    for(const payload of this.payloads){const row=decode(payload);
      if(row?.type==='attachment'&&row.attachment?.type==='prompt_snapshot')return row;}
    return undefined;
  }
  checkpointEntries() {
    let boundary=-1,snapshot=-1;
    for(let i=0;i<this.payloads.length;i++){
      const row=decode(this.payloads[i]);
      if(row?.type==='attachment'&&row.attachment?.type==='prompt_snapshot')snapshot=i;
      if(row?.type==='system'&&row.subtype==='compact_boundary')boundary=i;
    }
    if(boundary<0)return null;
    const out=[];if(snapshot>=0)out.push(decode(this.payloads[snapshot]));
    for(let i=boundary;i<this.payloads.length;i++)out.push(decode(this.payloads[i]));
    return out;
  }
  healthy() { if(this.poisoned)throw this.poisoned; }
  seal() {
    this.healthy();
    if(this.pending.size) throw new Error('Claude SessionStore has unacknowledged writes');
    this.recording=false;this.predispatch=null;this.predispatchByUuid=null;this.sealed=true;
  }
  beginPredispatch() {
    this.healthy();
    if(!this.sealed || this.recording || this.predispatch!==null)
      throw new Error('Claude SessionStore pre-dispatch state invalid');
    this.sealed=false;this.predispatch=[];this.predispatchByUuid=new Map();
  }
  async _archive(entries) {
    if(!entries.length)return;
    const id=randomUUID(),entryPayloads=entries.map(encode);
    await new Promise((resolve,reject)=>{
      const fail=error=>{this.pending.delete(id);this.poisoned=error;reject(error);};
      const timer=setTimeout(()=>fail(new Error('archive acknowledgement timeout')),this.ackTimeoutMs);
      this.pending.set(id,error=>{clearTimeout(timer);error?fail(error):resolve();});
      try { process.stdout.write(JSON.stringify({type:'archive',id,entries,entryPayloads})+'\n'); }
      catch(error) { clearTimeout(timer);fail(error); }
    });
  }
  beginTurn() {
    this.healthy();
    if(this.predispatch===null) throw new Error('Claude SessionStore pre-dispatch state missing');
    const staged=this.predispatch;this.predispatch=null;this.predispatchByUuid=null;
    const buffered=[];
    for(const payload of staged){const added=this._store(decode(payload));if(added!==null)buffered.push(decode(added));}
    this.recording=true;
    return this._archive(buffered);
  }
  async append(key, entries) {
    this.check(key);this.healthy();
    // Bootstrap may populate the store before its first seal. After a durable
    // result, only byte-identical UUID duplicates are harmless. Reject before
    // mutating, even when a duplicate precedes a new entry in the same batch.
    if(this.sealed && entries.some(entry=>!entry?.uuid
        || !mirrorEquivalent(this.byUuid.get(entry.uuid),encode(entry)))) {
      this.poison(new Error('Claude SessionStore write outside an active turn'));
      this.healthy();
    }
    if(this.predispatch!==null) {
      for(const entry of entries){
        const payload=encode(entry),uuid=typeof entry?.uuid==='string'?entry.uuid:'';
        const old=uuid ? (this.predispatchByUuid.get(uuid) ?? this.byUuid.get(uuid)) : undefined;
        if(old!==undefined){if(!mirrorEquivalent(old,payload))throw conflictError(uuid,old,payload);continue;}
        this.predispatch.push(payload);if(uuid)this.predispatchByUuid.set(uuid,payload);
      }
      return;
    }
    const added=[];
    for (const entry of entries) {
      const payload=this._store(entry);
      if(payload!==null)added.push(decode(payload));
    }
    if(this.recording)await this._archive(added);
  }
  async load(key) {this.check(key);this.healthy();return this.slice(0);}
  async listSessions() {return [{sessionId:this.sessionId,mtime:Date.now()}];}
  async listSubkeys(key) {this.check(key);return [];}
  revision() {
    if(this._revision!==null)return this._revision;
    // Preserve the old SHA256(JSON.stringify(entries)) identity without
    // materializing one more history-sized string.
    this._revision=payloadRevision(this.payloads);return this._revision;
  }
}
