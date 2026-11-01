"""Conversation-owned event journal. Native working copies are never authority."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import contextlib
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading
import uuid
import re
from collections import OrderedDict

from .coordinator import digest
from .store import Conflict, InvalidState, valid_id


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


class _ConnectionSlot:
    def __init__(self):
        self.lock = threading.RLock()
        self.connection = None
        self.identity = None
        self.users = 0
        self.retired = False


class _ArchiveConnections:
    """Process-local bounded LRU of SQLite connections, one serialized slot per DB."""
    MAX_OPEN = 16
    RETAIN_IDLE = not oscompat.IS_WINDOWS
    _lock = threading.Lock()
    _slots = OrderedDict()
    _pid = os.getpid()

    @staticmethod
    def _identity(path):
        stat = os.stat(path)
        return stat.st_dev, stat.st_ino

    @classmethod
    def _reset_after_fork(cls):
        pid = os.getpid()
        if cls._pid == pid:
            return
        # Inherited locks and SQLite handles must never be reused in the child.
        # Do not close connections there: the parent owns their normal shutdown.
        cls._lock = threading.Lock()
        cls._slots = OrderedDict()
        cls._pid = pid

    @classmethod
    @contextlib.contextmanager
    def acquire(cls, path, initialize, *, cache_key=None):
        key = os.fspath(cache_key or path)
        cached = True
        cls._reset_after_fork()
        with cls._lock:
            slot = cls._slots.get(key)
            if slot is None:
                while len(cls._slots) >= cls.MAX_OPEN:
                    victim = next(((name, value) for name, value in cls._slots.items()
                                   if value.users == 0), None)
                    if victim is None:
                        cached = False
                        break
                    name, old = victim
                    cls._slots.pop(name, None)
                    if old.connection is not None:
                        old.connection.close()
                slot = _ConnectionSlot()
                if cached:
                    cls._slots[key] = slot
            elif cached:
                cls._slots.move_to_end(key)
            slot.users += 1
        try:
            with slot.lock:
                identity = cls._identity(path)
                if slot.connection is not None and slot.identity != identity:
                    slot.connection.close()
                    slot.connection = None
                    slot.identity = None
                if slot.connection is None:
                    slot.connection = initialize()
                    slot.identity = cls._identity(path)
                yield slot.connection
        finally:
            with cls._lock:
                slot.users -= 1
                # Windows cannot unlink/replace a database with an idle handle.
                # Share slots during concurrent use, but release the last handle
                # deterministically; POSIX retains its bounded idle LRU.
                close_after = (not cached or slot.retired or not cls.RETAIN_IDLE) and slot.users == 0
                if close_after and cls._slots.get(key) is slot:
                    cls._slots.pop(key, None)
                    slot.retired = True
            if close_after:
                with slot.lock:
                    if slot.connection is not None:
                        slot.connection.close()
                        slot.connection = None
                        slot.identity = None

    @classmethod
    def close_all(cls):
        cls._reset_after_fork()
        with cls._lock:
            slots, cls._slots = list(cls._slots.values()), OrderedDict()
            for slot in slots:
                slot.retired = True
        for slot in slots:
            with slot.lock:
                if slot.connection is not None:
                    slot.connection.close()
                    slot.connection = None
                    slot.identity = None


class UnifiedArchive:
    VERSION = 1
    APPLICATION_ID = 0x44534B41  # "DSKA"

    def __init__(self, root, conversation_id):
        self.conversation_id = valid_id(conversation_id, 'conversation_id')
        self.directory = Path(root) / 'archive' / self.conversation_id
        self.path = self.directory / 'history.sqlite3'

    def _validate_connection(self, connection):
        application_id = connection.execute('PRAGMA application_id').fetchone()[0]
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if application_id not in (0, self.APPLICATION_ID):
            raise Conflict('Shepherd unified archive application id mismatch')
        if version > self.VERSION:
            raise Conflict('Shepherd unified archive schema is newer than this Desk')

    def _connect_readonly(self):
        connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=ro',
                                     uri=True, timeout=0.1, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            self._validate_connection(connection)
        except Exception:
            connection.close()
            raise
        return connection

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=10, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        try:
            self._validate_connection(connection)
        except Exception:
            connection.close()
            raise
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=FULL')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY, identity TEXT NOT NULL, terminal TEXT,
                tail_hash TEXT NOT NULL DEFAULT '', count INTEGER NOT NULL DEFAULT 0);
            CREATE INDEX IF NOT EXISTS turns_identity_terminal ON turns(identity,terminal);
            CREATE TABLE IF NOT EXISTS abandoned_turns (
                evidence_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                identity TEXT NOT NULL, tail_hash TEXT NOT NULL, count INTEGER NOT NULL,
                events TEXT NOT NULL, reason TEXT NOT NULL,
                replacement_identity TEXT NOT NULL, abandoned_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS abandoned_turns_id ON abandoned_turns(turn_id);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, turn_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL,
                UNIQUE(turn_id, ordinal));
            DROP INDEX IF EXISTS events_turn;
            CREATE TABLE IF NOT EXISTS cursors (
                binding_id TEXT PRIMARY KEY, seq INTEGER NOT NULL, revision TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS checkpoints (
                context_key TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                through_seq INTEGER NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS context_limits (
                context_key TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                history_tokens INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS portable_checkpoints (
                checkpoint_id TEXT PRIMARY KEY, turn_id TEXT NOT NULL,
                through_seq INTEGER NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS fork_snapshots (
                turn_id TEXT PRIMARY KEY, payload TEXT NOT NULL, hash TEXT NOT NULL);
        ''')
        connection.execute('PRAGMA application_id=%d' % self.APPLICATION_ID)
        connection.execute('PRAGMA user_version=%d' % self.VERSION)
        connection.commit()
        return connection

    @contextlib.contextmanager
    def db(self):
        self.directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        if self.directory.is_symlink() or self.path.is_symlink():
            raise Conflict('Shepherd unified archive path is not private')
        if not self.path.exists():
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
            except FileExistsError:
                pass
        if oscompat.loose_mode(self.path.stat().st_mode):
            raise Conflict('Shepherd unified archive permissions are not private')
        with _ArchiveConnections.acquire(self.path, self._connect) as connection:
            self._validate_connection(connection)
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    @staticmethod
    def identity(binding):
        return {key: binding[key] for key in ('conversation_id', 'binding_id', 'engine',
                'native_session_id', 'connector_version', 'ownership_nonce')}

    def begin(self, binding, turn_id):
        valid_id(turn_id, 'turn_id')
        identity = self.identity(binding)
        if identity['conversation_id'] != self.conversation_id:
            raise Conflict('Shepherd archive conversation mismatch')
        with self.db() as db:
            db.execute('INSERT OR IGNORE INTO turns(turn_id,identity) VALUES(?,?)',
                       (turn_id, encoded(identity)))
            self._turn(db, binding, turn_id)

    @staticmethod
    def _validate_native_anchor(anchor):
        fields = {'type', 'attempt_id', 'parent_uuid', 'store_revision',
                  'store_size', 'bridge_protocol', 'claude_version'}
        if (not isinstance(anchor, dict) or not fields.issubset(anchor)
                or set(anchor) - (fields | {'grounding_uuid'})):
            raise InvalidState('Shepherd native pre-dispatch anchor invalid')
        if anchor.get('type') != 'native_anchor':
            raise InvalidState('Shepherd native pre-dispatch anchor invalid')
        valid_id(anchor.get('attempt_id'), 'attempt_id')
        parent = anchor.get('parent_uuid')
        try:
            if parent and str(uuid.UUID(parent)) != parent.lower():
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise InvalidState('Shepherd native pre-dispatch parent UUID invalid')
        revision = anchor.get('store_revision')
        if not isinstance(revision, str) or not re.fullmatch(r'[0-9a-f]{64}', revision):
            raise InvalidState('Shepherd native pre-dispatch revision invalid')
        if (not isinstance(anchor.get('store_size'), int)
                or isinstance(anchor.get('store_size'), bool) or anchor['store_size'] < 0):
            raise InvalidState('Shepherd native pre-dispatch size invalid')
        if (not isinstance(anchor.get('bridge_protocol'), int)
                or isinstance(anchor.get('bridge_protocol'), bool)
                or anchor['bridge_protocol'] not in (3, 4)):
            raise InvalidState('Shepherd native pre-dispatch protocol invalid')
        version = anchor.get('claude_version')
        if (not isinstance(version, str) or not version or len(version) > 80
                or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._+-]*', version)):
            raise InvalidState('Shepherd native pre-dispatch Claude version invalid')
        grounding = anchor.get('grounding_uuid')
        try:
            if grounding is not None and str(uuid.UUID(grounding)) != grounding.lower():
                raise ValueError
        except (AttributeError, TypeError, ValueError):
            raise InvalidState('Shepherd native pre-dispatch Grounding UUID invalid')

    def _validate_predispatch_parts(self, identity, runtime, native, grounding, images):
        """One schema for creating and abandoning pre-dispatch evidence."""
        if (not isinstance(runtime, dict) or runtime.get('type') != 'runtime'
                or set(runtime) - {'type', 'model', 'effort', 'engine'}):
            raise InvalidState('Shepherd pre-dispatch anchor invalid')
        if native is not None:
            if identity['engine'] != 'claude':
                raise InvalidState('Shepherd native pre-dispatch anchor engine invalid')
            self._validate_native_anchor(native)
        if grounding is not None:
            if (native is None or not native.get('grounding_uuid') or not isinstance(grounding, dict)
                    or set(grounding) != {'type', 'grounding_uuid', 'grounding_sha256'}
                    or grounding.get('type') != 'runtime'
                    or grounding.get('grounding_uuid') != native.get('grounding_uuid')
                    or not isinstance(grounding.get('grounding_sha256'), str)
                    or not re.fullmatch(r'[0-9a-f]{64}', grounding['grounding_sha256'])):
                raise InvalidState('Shepherd native Grounding runtime invalid')
        elif native is not None and native.get('grounding_uuid'):
            raise InvalidState('Shepherd native Grounding runtime missing')
        if images is not None:
            from .image_transport import validate_receipt
            if identity['engine'] != 'claude' or native is None:
                raise InvalidState('Shepherd image transport anchor engine invalid')
            validate_receipt(images)

    def begin_predispatch(self, binding, turn_id, runtime, *, native_anchor=None,
                          grounding_runtime=None, image_transport=None, attempt_id=None):
        """Atomically create an idempotent pre-dispatch recovery anchor."""
        valid_id(turn_id, 'turn_id')
        if native_anchor is not None:
            valid_id(attempt_id, 'attempt_id')
        identity = self.identity(binding)
        if identity['engine'] == 'claude' and native_anchor is None:
            raise InvalidState('Shepherd Claude pre-dispatch requires native anchor')
        if identity['conversation_id'] != self.conversation_id:
            raise Conflict('Shepherd archive conversation mismatch')
        self._validate_predispatch_parts(identity, runtime, native_anchor,
                                         grounding_runtime, image_transport)
        if native_anchor is not None and native_anchor['attempt_id'] != attempt_id:
            raise Conflict('Shepherd native pre-dispatch attempt mismatch')
        payloads = [encoded(runtime)]
        if native_anchor is not None:
            payloads.append(encoded(native_anchor))
        if grounding_runtime is not None:
            payloads.append(encoded(grounding_runtime))
        if image_transport is not None:
            payloads.append(encoded({'type':'runtime', 'image_transport':image_transport}))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM turns WHERE turn_id=?', (turn_id,)).fetchone()
            if row is not None and row['identity'] != encoded(identity):
                raise Conflict('Shepherd archive turn identity mismatch')
            if row is None:
                db.execute('INSERT INTO turns(turn_id,identity) VALUES(?,?)',
                           (turn_id, encoded(identity)))
                fingerprint = ''
                for ordinal, payload in enumerate(payloads, 1):
                    fingerprint = hashlib.sha256((fingerprint + '\0' + payload).encode()).hexdigest()
                    db.execute('INSERT INTO events(turn_id,ordinal,payload,hash) VALUES(?,?,?,?)',
                               (turn_id, ordinal, payload, fingerprint))
                db.execute('UPDATE turns SET count=?,tail_hash=? WHERE turn_id=?',
                           (len(payloads), fingerprint, turn_id))
                return
            rows, fingerprint = self._verified_event_rows(db, row)
            if (row['terminal'] is None and row['count'] == len(payloads)
                    and len(rows) == len(payloads)
                    and fingerprint == row['tail_hash']
                    and all(item['ordinal'] == ordinal and item['payload'] == payload
                            for ordinal, (item, payload) in enumerate(zip(rows, payloads), 1))):
                return
            raise Conflict('Shepherd archive turn identity mismatch')

    def abandon_predispatch(self, binding, turn_id, reason='dispatch_fence_rejected',
                            *, attempt_id=None, missing_ok=False):
        """Preserve and release this binding's anchor after dispatch was rejected."""
        valid_id(turn_id, 'turn_id')
        if not isinstance(reason, str) or not reason:
            raise InvalidState('Shepherd abandoned anchor reason invalid')
        if attempt_id is not None:
            valid_id(attempt_id, 'attempt_id')
        identity = self.identity(binding)
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            turn = db.execute('SELECT * FROM turns WHERE turn_id=?', (turn_id,)).fetchone()
            if turn is None and missing_ok:
                return False
            if turn is None or turn['identity'] != encoded(identity):
                raise Conflict('Shepherd archive turn identity mismatch')
            if attempt_id is not None:
                anchor = db.execute(
                    'SELECT payload FROM events WHERE turn_id=? AND ordinal=2',
                    (turn_id,)).fetchone()
                try:
                    anchor = json.loads(anchor['payload']) if anchor is not None else None
                except (TypeError, ValueError):
                    anchor = None
                if (identity['engine'] == 'claude' and anchor is None) or (
                        anchor is not None and (not isinstance(anchor, dict)
                        or anchor.get('type') != 'native_anchor'
                        or anchor.get('attempt_id') != attempt_id)):
                    raise Conflict('Shepherd native pre-dispatch attempt mismatch')
            self._abandon_predispatch_anchor(db, turn, identity, reason)
            return True

    def _abandon_predispatch_anchor(self, db, turn, replacement_identity, reason):
        """Preserve only the exact write schema, never a native output suffix."""
        rows, fingerprint = self._verified_event_rows(db, turn)
        if (turn['terminal'] is not None or turn['count'] != len(rows)
                or fingerprint != turn['tail_hash'] or len(rows) not in (1, 2, 3, 4)
                or [row['ordinal'] for row in rows] != list(range(1, len(rows) + 1))):
            raise Conflict('Shepherd archive turn identity mismatch')
        try:
            decoded = [json.loads(row['payload']) for row in rows]
            runtime = decoded[0]
            native = decoded[1] if len(decoded) >= 2 else None
            cursor = 2 if native is not None else 1
            grounding = None
            if native is not None and native.get('grounding_uuid'):
                grounding = decoded[cursor]
                cursor += 1
            images = None
            if cursor < len(decoded):
                record = decoded[cursor]
                if (not isinstance(record, dict)
                        or set(record) != {'type', 'image_transport'}
                        or record['type'] != 'runtime' or record['image_transport'] is None):
                    raise InvalidState('Shepherd image transport runtime invalid')
                images = record['image_transport']
                cursor += 1
            if cursor != len(decoded):
                raise InvalidState('Shepherd pre-dispatch suffix invalid')
            self._validate_predispatch_parts(json.loads(turn['identity']), runtime,
                                             native, grounding, images)
        except (InvalidState, AttributeError, IndexError, TypeError, ValueError) as exc:
            raise Conflict('Shepherd archive turn identity mismatch') from exc
        evidence = {
            'turn_id':turn['turn_id'], 'identity':json.loads(turn['identity']),
            'terminal':turn['terminal'], 'tail_hash':turn['tail_hash'],
            'count':turn['count'],
            'events':[{'ordinal':row['ordinal'], 'payload':json.loads(row['payload']),
                       'hash':row['hash']} for row in rows],
        }
        db.execute('''INSERT INTO abandoned_turns(
                evidence_id,turn_id,identity,tail_hash,count,events,reason,
                replacement_identity,abandoned_at) VALUES(?,?,?,?,?,?,?,?,?)''',
            ('abandoned_' + uuid.uuid4().hex, turn['turn_id'], turn['identity'],
             turn['tail_hash'], turn['count'], encoded(evidence['events']),
             reason, encoded(replacement_identity), str(__import__('time').time())))
        db.execute('DELETE FROM events WHERE turn_id=?', (turn['turn_id'],))
        db.execute('DELETE FROM turns WHERE turn_id=?', (turn['turn_id'],))

    def _turn(self, db, binding, turn_id):
        row = db.execute('SELECT * FROM turns WHERE turn_id=?', (turn_id,)).fetchone()
        if row is None or row['identity'] != encoded(self.identity(binding)):
            raise Conflict('Shepherd archive turn identity mismatch')
        return row

    def append(self, binding, turn_id, event):
        return self.append_many(binding, turn_id, [event])

    def append_many(self, binding, turn_id, events):
        payloads=[]
        for event in events:
            if not isinstance(event, dict) or not isinstance(event.get('type'), str):
                raise InvalidState('Shepherd archive event must be typed')
            if event.get('type') == 'native_anchor':
                raise InvalidState('Shepherd native anchor requires pre-dispatch transaction')
            payloads.append(encoded(event))
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            turn = self._turn(db, binding, turn_id)
            if turn['terminal'] is not None:
                raise Conflict('Shepherd archive turn is already sealed')
            fingerprint, ordinal = turn['tail_hash'], turn['count']
            for payload in payloads:
                fingerprint = hashlib.sha256((fingerprint+'\0'+payload).encode()).hexdigest()
                ordinal += 1
                db.execute('INSERT INTO events(turn_id,ordinal,payload,hash) VALUES(?,?,?,?)',
                           (turn_id, ordinal, payload, fingerprint))
            db.execute('UPDATE turns SET count=?,tail_hash=? WHERE turn_id=?',
                       (ordinal, fingerprint, turn_id))
        return fingerprint

    def read(self, binding, turn_id):
        return self.read_many(binding, [turn_id])[0]

    @contextlib.contextmanager
    def read_db(self):
        """Reuse a bounded read-only slot without serializing behind the writer."""
        if self.directory.is_symlink() or self.path.is_symlink():
            raise Conflict('Shepherd unified archive path is not private')
        if oscompat.loose_mode(self.path.stat().st_mode):
            raise Conflict('Shepherd unified archive permissions are not private')
        key = os.fspath(self.path) + ':readonly'
        with _ArchiveConnections.acquire(self.path, self._connect_readonly,
                                         cache_key=key) as connection:
            self._validate_connection(connection)
            try:
                yield connection
            finally:
                # read_many() opens an explicit snapshot. End it before this
                # connection returns to the pool; rollback is write-free here.
                connection.rollback()

    def read_many(self, binding, turn_ids):
        if not turn_ids: return []
        with self.read_db() as db:
            # One read snapshot and one WAL connection for the complete batch.
            db.execute('BEGIN')
            return [self._read(db, binding, turn_id) for turn_id in turn_ids]

    def _verified_event_rows(self, db, turn):
        rows = db.execute(
            'SELECT ordinal,payload,hash FROM events WHERE turn_id=? ORDER BY ordinal',
            (turn['turn_id'],)).fetchall()
        fingerprint = ''
        for n, row in enumerate(rows, 1):
            fingerprint = hashlib.sha256((fingerprint+'\0'+row['payload']).encode()).hexdigest()
            if row['ordinal'] != n or row['hash'] != fingerprint:
                raise Conflict('Shepherd archive event hash mismatch')
        if len(rows) != turn['count'] or fingerprint != turn['tail_hash']:
            raise Conflict('Shepherd archive tail hash mismatch')
        return rows, fingerprint

    def _read(self, db, binding, turn_id):
        turn = self._turn(db, binding, turn_id)
        rows, _ = self._verified_event_rows(db, turn)
        entries = [json.loads(row['payload']) for row in rows]
        source = {'codec':binding['connector_version'], 'identity':json.loads(turn['identity']),
                  'turn_id':turn_id, 'events':entries}
        return {'source':source, 'terminal':turn['terminal'], 'revision':digest(source)}

    def finish_verified(self, binding, turn_id, terminal, *, working_context=None):
        """Seal a turn and return the source verified in that same transaction."""
        if terminal not in ('completed','failed_recoverable','interrupted_confirmed','unknown'):
            raise InvalidState('Shepherd archive terminal invalid')
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            turn = self._turn(db, binding, turn_id)
            verified = self._read(db, binding, turn_id)
            if turn['terminal'] is not None and turn['terminal'] != terminal:
                raise Conflict('Shepherd archive terminal conflict')
            db.execute('UPDATE turns SET terminal=? WHERE turn_id=?', (terminal,turn_id))
            db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?,?)',
                       (binding['binding_id'],int(turn_id.rsplit(':',1)[1]),verified['revision']))
            # The learning record points back to immutable native rejection evidence.
            # Seal it atomically with the failed turn, never with an unconfirmed exit.
            if terminal == 'failed_recoverable':
                for event in verified['source']['events']:
                    rejection = event.get('context_rejection') if event.get('type') == 'runtime' else None
                    if isinstance(rejection, dict):
                        db.execute('INSERT OR REPLACE INTO context_limits VALUES(?,?,?)',
                            (rejection['context_key'], turn_id, rejection['history_tokens']))
            if working_context is not None:
                if terminal != 'completed' or working_context['through_seq'] != int(turn_id.rsplit(':',1)[1]):
                    raise InvalidState('Working context must cover this completed turn')
                payload={'native':working_context['native'],'source_hash':verified['revision']}
                self._put_checkpoint(db,working_context['key'],turn_id,
                                     working_context['through_seq'],payload)
        return verified

    def finish(self, binding, turn_id, terminal, *, working_context=None):
        return self.finish_verified(binding, turn_id, terminal,
                                    working_context=working_context)['revision']

    def has_unsealed_turn(self, binding):
        # Called by resident cleanup callbacks which can outlive their original
        # runner. Consult durable binding state, never an old runner's booleans.
        with self.db() as db:
            return db.execute('SELECT 1 FROM turns WHERE identity=? AND terminal IS NULL LIMIT 1',
                              (encoded(self.identity(binding)),)).fetchone() is not None

    def retain_native_home(self, binding, home):
        """Move failed execution evidence intact; never keep a Writer alive."""
        root = self.directory.parent.parent
        expected = root / 'resident' / 'codex-family' / binding['engine'] / binding['binding_id'] / 'home'
        home = Path(home)
        if home != expected:
            raise Conflict('Shepherd retained native HOME identity mismatch')
        for part in (home, *home.parents):
            if part.is_symlink():
                raise Conflict('Shepherd retained native HOME is a symlink')
            if part == root: break
        if not home.exists(): return
        # Archive directory checks precede the rename. If a filesystem operation
        # fails, the caller must NOT fall through to destructive cleanup.
        with self.db():
            destination = self.directory / ('native-evidence-' + uuid.uuid4().hex)
            os.rename(home, destination)
            for folder in (self.directory, home.parent):
                oscompat.fsync_directory(folder)

    def retain_context_evidence(self, binding, turn_id, raw):
        """Keep rejected native cache bytes independently of disposable HOME.

        The journal holds the content-addressed reference, not another replayable
        history. Write/fsync failures are fatal: never claim lossless recovery
        unless the original bytes have actually been retained.
        """
        self.read(binding, turn_id)  # Validate ownership and existing journal first.
        fingerprint = hashlib.sha256(raw).hexdigest()
        name = 'context-evidence-' + fingerprint + '.jsonl'
        path = self.directory / name
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(fd, 'rb') as stream:
                if stream.read() != raw:
                    raise Conflict('Shepherd context evidence hash mismatch')
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
        oscompat.fsync_directory(self.directory)
        return {'file': name, 'sha256': fingerprint, 'bytes': len(raw)}

    def boundary(self):
        with self.db() as db:
            return {'start_seq':db.execute('SELECT COALESCE(MAX(seq),0) FROM events').fetchone()[0]}

    def suffix_turn(self, binding, boundary):
        start = boundary.get('start_seq')
        if not isinstance(start,int) or start < 0:
            raise InvalidState('Shepherd archive recovery boundary invalid')
        with self.db() as db:
            candidates = db.execute('SELECT turn_id,identity FROM turns WHERE terminal IS NULL').fetchall()
            own = [row['turn_id'] for row in candidates if row['identity'] == encoded(self.identity(binding))]
            if len(own) != 1:
                raise Conflict('Shepherd archive recovery turn is ambiguous')
            foreign = db.execute('SELECT DISTINCT turn_id FROM events WHERE seq>?', (start,)).fetchall()
            if any(row['turn_id'] != own[0] for row in foreign):
                raise Conflict('Shepherd archive recovery boundary crosses turns')
            return own[0]

    def verify_recovery_boundary(self, binding, turn_id, boundary):
        """A locator cannot bypass the exact dispatched suffix boundary."""
        start = boundary.get('start_seq')
        if not isinstance(start, int) or isinstance(start, bool) or start < 0:
            raise InvalidState('Shepherd archive recovery boundary invalid')
        with self.db() as db:
            db.execute('BEGIN')
            self._turn(db, binding, turn_id)
            first = db.execute('SELECT MIN(seq) FROM events WHERE turn_id=?', (turn_id,)).fetchone()[0]
            foreign = db.execute('SELECT 1 FROM events WHERE seq>? AND turn_id!=? LIMIT 1',
                                 (start, turn_id)).fetchone()
            if (first is not None and first <= start) or foreign:
                raise Conflict('Shepherd archive recovery boundary crosses turns')

    def cursor(self, binding_id):
        if not self.path.exists() or self.path.stat().st_size == 0:
            return {'seq':0,'revision':''}
        with self.read_db() as db:
            row=db.execute('SELECT seq,revision FROM cursors WHERE binding_id=?',(binding_id,)).fetchone()
            return dict(row) if row else {'seq':0,'revision':''}

    def advance(self, binding_id, seq, revision, expected):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT seq,revision FROM cursors WHERE binding_id=?',(binding_id,)).fetchone()
            if row and row['seq']==seq and row['revision']==revision:
                return revision
            if (row['revision'] if row else '') != expected or seq < (row['seq'] if row else 0):
                raise Conflict('Shepherd archive cursor revision conflict')
            db.execute('INSERT OR REPLACE INTO cursors VALUES(?,?,?)',(binding_id,seq,revision))
        return revision

    def checkpoint(self, context_key, *, turn_id=None, through_seq=None, payload=None):
        with self.db() as db:
            if payload is not None:
                db.execute('BEGIN IMMEDIATE')
                self._put_checkpoint(db,context_key,turn_id,through_seq,payload)
                return
            row=db.execute('SELECT * FROM checkpoints WHERE context_key=?',(context_key,)).fetchone()
            if row is None: return None
            body=json.loads(row['payload'])
            if digest(body)!=row['hash']: raise Conflict('Shepherd checkpoint hash mismatch')
            return {'turn_id':row['turn_id'],'through_seq':row['through_seq'],'payload':body}

    def fork_snapshot(self, binding, turn_id, *, payload=None):
        """Independent, immutable copies of already verified public history."""
        identity = self.identity(binding)
        if identity['conversation_id'] != self.conversation_id:
            raise Conflict('Shepherd fork snapshot conversation mismatch')
        valid_id(turn_id, 'turn_id')
        if payload is not None:
            if (payload.get('identity') != identity or payload.get('turn_id') != turn_id
                    or not isinstance(payload.get('public'), list)):
                raise InvalidState('Shepherd fork snapshot identity invalid')
            with self.db() as db:
                db.execute('BEGIN IMMEDIATE')
                old = db.execute('SELECT hash FROM fork_snapshots WHERE turn_id=?',
                                 (turn_id,)).fetchone()
                if old is not None and old['hash'] != digest(payload):
                    raise Conflict('Shepherd fork snapshot is immutable')
                db.execute('INSERT OR IGNORE INTO fork_snapshots VALUES(?,?,?)',
                           (turn_id, encoded(payload), digest(payload)))
            return payload
        with self.read_db() as db:
            row = db.execute('SELECT * FROM fork_snapshots WHERE turn_id=?',
                             (turn_id,)).fetchone()
            if row is None:
                raise Conflict('Shepherd fork snapshot missing')
            body = json.loads(row['payload'])
            if (digest(body) != row['hash'] or body.get('identity') != identity
                    or body.get('turn_id') != turn_id):
                raise Conflict('Shepherd fork snapshot identity/hash mismatch')
            return body

    def context_limit(self, context_key):
        with self.db() as db:
            row = db.execute('SELECT * FROM context_limits WHERE context_key=?',
                             (context_key,)).fetchone()
            if row is None:
                return None
            turn = db.execute('SELECT identity FROM turns WHERE turn_id=?',
                              (row['turn_id'],)).fetchone()
            if turn is None:
                raise Conflict('Shepherd context rejection source missing')
            verified = self._read(db, json.loads(turn['identity']), row['turn_id'])
            expected = {'context_key': context_key, 'history_tokens': row['history_tokens']}
            evidence = [event for event in verified['source']['events']
                        if event.get('type') == 'runtime' and event.get('context_rejection') == expected]
            if verified['terminal'] != 'failed_recoverable' or not evidence:
                raise Conflict('Shepherd context rejection evidence mismatch')
            reason = 'context_window_exceeded'  # Frozen pre-detail receipts remain valid.
            for event in evidence:
                if 'context_rejection_details' not in event:
                    continue
                details = event['context_rejection_details']
                if (not isinstance(details, dict) or details.get('version') != 1
                        or details.get('reason') not in ('context_window_exceeded', 'compaction_thrashing')
                        or type(details.get('history_tokens_before')) is not int
                        or details['history_tokens_before'] <= 0):
                    raise Conflict('Shepherd context rejection detail mismatch')
                reason = details['reason']
            return {**expected, 'turn_id': row['turn_id'], 'source_hash': verified['revision'],
                    'reason': reason}

    def checkpoint_candidates(self):
        """Verified candidates; callers must still check execution scope/coverage."""
        with self.db() as db:
            rows=db.execute('SELECT * FROM checkpoints ORDER BY through_seq DESC, context_key').fetchall()
        result=[]
        for row in rows:
            body=json.loads(row['payload'])
            if digest(body)!=row['hash']:
                raise Conflict('Shepherd checkpoint hash mismatch')
            result.append({'context_key':row['context_key'],'turn_id':row['turn_id'],
                           'through_seq':row['through_seq'],'payload':body})
        return result

    def portable_checkpoint(self, checkpoint_id, *, turn_id=None, through_seq=None, payload=None):
        """Store or read one immutable, conversation-owned portable checkpoint."""
        valid_id(checkpoint_id,'checkpoint_id')
        with self.db() as db:
            if payload is None:
                row=db.execute('SELECT * FROM portable_checkpoints WHERE checkpoint_id=?',
                               (checkpoint_id,)).fetchone()
                if row is None:return None
                body=json.loads(row['payload'])
                if digest(body)!=row['hash']:
                    raise Conflict('Shepherd portable checkpoint hash mismatch')
                return {'checkpoint_id':row['checkpoint_id'],'turn_id':row['turn_id'],
                        'through_seq':row['through_seq'],'payload':body}
            if type(through_seq) is not int or through_seq < 1:
                raise InvalidState('Shepherd portable checkpoint coverage invalid')
            encoded_payload=encoded(payload);fingerprint=digest(payload)
            db.execute('BEGIN IMMEDIATE')
            old=db.execute('SELECT * FROM portable_checkpoints WHERE checkpoint_id=?',
                           (checkpoint_id,)).fetchone()
            if old:
                if (old['turn_id']!=turn_id or old['through_seq']!=through_seq
                        or old['hash']!=fingerprint or old['payload']!=encoded_payload):
                    raise Conflict('Shepherd portable checkpoint identity conflict')
                return
            db.execute('INSERT INTO portable_checkpoints VALUES(?,?,?,?,?)',
                       (checkpoint_id,turn_id,through_seq,encoded_payload,fingerprint))

    def portable_candidates(self):
        with self.db() as db:
            rows=db.execute('SELECT * FROM portable_checkpoints '
                            'ORDER BY through_seq DESC,checkpoint_id').fetchall()
        result=[]
        for row in rows:
            body=json.loads(row['payload'])
            if digest(body)!=row['hash']:
                raise Conflict('Shepherd portable checkpoint hash mismatch')
            result.append({'checkpoint_id':row['checkpoint_id'],'turn_id':row['turn_id'],
                           'through_seq':row['through_seq'],'payload':body})
        return result

    @staticmethod
    def _put_checkpoint(db, key, turn_id, through_seq, payload):
        if type(through_seq) is not int or through_seq < 1:
            raise InvalidState('Shepherd working context coverage invalid')
        old=db.execute('SELECT * FROM checkpoints WHERE context_key=?',(key,)).fetchone()
        fingerprint=digest(payload)
        if old and (old['through_seq'] > through_seq or
                    (old['through_seq'] == through_seq and
                     (old['turn_id'] != turn_id or old['hash'] != fingerprint))):
            raise Conflict('Shepherd working context cannot overwrite newer or different evidence')
        db.execute('INSERT OR REPLACE INTO checkpoints VALUES(?,?,?,?,?)',
                   (key,turn_id,through_seq,encoded(payload),fingerprint))
