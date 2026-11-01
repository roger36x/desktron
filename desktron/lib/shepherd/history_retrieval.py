"""Verified discovery, bounded reading, and citations for Shepherd history."""
from __future__ import annotations

import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import base64
import contextlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import time

from durable_json import locked
from error_notice import safe_text
from .coordinator import digest, estimate_tokens
from .history_safety import redact_history_text
from .projector import ReadProjector
from .source_resolver import SourceResolver
from .store import Conflict, InvalidState, valid_id

INDEX_VERSION = 5
DEFAULT_MAX_TOKENS = 8000
DEFAULT_MAX_BYTES = 128 * 1024
DEFAULT_MAX_ITEMS = 100
MAX_QUERY_RESULTS = 50
MAX_NGRAMS_PER_DOCUMENT = 65536
MAX_DOCUMENT_CHARS = 65536
MAX_CONVERSATION_CHARS = 8 * 1024 * 1024
MAX_CONVERSATION_DOCUMENTS = 20000
_LOG = logging.getLogger(__name__)


class HistoryError(Conflict):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'))


def _token(value):
    raw = _encoded(value).encode()
    return len(raw), estimate_tokens(value)


def _b64(value):
    return base64.urlsafe_b64encode(_encoded(value).encode()).decode().rstrip('=')


def _unb64(value):
    if not isinstance(value,str) or len(value)>8192:
        raise HistoryError('invalid_locator','历史定位符无效')
    try:
        raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
        return json.loads(raw)
    except Exception as exc:
        raise HistoryError('invalid_locator', '历史定位符无效') from exc


def _text(content):
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ''
    values = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get('type') in ('text', 'input_text', 'output_text', 'summary_text'):
            value = str(block.get('text') or '')
            if value:
                values.append(value)
    return '\n'.join(values)


def _snippet(text, query, radius=96):
    text, query = str(text or '').strip(), str(query or '').strip()
    at = text.casefold().find(query.casefold())
    if at < 0:
        at = 0
    lo, hi = max(0, at-radius), min(len(text), at+len(query)+radius)
    value = re.sub(r'\s+', ' ', text[lo:hi]).strip()
    return ('…' if lo else '') + value + ('…' if hi < len(text) else '')


def _item_documents(row):
    documents = []
    for ordinal, item in enumerate(row.get('public') or []):
        if not isinstance(item, dict):
            continue
        kind = item.get('type')
        if kind == 'message' and item.get('role') in ('user', 'assistant'):
            value = redact_history_text(_text(item.get('content')).strip())
            if value:
                documents.append({'ordinal':ordinal, 'role':item['role'],
                                  'kind':'message', 'text':value,
                                  'item_hash':digest(item)})
        elif kind in ('function_call', 'custom_tool_call'):
            name = redact_history_text(str(item.get('name') or '').strip(),500)
            if name:
                documents.append({'ordinal':ordinal, 'role':'tool', 'kind':'tool_name',
                                  'text':'[historical tool] '+name,
                                  'item_hash':digest(item)})
    return documents


def _citation(row, document):
    return 'shr1.' + _b64({'v':1, 'conversation':row['conversation_id'],
        'seq':row['seq'], 'turn_id':row['turn_id'], 'turn_hash':row['turn_hash'],
        'source_hash':row['source_hash'],'public_hash':row['public_hash'],
        'item':document['ordinal'],'item_hash':document['item_hash']})


def _cursor(value):
    return 'shrc1.' + _b64(value)


def _parse(prefix, value, code):
    if not isinstance(value, str) or not value.startswith(prefix):
        raise HistoryError(code, '历史定位符无效')
    body = _unb64(value[len(prefix):])
    if not isinstance(body, dict) or type(body.get('v')) is not int or body['v'] != 1:
        raise HistoryError(code, '历史定位符版本无效')
    return body


