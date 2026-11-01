import { createHash } from 'node:crypto';
import { DatabaseSync } from 'node:sqlite';
import { lstatSync } from 'node:fs';

const MAX_ENTRY = 4 * 1024 * 1024, MAX_BATCH = 16 * 1024 * 1024;
function canonical(value) {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort()
    .map(key => JSON.stringify(key) + ':' + canonical(value[key])).join(',') + '}';
  return JSON.stringify(value);
}
function nextRevision(revision, payload) {
  return createHash('sha256').update(revision + '\0' + payload).digest('hex');
}
export class ShepherdClaudeStore {
  constructor(path, sessionId) { this.path = path; this.sessionId = sessionId; }
  db() {
    const info = lstatSync(this.path);
    if (!info.isFile() || info.isSymbolicLink() || (process.platform !== 'win32' && (info.mode & 0o077))) throw new Error('unsafe SessionStore file');
    const db = new DatabaseSync(this.path);
    db.exec('PRAGMA busy_timeout=10000; PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL');
    return db;
  }
  check(key) {
    if (!key || key.sessionId !== this.sessionId || key.subpath !== undefined)
      throw new Error('session key mismatch');
  }
  async append(key, entries) {
    this.check(key); const db = this.db();
    try {
      db.exec('BEGIN IMMEDIATE');
      const byUuid = db.prepare('SELECT payload FROM entries WHERE uuid=?');
      const insert = db.prepare('INSERT INTO entries(uuid,payload) VALUES(?,?)');
      let revision = db.prepare("SELECT value FROM meta WHERE key='revision'").get()?.value;
      if (typeof revision !== 'string') throw new Error('SessionStore revision missing');
      let batchBytes = 0;
      for (const entry of entries) {
        if (!entry || typeof entry.type !== 'string') throw new Error('invalid SessionStore entry');
        const payload = canonical(entry), bytes = Buffer.byteLength(payload);
        if (bytes > MAX_ENTRY || (batchBytes += bytes) > MAX_BATCH) throw new Error('SessionStore batch too large');
        const uuid = typeof entry.uuid === 'string' ? entry.uuid : null;
        if (uuid) {
          const old = byUuid.get(uuid);
          if (old) { if (old.payload !== payload) throw new Error('SessionStore UUID conflict'); continue; }
        }
        insert.run(uuid, payload); revision = nextRevision(revision, payload);
      }
      db.prepare("UPDATE meta SET value=? WHERE key='revision'").run(revision);
      db.exec('COMMIT');
    } catch (error) { try { db.exec('ROLLBACK'); } catch {} throw error; }
    finally { db.close(); }
  }
  async load(key) {
    this.check(key); const db = this.db();
    try { return db.prepare('SELECT payload FROM entries ORDER BY seq').all().map(row => JSON.parse(row.payload)); }
    finally { db.close(); }
  }
  async listSessions(_projectKey) { return [{ sessionId: this.sessionId, mtime: Date.now() }]; }
  async listSubkeys(key) { this.check(key); return []; }
  payloadSlice(start=0) {
    const db=this.db();
    try {return db.prepare('SELECT payload FROM entries ORDER BY seq LIMIT -1 OFFSET ?')
      .all(Math.max(0,Number(start)||0)).map(row=>row.payload);}
    finally {db.close();}
  }
  revision() {
    const db = this.db();
    try { return db.prepare("SELECT value FROM meta WHERE key='revision'").get()?.value || ''; }
    finally { db.close(); }
  }
}
