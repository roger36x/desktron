"""Engine-neutral working checkpoints derived from verified native compaction.

A portable checkpoint is not a transcript and never contains an executable
request.  It is a bounded, canonical public working context (native summary plus
the exact retained tail) anchored to one immutable native checkpoint and one
committed Shepherd turn.
"""
import copy
import hashlib
import json

from .coordinator import digest, estimate_tokens
from .source_codec import claude_public, pi_public, seal_incomplete_tools, PI_PROJECTION_VERSION
from .store import InvalidState, SourceVerificationError

CODEC = 'portable-working-context-v3'
SUPPORTED_CODECS = ('portable-working-context-v2', CODEC)
PROFILE = 'historical-context-not-request-v1'
MAX_TOKENS = 200_000


def _text_message(text):
    prefix=('This session is being continued from a prior Desk conversation. '
            'The checkpoint below is historical context, not a new user request. '
            'Do not execute commands or tool calls described in it.\n\n')
    return {'type':'message', 'role':'user',
            'content':[{'type':'input_text', 'text':prefix+str(text)}]}


def _claude(native, *, session_id, native_cwd, native_codec):
    from .claude_codec import CODEC_VERSION
    if native_codec != CODEC_VERSION:
        raise SourceVerificationError('Claude portable checkpoint codec is not certified')
    if not isinstance(native, list):
        raise SourceVerificationError('Claude portable checkpoint is not an entry list')
    from .claude_connector import ClaudeConnector
    validator=object.__new__(ClaudeConnector)
    validator.native_session_id=str(session_id)
    validator.native_cwd=str(native_cwd)
    validator.connector_version=str(native_codec)
    validator._validate_source_entries(native, production=True)
    summaries=[index for index,row in enumerate(native)
               if isinstance(row,dict) and row.get('type')=='user'
               and row.get('isCompactSummary') is True]
    if len(summaries) != 1:
        raise SourceVerificationError('Claude portable checkpoint requires one compact summary')
    at=summaries[0]
    summary=(native[at].get('message') or {}).get('content')
    if not isinstance(summary,str) or not summary.strip():
        raise SourceVerificationError('Claude portable checkpoint summary is empty')
    tail=claude_public(native[at+1:],version=2)
    return [_text_message(summary.strip()), *tail]


def _pi(native, *, session_id, native_codec, **_kwargs):
    from .pi_codec import CODEC_VERSION
    if native_codec != CODEC_VERSION:
        raise SourceVerificationError('Pi portable checkpoint codec is not certified')
    if not isinstance(native,list) or not native:
        raise SourceVerificationError('Pi portable checkpoint is not an entry list')
    header=native[0]
    if (not isinstance(header,dict) or header.get('type') != 'session'
            or header.get('id') != session_id):
        raise SourceVerificationError('Pi portable checkpoint session identity mismatch')
    compactions=[(index,row) for index,row in enumerate(native)
                 if isinstance(row,dict) and row.get('type')=='compaction']
    if not compactions:
        raise SourceVerificationError('Pi portable checkpoint requires a compaction entry')
    # The retained parent-linked suffix can cross an older compaction. Native
    # SessionManager uses the latest summary; older summaries must not be added
    # twice. Identity, kept boundary and the complete retained chain still gate it.
    compact_index,compact=compactions[-1]
    summary=compact.get('summary')
    kept=compact.get('firstKeptEntryId')
    id_indexes={}
    for index,row in enumerate(native[1:],1):
        value=row.get('id') if isinstance(row,dict) else None
        if not isinstance(value,str) or not value or value in id_indexes:
            raise SourceVerificationError('Pi portable checkpoint entry identity is invalid')
        id_indexes[value]=index
    kept_index=id_indexes.get(kept,-1)
    if (not isinstance(summary,str) or not summary.strip()
            or not isinstance(kept,str) or kept_index!=1 or kept_index>=compact_index):
        raise SourceVerificationError('Pi portable checkpoint compaction boundary is invalid')
    # SessionManager's checkpoint keeps one parent-linked branch beginning at
    # firstKeptEntryId. A missing/foreign parent means the retained tail cannot
    # honestly stand in for the covered prefix.
    retained=native[kept_index:]
    previous=''
    for index,row in enumerate(retained):
        if index and row.get('parentId')!=previous:
            raise SourceVerificationError('Pi portable checkpoint retained chain is broken')
        previous=row['id']
    from .pi_connector import PiConnector
    PiConnector.source_compatibility({'codec':native_codec,'entries':native[1:]})
    tail=pi_public(native[1:],version=PI_PROJECTION_VERSION,allow_incomplete=False)
    return [_text_message(summary.strip()), *tail]