class HistoryRetrieval:
    """Non-authoritative FTS candidates backed by verified public projections."""

    def __init__(self, root):
        self.root = os.path.abspath(os.fspath(root))
        self.index_root = Path(self.root) / 'retrieval'
        self.path = self.index_root / 'history.sqlite3'

    def _service(self):
        from .service import Service
        return Service(self.root)

    def _state(self, conversation, service=None):
        valid_id(conversation, 'conversation_id')
        service = service or self._service()
        state = service.store.load_raw(conversation)
        if state is None or state.get('tombstone'):
            raise HistoryError('conversation_not_found', 'Shepherd Conversation 不存在')
        return service, state

    @staticmethod
    def _prefix_hash(rows, through=None):
        stop=len(rows) if through is None else int(through)
        return digest([[row['seq'],row['turn_hash']] for row in rows[:stop]])

    @classmethod
    def _head(cls,state):
        rows=state.get('turns') or []
        return len(rows),cls._prefix_hash(rows)

    def _metadata(self):
        path=Path(self.root).parent/'adapter'/'shepherd'/'sessions.json'
        try:
            if path.is_symlink() or not path.is_file():return {},[]
            value=json.loads(path.read_text(encoding='utf-8'))
            if not isinstance(value,dict):
                return {},[{'conversation':'*','code':'metadata_unavailable',
                            'message':'会话目录元数据格式无效'}]
            return value,[]
        except (OSError,ValueError) as exc:
            return {},[{'conversation':'*','code':'metadata_unavailable',
                        'message':safe_text(str(exc),200)}]

    @staticmethod
    def _projector(service, state):
        return ReadProjector(service.store, SourceResolver([
            service.connector(binding) for binding in state['bindings'].values()]))

    @contextlib.contextmanager
    def _db(self):
        self.index_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.index_root.is_symlink() or self.path.is_symlink():
            raise HistoryError('unsafe_index_path', '历史索引路径不安全')
        try:
            os.chmod(self.index_root, 0o700)
        except OSError:
            pass
        if not self.path.exists():
            try:
                fd=os.open(self.path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                os.close(fd)
            except FileExistsError:
                pass
        stat=self.path.stat()
        if stat.st_uid!=os.getuid() or oscompat.loose_mode(stat.st_mode):
            raise HistoryError('unsafe_index_path','历史索引权限不安全')
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA secure_delete=ON')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()
            for path in (self.path, Path(str(self.path)+'-wal'), Path(str(self.path)+'-shm')):
                try: os.chmod(path, 0o600)
                except FileNotFoundError: pass

    def _schema(self):
        with locked(str(self.index_root/'schema')):
            self._schema_locked()

    def _schema_locked(self):
        with self._db() as db:
            db.execute('PRAGMA journal_mode=WAL')
            version = db.execute('PRAGMA user_version').fetchone()[0]
            rebuilt=version!=INDEX_VERSION
            if rebuilt:
                db.execute('DROP TABLE IF EXISTS history_index_limits')
                db.execute('DROP TABLE IF EXISTS history_doc_map')
                db.execute('DROP TABLE IF EXISTS history_ngram_limits')
                db.execute('DROP TABLE IF EXISTS history_ngrams')
                db.execute('DROP TABLE IF EXISTS history_docs')
                db.execute('DROP TABLE IF EXISTS history_heads')
            db.execute('''CREATE TABLE IF NOT EXISTS history_heads(
                conversation TEXT PRIMARY KEY, head_seq INTEGER NOT NULL,
                head_hash TEXT NOT NULL, indexed_at REAL NOT NULL)''')
            db.execute('''CREATE TABLE IF NOT EXISTS history_ngrams(
                gram TEXT NOT NULL, docid INTEGER NOT NULL, conversation TEXT NOT NULL,
                PRIMARY KEY(gram,docid)) WITHOUT ROWID''')
            db.execute('CREATE INDEX IF NOT EXISTS history_ngrams_conversation '
                       'ON history_ngrams(conversation,gram)')
            db.execute('CREATE INDEX IF NOT EXISTS history_ngrams_docid '
                       'ON history_ngrams(docid)')
            db.execute('''CREATE TABLE IF NOT EXISTS history_doc_map(
                docid INTEGER PRIMARY KEY, conversation TEXT NOT NULL, seq INTEGER NOT NULL,
                text_chars INTEGER NOT NULL)''')
            db.execute('CREATE INDEX IF NOT EXISTS history_doc_map_conversation_seq '
                       'ON history_doc_map(conversation,seq)')
            db.execute('''CREATE TABLE IF NOT EXISTS history_index_limits(
                conversation TEXT NOT NULL, seq INTEGER NOT NULL, ordinal INTEGER NOT NULL,
                indexed_chars INTEGER NOT NULL, total_chars INTEGER NOT NULL,
                PRIMARY KEY(conversation,seq,ordinal)) WITHOUT ROWID''')
            db.execute('''CREATE TABLE IF NOT EXISTS history_ngram_limits(
                docid INTEGER PRIMARY KEY, conversation TEXT NOT NULL,
                indexed_chars INTEGER NOT NULL, total_chars INTEGER NOT NULL)''')
            try:
                db.execute('''CREATE VIRTUAL TABLE IF NOT EXISTS history_docs USING fts5(
                    text, conversation UNINDEXED, seq UNINDEXED, turn_id UNINDEXED,
                    turn_hash UNINDEXED, public_hash UNINDEXED, item_ordinal UNINDEXED,
                    item_hash UNINDEXED, role UNINDEXED, kind UNINDEXED, settled_at UNINDEXED,
                    tokenize='trigram')''')
            except sqlite3.OperationalError as exc:
                raise HistoryError('tokenizer_unavailable',
                    '历史搜索需要 SQLite FTS5 trigram；仍可按回合有界读取') from exc
            if not rebuilt:db.execute(f'PRAGMA user_version={INDEX_VERSION}')
        if rebuilt:
            with self._db() as db:
                checkpoint=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint and int(checkpoint[0] or 0):
                    raise HistoryError('index_busy','历史索引迁移仍有读者')
                db.execute('VACUUM')
                db.execute(f'PRAGMA user_version={INDEX_VERSION}')

    def catalog(self):
        self._schema()
        service = self._service()
        with self._db() as db:
            indexed = {row['conversation']:dict(row) for row in db.execute(
                'SELECT * FROM history_heads')}
        metadata,warnings=self._metadata();result=[]
        live = set()
        for conversation in service.store.conversation_ids():
            try:state = service.store.load_raw(conversation)
            except (Conflict,InvalidState,OSError,ValueError) as exc:
                # Preserve an old cache while the authoritative state is unreadable;
                # a later repair may make it usable again.
                live.add(conversation)
                warnings.append({'conversation':conversation,'code':'state_unavailable',
                                 'message':safe_text(str(exc),200)})
                continue
            if state is None or state.get('tombstone'):
                continue
            live.add(conversation)
            rows = state.get('turns') or []
            head_seq, head_hash = self._head(state)
            have=indexed.get(conversation) or {};meta=metadata.get(conversation)
            if not isinstance(meta,dict):
                if meta is not None:warnings.append({'conversation':conversation,
                    'code':'metadata_unavailable','message':'会话目录元数据格式无效'})
                meta={}
            tm=meta.get('time')
            if tm is not None and not isinstance(tm,dict):
                warnings.append({'conversation':conversation,'code':'metadata_unavailable',
                                 'message':'会话时间元数据格式无效'})
                tm={}
            tm=tm or {}
            try:timestamp=int(tm.get('updated') or state.get('updated_at',0)*1000)
            except (TypeError,ValueError):
                timestamp=int(state.get('updated_at',0)*1000)
                warnings.append({'conversation':conversation,'code':'metadata_unavailable',
                                 'message':'会话更新时间格式无效'})
            result.append({'conversation':conversation,
                'title':str(meta.get('title') or ''),'domain':str(meta.get('domain') or ''),
                'engine':str(meta.get('selected_engine') or meta.get('engine') or ''),
                'total':head_seq,'head_hash':head_hash,'updated_at':timestamp,
                'available':sum(row.get('history_state')=='available' for row in rows),
                'omitted':sum(row.get('history_state')=='omitted' for row in rows),
                'index':{'state':'ready' if have.get('head_seq')==head_seq
                    and have.get('head_hash')==head_hash else 'stale',
                    'head_seq':int(have.get('head_seq') or 0)}})
        stale=[]
        with self._db() as db:
            stale=[row['conversation'] for row in db.execute(
                'SELECT conversation FROM history_heads').fetchall()
                if row['conversation'] not in live]
        for conversation in stale:
            try:self.remove(conversation,vacuum=True)
            except HistoryError as exc:warnings.append({'conversation':conversation,
                'code':exc.code,'message':safe_text(str(exc),200)})
        return {'conversations':sorted(result,key=lambda row:(-row['updated_at'],row['conversation'])),
                'warnings':warnings}

    @staticmethod
    def _enrich(state, row):
        ledger=(state.get('turns') or [])[row['seq']-1]
        return {**row,'public_hash':ledger.get('public_hash') or digest(row['public']),
                'source_hash':ledger.get('source_hash','')}

    def _lock_path(self,conversation):
        return str(self.index_root/'locks'/valid_id(conversation,'conversation_id'))

    def guard(self,conversation):
        return locked(self._lock_path(conversation))

    @staticmethod
    def _delete_documents(db,conversation,cut=1):
        db.execute('DELETE FROM history_index_limits WHERE conversation=? AND seq>=?',
                   (conversation,cut))
        docids=[row[0] for row in db.execute(
            'SELECT docid FROM history_doc_map WHERE conversation=? AND seq>=?',
            (conversation,cut))]
        if docids:
            db.executemany('DELETE FROM history_ngrams WHERE docid=?',((value,) for value in docids))
            db.executemany('DELETE FROM history_ngram_limits WHERE docid=?',
                           ((value,) for value in docids))
            db.executemany('DELETE FROM history_docs WHERE rowid=?',((value,) for value in docids))
            db.executemany('DELETE FROM history_doc_map WHERE docid=?',((value,) for value in docids))

    def _remove_locked(self,conversation,*,vacuum=False):
        self._schema()
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            self._delete_documents(db,conversation)
            db.execute('DELETE FROM history_heads WHERE conversation=?',(conversation,))
        if vacuum:
            with self._db() as db:
                checkpoint=db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                if checkpoint and int(checkpoint[0] or 0):
                    raise HistoryError('index_busy','历史索引仍有读者，物理清理未完成')
                db.execute('VACUUM')
        return {'conversation':conversation,'removed':True}

    def remove(self,conversation,*,vacuum=False):
        with self.guard(conversation):
            if not self.path.exists():return {'conversation':conversation,'removed':True}
            return self._remove_locked(conversation,vacuum=vacuum)

    def tombstone_and_remove(self,conversation,coordinator):
        with self.guard(conversation):
            tombstone=coordinator.tombstone_for_delete(conversation)
            self._cleanup_deleted_locked(conversation)
            return tombstone

    def _cleanup_deleted_locked(self, conversation):
        # The authoritative tombstone is the durable retry record. A cache
        # failure must not undo deletion or prevent the adapter from starting.
        try:
            if self.path.exists():
                self._remove_locked(conversation, vacuum=True)
        except (sqlite3.Error, HistoryError, OSError) as exc:
            warning = {'conversation': conversation, 'code': 'index_cleanup_pending',
                       'message': safe_text(str(exc), 200)}
            _LOG.warning('History index cleanup pending for %s: %s',
                         conversation, warning['message'])
            return warning
        return None

    def cleanup_deleted(self, conversation):
        with self.guard(conversation):
            state = self._service().store.load_raw(conversation)
            if state is None or not state.get('tombstone'):
                raise HistoryError('not_deleted', '只能补偿已删除会话的索引清理')
            return self._cleanup_deleted_locked(conversation)

    def sync(self, conversation):
        with locked(self._lock_path(conversation)):
            return self._sync_locked(conversation)

    @staticmethod
    def _insert_document(db,conversation,row,document):
        cursor=db.execute('INSERT INTO history_docs VALUES(?,?,?,?,?,?,?,?,?,?,?)',(
            document['text'],conversation,row['seq'],row['turn_id'],
            row['turn_hash'],row['public_hash'],document['ordinal'],
            document['item_hash'],document['role'],document['kind'],
            float(row.get('settled_at') or 0)))
        # Stream grams into SQLite; do not build an unbounded Python set for a
        # high-entropy multi-megabyte message. The primary key removes duplicates.
        db.execute('INSERT INTO history_doc_map VALUES(?,?,?,?)',
                   (cursor.lastrowid,conversation,int(row['seq']),len(document['text'])))
        text=document['text'];indexed=min(len(text),MAX_NGRAMS_PER_DOCUMENT+1)
        def grams():
            for index in range(max(0,indexed-1)):
                yield (text[index:index+2].casefold(),cursor.lastrowid,conversation)
        db.executemany('INSERT OR IGNORE INTO history_ngrams VALUES(?,?,?)',grams())
        if indexed<len(text):
            db.execute('INSERT OR REPLACE INTO history_ngram_limits VALUES(?,?,?,?)',
                       (cursor.lastrowid,conversation,indexed,len(text)))

    def _sync_locked(self,conversation):
        self._schema()
        while True:
            service,state=self._state(conversation);rows=state.get('turns') or []
            head_seq,head_hash=self._head(state)
            with self._db() as db:
                old=db.execute('SELECT * FROM history_heads WHERE conversation=?',
                               (conversation,)).fetchone()
            start=1;incremental=False
            if old and 0<=old['head_seq']<=head_seq:
                expected=self._prefix_hash(rows,old['head_seq'])
                if old['head_hash']==expected:
                    start=old['head_seq']+1;incremental=True
            projector=self._projector(service,state);indexed=0;retry=False
            # WAL readers keep seeing the old head until all verified batches commit.
            with self._db() as db:
                db.execute('BEGIN IMMEDIATE')
                current=db.execute('SELECT * FROM history_heads WHERE conversation=?',
                                   (conversation,)).fetchone()
                if current and int(current['head_seq'])>head_seq:
                    return {'conversation':conversation,'head_seq':int(current['head_seq']),
                            'head_hash':current['head_hash'],'indexed':0,'mode':'newer_kept'}
                self._delete_documents(db,conversation,start if incremental else 1)
                used, count = db.execute(
                    'SELECT coalesce(sum(text_chars),0),count(*) FROM history_doc_map '
                    'WHERE conversation=?', (conversation,)).fetchone()
                for at in range(start,head_seq+1,32):
                    batch=projector.project(conversation,at,min(head_seq,at+31))
                    for row in batch:
                        if row.get('history_state')!='available':continue
                        row=self._enrich(state,row);indexed+=1
                        for document in _item_documents(row):
                            total = len(document['text'])
                            allowed = max(0, min(total, MAX_DOCUMENT_CHARS,
                                                 MAX_CONVERSATION_CHARS - used))
                            if count >= MAX_CONVERSATION_DOCUMENTS:
                                allowed = 0
                            if allowed < total:
                                db.execute('INSERT INTO history_index_limits VALUES(?,?,?,?,?)',
                                           (conversation, row['seq'], document['ordinal'], allowed, total))
                            if allowed:
                                self._insert_document(db, conversation, row,
                                                      {**document, 'text': document['text'][:allowed]})
                                used += allowed
                                count += 1
                    del batch
                latest=service.store.load_raw(conversation)
                if latest is None or latest.get('tombstone'):
                    db.rollback();self._remove_locked(conversation)
                    raise HistoryError('conversation_not_found','Shepherd Conversation 不存在')
                if self._head(latest)!=(head_seq,head_hash):
                    db.rollback();retry=True
                else:
                    db.execute('INSERT OR REPLACE INTO history_heads VALUES(?,?,?,?)',
                               (conversation,head_seq,head_hash,time.time()))
            if retry:continue
            return {'conversation':conversation,'head_seq':head_seq,'head_hash':head_hash,
                    'indexed':indexed,'mode':'incremental' if incremental else 'rebuild'}

    def search(self, query, *, conversation, role='', limit=10, sort='relevance'):
        with self.guard(conversation):
            self._sync_locked(conversation)
            return self._search_index(query, conversation=conversation, role=role,
                                      limit=limit, sort=sort, guarded=True)

    def search_cached(self, query, *, role='', limit=10, sort='relevance', exclude=()):
        """Search the already-built cross-conversation cache.

        UI callers refresh stale conversations separately so an interactive query never
        waits for every native source to be projected. Every candidate is still verified
        against the authoritative public projection before it is returned.
        """
        self._schema()
        return self._search_index(query, conversation='', role=role, limit=limit,
                                  sort=sort, guarded=False, exclude=exclude)

    def _search_index(self, query, *, conversation='', role='', limit=10,
                      sort='relevance', guarded=False, exclude=()):
        query = str(query or '').strip()
        excluded = {str(value) for value in (exclude or ()) if value}
        if len(query)>1024:
            raise HistoryError('query_too_long','历史查询超过长度限制')
        if len(query) < 2:
            raise HistoryError('query_too_short','历史查询至少需要两个字符')
        if sort not in ('relevance','time'):
            raise HistoryError('invalid_sort','历史排序方式无效')
        limit = max(1,min(MAX_QUERY_RESULTS,int(limit)))
        warnings=[]
        with self._db() as db:
            rows = db.execute('SELECT * FROM history_index_limits' +
                              (' WHERE conversation=?' if conversation else ''),
                              ((conversation,) if conversation else ())).fetchall()
            limits = {(str(row['conversation']), int(row['seq']), int(row['ordinal'])):
                      int(row['indexed_chars']) for row in rows}
        if limits:
            warnings.append({'conversation': conversation or '*', 'code': 'index_partial',
                             'message': '正文索引达到大小上限；未索引部分仍可按回合有界读取',
                             'documents': len(limits)})
        if len(query)==2:
            with self._db() as db:
                limited={row[0] for row in db.execute(
                    'SELECT DISTINCT conversation FROM history_ngram_limits')}
            if conversation:
                limited &= {conversation}
            for cid in sorted(limited):
                warnings.append({'conversation':cid,'code':'short_query_partial',
                    'message':'部分超长正文只建立了有界双字索引；可改用更长关键词或按回合读取'})
        params = []
        if len(query) >= 3:
            sql = ('SELECT text,conversation,seq,turn_id,turn_hash,public_hash,'
                   'item_ordinal,item_hash,role,kind,settled_at,bm25(history_docs) score '
                   'FROM history_docs WHERE history_docs MATCH ?')
            params.append('"'+query.replace('"','""')+'"')
        else:
            sql = ('SELECT d.text,d.conversation,d.seq,d.turn_id,d.turn_hash,d.public_hash,'
                   'd.item_ordinal,d.item_hash,d.role,d.kind,d.settled_at,0 score '
                   'FROM history_ngrams n JOIN history_docs d ON d.rowid=n.docid '
                   'WHERE n.gram=?')
            params.append(query.casefold())
        prefix='d.' if len(query)<3 else ''
        if conversation:
            sql += f' AND {prefix}conversation=?'; params.append(conversation)
        if role:
            if role not in ('user','assistant','tool'):
                raise HistoryError('invalid_role','历史角色筛选无效')
            sql += f' AND {prefix}role=?'; params.append(role)
        sql += (f' ORDER BY CAST({prefix}settled_at AS REAL) DESC, CAST({prefix}seq AS INTEGER) DESC'
                if sort=='time' else f' ORDER BY score, CAST({prefix}seq AS INTEGER) DESC')
        base_sql=sql;candidate_params=list(params)
        hits=[];metadata,metadata_warnings=self._metadata();warnings.extend(metadata_warnings)
        verified={};contexts={};offset=0;rows=[]
        while len(hits)<limit:
            with self._db() as db:
                rows=db.execute(base_sql+' LIMIT ? OFFSET ?',
                    [*candidate_params,max(limit*2,20),offset]).fetchall()
            if not rows:break
            offset+=len(rows)
            for cached in rows:
                cid=cached['conversation']
                if cid in excluded:
                    continue
                seq=int(cached['seq']);key=(cid,seq)
                if key not in verified:
                    try:
                        def project():
                            if cid not in contexts:
                                service,state=self._state(cid)
                                contexts[cid]=(state,self._projector(service,state))
                            state,projector=contexts[cid]
                            projected=projector.project(cid,seq,seq)[0]
                            return state,self._enrich(state,projected)
                        if guarded:
                            verified[key]=project()
                        else:
                            with self.guard(cid):
                                verified[key]=project()
                    except Exception as exc:
                        warnings.append({'conversation':cid,'code':'candidate_unavailable',
                                         'message':safe_text(str(exc),200)})
                        continue
                _state,row=verified[key];ordinal=int(cached['item_ordinal'])
                if row.get('history_state')!='available' or not 0<=ordinal<len(row.get('public') or []):
                    warnings.append({'conversation':cid,'code':'candidate_stale',
                                     'message':'历史候选已变化，已跳过'});continue
                documents={doc['ordinal']:doc for doc in _item_documents(row)}
                document=documents.get(ordinal)
                indexed_chars=limits.get((cid,seq,ordinal),len(document['text']) if document else 0)
                if (document is None or document['item_hash']!=cached['item_hash']
                        or document['text'][:indexed_chars]!=cached['text']
                        or row['turn_hash']!=cached['turn_hash']
                        or row['public_hash']!=cached['public_hash']
                        or query.casefold() not in document['text'].casefold()):
                    warnings.append({'conversation':cid,'code':'candidate_stale',
                                     'message':'历史候选校验失败，已跳过'});continue
                meta=metadata.get(cid);meta=meta if isinstance(meta,dict) else {}
                hits.append({'conversation':cid,'title':str(meta.get('title') or ''),
                    'domain':str(meta.get('domain') or ''),'seq':seq,'turn_id':row['turn_id'],
                    'role':document['role'],'kind':document['kind'],
                    'settled_at':float(row.get('settled_at') or 0),
                    'snippet':_snippet(document['text'],query),
                    'estimated_tokens':estimate_tokens(document['text']),
                    'citation':_citation({**row,'conversation_id':cid},document)})
                if len(hits)>=limit:break
            if len(rows)<max(limit*2,20):break
        return {'query':query,'sort':sort,'hits':hits,
                'conversations':len(set(hit['conversation'] for hit in hits)),
                'warnings':warnings}

    def _snapshot(self, state, cursor=None, *, start=1, end=None):
        rows = state.get('turns') or []
        if cursor:
            value = _parse('shrc1.',cursor,'invalid_cursor')
            if value.get('conversation') != state['conversation_id']:
                raise HistoryError('cursor_conversation_mismatch','历史游标不属于该会话')
            try:
                head_seq=int(value.get('head_seq'));end_seq=int(value.get('end_seq'))
                seq=int(value.get('seq'));item=int(value.get('item'));offset=int(value.get('offset'))
            except (TypeError,ValueError) as exc:
                raise HistoryError('invalid_cursor','历史游标字段无效') from exc
            if (not 0<=head_seq<=len(rows) or not 0<=end_seq<=head_seq
                    or not 1<=seq<=end_seq+1 or item<0 or offset<0):
                raise HistoryError('stale_cursor','历史游标已失效')
            expected = self._prefix_hash(rows,head_seq)
            if value.get('head_hash') != expected:
                raise HistoryError('stale_cursor','历史游标前缀已变化')
            return {**value,'head_seq':head_seq,'end_seq':end_seq,
                    'seq':seq,'item':item,'offset':offset}
        head_seq, head_hash = self._head(state)
        end_seq=min(head_seq,int(end) if end is not None else head_seq)
        return {'v':1,'conversation':state['conversation_id'],'head_seq':head_seq,
                'head_hash':head_hash,'end_seq':end_seq,
                'seq':max(1,int(start)),'item':0,'offset':0}

    @staticmethod
    def _record(row,item,ordinal):
        kind=item.get('type') if isinstance(item,dict) else ''
        base={'seq':row['seq'],'turn_id':row['turn_id'],'outcome':row['outcome'],
              'history_state':row['history_state'],'item':ordinal}
        if kind=='message' and item.get('role') in ('user','assistant'):
            text=redact_history_text(_text(item.get('content')))
            if text:return {**base,'kind':'message','role':item['role'],'text':text}
        if kind in ('function_call','custom_tool_call'):
            name=redact_history_text(str(item.get('name') or ''),500)
            return {**base,'kind':'tool_call','role':'tool',
                    'text':'[historical tool] '+name}
        return None

    @staticmethod
    def _fit(base,text,offset,max_bytes,max_tokens):
        source=str(text or '')[offset:]
        lo,hi,best=0,len(source),None
        while lo<=hi:
            mid=(lo+hi)//2
            candidate={**base,'text':source[:mid],
                       'text_offset':offset,'continued':mid<len(source)}
            size,tokens=_token(candidate)
            if size<=max_bytes and tokens<=max_tokens:
                best=candidate;lo=mid+1
            else:hi=mid-1
        return best

    def read(self, conversation, *, start=1, end=None, cursor='', max_turns=10,
             max_tokens=DEFAULT_MAX_TOKENS, max_bytes=DEFAULT_MAX_BYTES,
             max_items=DEFAULT_MAX_ITEMS):
        with self.guard(conversation):
            return self._read_locked(conversation,start=start,end=end,cursor=cursor,
                max_turns=max_turns,max_tokens=max_tokens,max_bytes=max_bytes,
                max_items=max_items)

    def _read_locked(self, conversation, *, start=1, end=None, cursor='', max_turns=10,
                     max_tokens=DEFAULT_MAX_TOKENS,max_bytes=DEFAULT_MAX_BYTES,
                     max_items=DEFAULT_MAX_ITEMS):
        service,state=self._state(conversation)
        max_turns=max(1,min(100,int(max_turns)));max_items=max(1,min(1000,int(max_items)))
        if int(max_tokens) < 512 or int(max_bytes) < 4096:
            raise HistoryError('budget_too_small', '历史读取至少需要 512 token 和 4096 byte 预算')
        max_tokens=min(200000,int(max_tokens))
        max_bytes=min(2*1024*1024,int(max_bytes))
        snapshot=self._snapshot(state,cursor or None,start=start,end=end)
        seq,item_at,text_at=int(snapshot['seq']),int(snapshot['item']),int(snapshot['offset'])
        end_seq=int(snapshot.get('end_seq',snapshot['head_seq']))
        stop=min(end_seq,seq+max_turns-1)
        projector=self._projector(service,state)
        projected=projector.project(conversation,seq,stop) if seq<=stop else []
        records=[];used_bytes=1024;used_tokens=256;position=(seq,item_at,text_at)
        item_count=0
        for row in projected:
            if row.get('history_state')!='available':
                position=(row['seq']+1,0,0);item_at=text_at=0
                continue
            row=self._enrich(state,row)
            begin=item_at if row['seq']==position[0] else 0
            for ordinal,item in enumerate(row.get('public') or []):
                if ordinal<begin:continue
                record=self._record(row,item,ordinal)
                if record is None:continue
                offset=text_at if row['seq']==position[0] and ordinal==begin else 0
                document={'ordinal':ordinal,'item_hash':digest(item)}
                cited={**record,'citation':_citation({**row,'conversation_id':conversation},document)}
                fitted=self._fit(cited,cited.pop('text'),offset,
                    max_bytes-used_bytes,max_tokens-used_tokens)
                if fitted is None or not fitted['text']:
                    if not records:
                        raise HistoryError('budget_too_small',
                            '历史读取预算不足以容纳最小引用记录')
                    position=(row['seq'],ordinal,offset);break
                size,tokens=_token(fitted);records.append(fitted)
                used_bytes+=size;used_tokens+=tokens;item_count+=1
                if fitted['continued']:
                    position=(row['seq'],ordinal,offset+len(fitted['text']));break
                position=(row['seq'],ordinal+1,0)
                if item_count>=max_items:break
            else:
                position=(row['seq']+1,0,0);item_at=text_at=0
                if item_count>=max_items:break
                continue
            break
        has_more=position[0]<=end_seq
        if has_more and position[0]==end_seq and projected and projected[-1]['seq']==end_seq:
            row=projected[-1];visible=[]
            for ordinal,item in enumerate(row.get('public') or []):
                record=self._record(row,item,ordinal)
                if record is not None:visible.append((ordinal,len(record['text'])))
            has_more=any(ordinal>position[1] or ordinal==position[1] and length>position[2]
                         for ordinal,length in visible)
        next_cursor=_cursor({**snapshot,'seq':position[0],'item':position[1],
                             'offset':position[2]}) if has_more else None
        result={'ok':True,'conversation':conversation,'snapshot':{
            'head_seq':snapshot['head_seq'],'head_hash':snapshot['head_hash'],
            'end_seq':end_seq},
            'records':records,'next_cursor':next_cursor,'truncated':bool(next_cursor),
            'usage':{'estimated_tokens':0,'serialized_bytes':0,'items':len(records)},
            'mode':'text','sensitive':False}
        # Usage includes its own digits; converge before enforcing the public cap.
        for _ in range(3):
            result['usage']['estimated_tokens']=estimate_tokens(result)
            result['usage']['serialized_bytes']=len(_encoded(result).encode())
        raw=_encoded(result).encode()
        if len(raw)>max_bytes or estimate_tokens(result)>max_tokens:
            raise HistoryError('budget_invariant','历史读取结果超过预算')
        return result

    def around(self, conversation, seq, *, before=3, after=3, **budgets):
        seq=int(seq);before=max(0,min(50,int(before)));after=max(0,min(50,int(after)))
        return self.read(conversation,start=max(1,seq-before),end=seq+after,
                         max_turns=before+after+1,**budgets)

    def verify(self,citation):
        value=_parse('shr1.',citation,'invalid_citation')
        conversation=value.get('conversation')
        if not isinstance(conversation,str):
            raise HistoryError('invalid_citation','历史引用字段无效')
        with self.guard(conversation):return self._verify_locked(value)

    def _verify_locked(self,value):
        try:conversation=valid_id(value.get('conversation'),'conversation_id')
        except InvalidState as exc:raise HistoryError('invalid_citation','历史引用字段无效') from exc
        if type(value.get('seq')) is not int or type(value.get('item')) is not int:
            raise HistoryError('invalid_citation','历史引用字段无效')
        seq=value['seq'];ordinal=value['item']
        if ordinal<0:raise HistoryError('invalid_citation','历史引用字段无效')
        for key in ('turn_id','turn_hash','source_hash','public_hash','item_hash'):
            field=value.get(key)
            if not isinstance(field,str) or not field:
                raise HistoryError('invalid_citation','历史引用字段无效')
        for key in ('turn_hash','source_hash','public_hash','item_hash'):
            if len(value[key])!=64 or any(char not in '0123456789abcdef' for char in value[key]):
                raise HistoryError('invalid_citation','历史引用字段无效')
        service,state=self._state(conversation)
        rows=state.get('turns') or []
        if not 1<=seq<=len(rows):raise HistoryError('citation_stale','历史引用已失效')
        ledger=rows[seq-1]
        if (ledger['turn_id']!=value.get('turn_id') or ledger['turn_hash']!=value.get('turn_hash')
                or ledger.get('source_hash')!=value.get('source_hash')):
            raise HistoryError('citation_stale','历史引用与当前账本不匹配')
        if ledger.get('history_state')!='available':
            raise HistoryError('citation_unverifiable','该回合没有可验证的公共历史')
        row=self._projector(service,state).project(conversation,seq,seq)[0]
        public_hash=ledger.get('public_hash') or digest(row['public'])
        if public_hash!=value.get('public_hash'):
            raise HistoryError('citation_stale','历史引用与当前公开投影不匹配')
        if not 0<=ordinal<len(row.get('public') or []):
            raise HistoryError('citation_stale','历史引用条目不存在')
        item=row['public'][ordinal]
        if digest(item)!=value.get('item_hash'):
            raise HistoryError('citation_stale','历史引用正文校验失败')
        return {'ok':True,'conversation':conversation,'seq':seq,'turn_id':row['turn_id'],
                'turn_hash':row['turn_hash'],'public_hash':public_hash,
                'item':ordinal,'kind':item.get('type'),'role':item.get('role'),
                'estimated_tokens':estimate_tokens(item)}
