"""SQLite-backed native side-store for Claude Agent SDK SessionStore."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import json
import os
import sqlite3
import stat
from contextlib import closing

from durable_json import locked
from .ownership import Ownership
from .store import Conflict, InvalidState, valid_id

_SCHEMA = 1
_MAX_ENTRY = 4 * 1024 * 1024
_MAX_BATCH = 16 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


class ClaudeNativeStore:
    def __init__(self, root, binding, ownership_nonce, ownership=None):
        self.root = os.path.abspath(os.fspath(root))
        self.binding = dict(binding or {})
        self.binding_id = valid_id(self.binding.get('binding_id'), 'binding_id')
        self.ownership_nonce = ownership_nonce
        default_base = os.path.dirname(os.path.dirname(self.root))
        self.ownership = ownership or Ownership(os.path.join(default_base, 'ownership'))
        base = os.path.realpath(os.path.dirname(self.ownership.root))
        expected = os.path.join(base, 'native', 'claude')
        if os.path.realpath(self.root) != expected:
            raise Conflict('Claude SessionStore 必须位于 Shepherd 私有目录')
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        try: os.chmod(self.root, 0o700)
        except OSError: pass
        self.path = os.path.join(self.root, self.binding_id + '.sqlite3')

    def initialize(self):
        self._verify_owner()
        with locked(self.path + '.coord'):
            self._safe_file(create=True)
            with closing(self._connect()) as db:
                db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS entries ('
                           'seq INTEGER PRIMARY KEY AUTOINCREMENT, uuid TEXT UNIQUE, payload TEXT NOT NULL)')
                row = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                if row is None:
                    db.execute("INSERT INTO meta(key,value) VALUES('schema',?)", (str(_SCHEMA),))
                    db.execute("INSERT INTO meta(key,value) VALUES('revision','')")
                elif row[0] != str(_SCHEMA):
                    raise InvalidState('Claude SessionStore schema 不受支持')
            self._chmod()
        return self.revision()

    def append(self, entries, expected_revision=''):
        self._verify_owner()
        if not isinstance(entries, list):
            raise InvalidState('Claude SessionStore append 需要数组')
        payloads = []
        total = 0
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('type'), str):
                raise InvalidState('Claude SessionStore entry 无效')
            payload = canonical(entry)
            size = len(payload.encode('utf-8'))
            if size > _MAX_ENTRY:
                raise InvalidState('Claude SessionStore 单条记录过大')
            total += size
            payloads.append((entry.get('uuid'), payload))
        if total > _MAX_BATCH:
            raise InvalidState('Claude SessionStore 批次过大')
        self.initialize()
        with closing(self._connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            current = self._meta(db, 'revision')
            if current != str(expected_revision or ''):
                if self._already_applied(db, payloads):
                    db.commit(); return current
                db.rollback(); raise Conflict('Claude SessionStore Revision 冲突')
            accepted = []
            for uuid, payload in payloads:
                if uuid:
                    old = db.execute('SELECT payload FROM entries WHERE uuid=?', (str(uuid),)).fetchone()
                    if old:
                        if old[0] != payload:
                            db.rollback(); raise Conflict('Claude SessionStore UUID 内容冲突')
                        continue
                db.execute('INSERT INTO entries(uuid,payload) VALUES(?,?)',
                           (str(uuid) if uuid else None, payload))
                accepted.append(payload)
            revision = current
            for payload in accepted:
                revision = hashlib.sha256((revision + '\0' + payload).encode()).hexdigest()
            if accepted:
                db.execute("UPDATE meta SET value=? WHERE key='revision'", (revision,))
            db.commit()
        self._chmod()
        return revision

    def load(self):
        self._verify_owner(); self.initialize()
        with closing(self._connect()) as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM entries ORDER BY seq')]

    def revision(self):
        self._verify_owner()
        if not os.path.exists(self.path):
            return ''
        self._safe_file()
        with closing(self._connect()) as db:
            return self._meta(db, 'revision')

    def prefix_revisions(self, ends):
        """Return revision-chain values from the exact stored JSON payload bytes."""
        self._verify_owner(); self.initialize()
        wanted=set(ends)
        if any(not isinstance(end, int) or isinstance(end, bool) or end < 0 for end in wanted):
            raise InvalidState('Claude SessionStore revision prefix invalid')
        revisions={0:''};revision=''
        with closing(self._connect()) as db:
            for index,row in enumerate(db.execute('SELECT payload FROM entries ORDER BY seq'),1):
                if index > max(wanted,default=0):break
                revision=hashlib.sha256((revision+'\0'+row[0]).encode()).hexdigest()
                if index in wanted:revisions[index]=revision
        if not wanted.issubset(revisions):
            raise Conflict('Claude SessionStore revision prefix exceeds store')
        return revisions

    def import_entries(self, entries, expected_revision='', start_index=0):
        """Append raw Claude transcript entries after a checked-out prefix.

        The CLI writes JSONL in an isolated CLAUDE_CONFIG_DIR. Callers pass the
        complete transcript plus the prefix length used for checkout; the prefix
        must still match byte-for-byte before any suffix is accepted.
        """
        current = self.load()
        if not isinstance(start_index, int) or start_index < 0 or start_index != len(current):
            raise Conflict('Claude checkout 前缀长度冲突')
        if entries[:start_index] != current:
            raise Conflict('Claude checkout 前缀内容冲突')
        suffix = entries[start_index:]
        return self.append(suffix, expected_revision), suffix

    def _verify_owner(self):
        self.ownership.verify(self.binding, self.ownership_nonce)

    def _safe_file(self, create=False):
        info = None
        try: info = os.lstat(self.path)
        except FileNotFoundError: pass
        if info is None and create:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            os.close(fd); info = os.lstat(self.path)
        if (info is None or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or oscompat.loose_mode(info.st_mode)):
            raise InvalidState('Claude SessionStore 文件或权限无效')

    def _connect(self):
        self._safe_file()
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        try:
            db.execute('PRAGMA busy_timeout=10000')
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('PRAGMA synchronous=FULL')
            return db
        except Exception:
            db.close()
            raise

    def _chmod(self):
        for suffix in ('', '-wal', '-shm'):
            try: os.chmod(self.path + suffix, 0o600)
            except FileNotFoundError: pass

    @staticmethod
    def _meta(db, key):
        row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        if row is None:
            raise InvalidState('Claude SessionStore 元数据缺失')
        return row[0]

    @staticmethod
    def _already_applied(db, payloads):
        if not payloads or any(not uuid for uuid, _payload in payloads):
            return False
        for uuid, payload in payloads:
            row = db.execute('SELECT payload FROM entries WHERE uuid=?', (str(uuid),)).fetchone()
            if not row or row[0] != payload:
                return False
        return True