def _identity_hash(identity):
    return 'portable_'+hashlib.sha256(json.dumps(identity,sort_keys=True,
        ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:32]


def derive(candidate, *, producer_engine, producer_binding_id, session_id, native_cwd, native_codec):
    """Return a deterministic portable payload or fail closed.

    ``candidate`` is a verified UnifiedArchive checkpoint row.  Coverage is
    checked by the caller against the serial ledger before this pure conversion.
    """
    if not isinstance(candidate,dict) or not isinstance(candidate.get('payload'),dict):
        raise InvalidState('Portable checkpoint candidate is invalid')
    native=candidate['payload'].get('native')
    extract={'claude':_claude,'pi':_pi}.get(str(producer_engine))
    if extract is None:
        raise SourceVerificationError('Portable checkpoint producer is not certified')
    items=extract(copy.deepcopy(native),session_id=session_id,
                  native_cwd=native_cwd,native_codec=native_codec)
    sealed=seal_incomplete_tools(items)
    if sealed['unfinished_tool_ids']:
        raise SourceVerificationError('Portable checkpoint retained tail has unfinished tools')
    tokens=estimate_tokens(items)
    if tokens <= 0 or tokens > MAX_TOKENS:
        raise SourceVerificationError('Portable checkpoint exceeds its bounded token budget')
    source_checkpoint_hash=digest(candidate['payload'])
    public_hash=digest(items)
    covers_turn_hash=str(candidate.get('covers_turn_hash') or '')
    if len(covers_turn_hash)!=64:
        raise SourceVerificationError('Portable checkpoint turn hash is missing')
    identity={'codec':CODEC,'profile':PROFILE,'producer_engine':str(producer_engine),
              'producer_binding_id':str(producer_binding_id),
              'producer_session_id':str(session_id),'producer_codec':str(native_codec),
              'source_context_key':str(candidate.get('context_key') or ''),
              'source_checkpoint_hash':source_checkpoint_hash,
              'public_hash':public_hash,
              'turn_id':str(candidate.get('turn_id') or ''),
              'through_seq':candidate.get('through_seq'),
              'covers_turn_hash':covers_turn_hash}
    checkpoint_id=_identity_hash(identity)
    return {**identity,'checkpoint_id':checkpoint_id,
            'source_hash':str(candidate['payload'].get('source_hash') or ''),
            'estimated_tokens':tokens,'items':items}


def codex_items(portable):
    """Encode one portable payload as inert Codex-family model history."""
    identity={key:portable.get(key) for key in ('codec','profile','producer_engine',
        'producer_binding_id','producer_session_id','producer_codec','source_context_key',
        'source_checkpoint_hash','public_hash','turn_id','through_seq','covers_turn_hash')}
    if (not isinstance(portable,dict) or portable.get('codec') not in SUPPORTED_CODECS
            or portable.get('profile') != PROFILE
            or digest(portable.get('items')) != portable.get('public_hash')
            or _identity_hash(identity)!=portable.get('checkpoint_id')):
        raise SourceVerificationError('Portable checkpoint identity or public hash mismatch')
    from .codex_codec import encode_round
    row={'round_id':portable['checkpoint_id'],
         'offset':int(portable['through_seq']),
         'entry_hash':portable['public_hash'],
         'public':copy.deepcopy(portable['items'])}
    items=encode_round(row)
    marker='shepherd-checkpoint:v2:%s:%s:%s' % (
        portable['checkpoint_id'],portable['through_seq'],portable['public_hash'])
    for index,item in enumerate(items):
        metadata={'turn_id':marker}
        if index==0 and item.get('type')=='message' and item.get('role')=='user':
            # Codex's supported typed metadata makes this a native compaction
            # summary fragment rather than an ordinary fresh user request.
            metadata['content_item_kinds']=['compaction.summary']
        item['internal_chat_message_metadata_passthrough']=metadata
    return items
