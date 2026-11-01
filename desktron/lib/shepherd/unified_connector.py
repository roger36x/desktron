"""Source-compatible connector whose authority is the shared event journal."""
import hashlib
import os

from .unified_archive import UnifiedArchive
from .coordinator import digest
from .store import Conflict, InvalidState, SourceVerificationError
from .source_receipts import SourceReceipts
from .source_codec import claude_public, pi_public, codex_public, seal_incomplete_tools, PI_PROJECTION_VERSION
from .claude_codec import encode_rounds, CODEC_VERSION as CLAUDE_CODEC
from .pi_codec import encode_round as pi_encode, CODEC_VERSION as PI_CODEC
from .codex_codec import encode_round as codex_encode, CODEC_VERSIONS
from .tool_call_ids import history_projection_version
from .image_content import model_patch_limited

PREFIX = 'unified-v1:'
CODECS = {'claude':CLAUDE_CODEC, 'pi':PI_CODEC, **CODEC_VERSIONS}


class UnifiedConnector:
    unified = True
    supports_interrupted_recovery = True

    def __init__(self, binding, ownership_nonce, *, store_root, ownership, binary=None):
        self.binding = dict(binding)
        for key in ('binding_id','native_session_id','native_cwd','engine','connector_version'):
            setattr(self,key,binding[key])
        self.store_root = os.fspath(store_root)
        self.ownership_nonce = ownership_nonce
        self.ownership = ownership
        self.binary = binary
        if self.connector_version != PREFIX + CODECS.get(self.engine,''):
            raise SourceVerificationError('Shepherd unified codec/version mismatch')
        self.native_codec = CODECS[self.engine]
        self.archive = UnifiedArchive(store_root,binding['conversation_id'])
        self.receipts = SourceReceipts(store_root)
        self.model_image_limit = False
        self.ownership.verify(self.binding, ownership_nonce)

    def preflight(self, rounds, *, checkpoint=None, checkpoint_meta=None):
        for row in rounds: self.encode(row)
        if checkpoint is not None:
            raise InvalidState('Unified checkpoints must be selected by execution context')

    def configure_target(self, options):
        self.model_image_limit = model_patch_limited(
            self.engine, options.get('model'), options.get('provider'))

    def encode(self, row):
        if self.engine == 'claude': return encode_rounds(self.binding,[row])
        return (pi_encode if self.engine=='pi' else codex_encode)(
            row, model_image_limit=self.model_image_limit)

    def append(self, rounds, expected_revision='', on_writer=None):
        self.ownership.verify(self.binding,self.ownership_nonce)
        self.preflight(rounds)
        if not rounds: return {'native_revision':self.revision()}
        # Switching validates a view and stores only its cursor, never a second body.
        seq=rounds[-1]['offset']; revision=rounds[-1]['entry_hash']
        self.archive.advance(self.binding_id,seq,revision,expected_revision)
        return {'native_revision':revision}

    def revision(self): return self.archive.cursor(self.binding_id)['revision']

    def verify(self):
        self.ownership.verify(self.binding,self.ownership_nonce)
        return {'native_revision':self.revision(), 'sessionId':self.native_session_id}

    def recovery_boundary(self):
        self.verify()
        return self.archive.boundary()

    def _ref(self, turn_id):
        return {'kind':'native_round','engine':self.engine,'session_id':self.native_session_id,
                'entry_id':turn_id,'source_key':f'{self.engine}:{self.native_session_id}:{turn_id}'}

    def resolve_source(self, source_ref, trusted_public=None):
        return self.resolve_sources([source_ref])[0]

    def resolve_sources(self, source_refs, trusted_publics=None):
        self.verify()
        for ref in source_refs:
            if ref != self._ref(ref.get('entry_id')):
                raise SourceVerificationError('Shepherd unified source identity mismatch')
        try:
            rows=self.archive.read_many(self.binding,[ref['entry_id'] for ref in source_refs])
            return [self._resolved(ref,row) for ref,row in zip(source_refs,rows)]
        except (Conflict, InvalidState, KeyError, TypeError) as exc:
            raise SourceVerificationError(str(exc)) from exc

    def _resolved(self, ref, row):
        receipt=self.receipts.read(self.binding,ref['entry_id'],required=False)
        locator=(receipt or {}).get('locator') or {}
        # Missing/legacy receipt versions retain the exact historical projection.
        public=self._public(row['source'],version=locator.get('projection_version',2))
        if locator.get('native_revision') and locator['native_revision']!=row['revision']:
            raise SourceVerificationError('Shepherd unified source revision mismatch')
        if locator.get('terminal_state') in ('unknown','failed_recoverable','interrupted_confirmed'):
            public=seal_incomplete_tools(public)['public']
        return {'source':row['source'],'public':public}

    def _projection_version(self, round_id):
        receipt=self.receipts.read(self.binding,round_id,required=False)
        if receipt is not None:
            return receipt['locator'].get('projection_version',2)
        return PI_PROJECTION_VERSION if self.engine=='pi' else 2

    def _public(self, source, *, version=2):
        if type(version) is not int or version not in ((2,3,4) if self.engine=='pi' else (2,)):
            raise SourceVerificationError('Shepherd unified projection version unsupported')
        events=source['events']
        native_kind={'claude':'claude_entry','pi':'pi_entry'}.get(self.engine,'codex_item')
        allowed={native_kind,'runtime'} | ({'native_anchor'} if self.engine=='claude' else set())
        if any(not isinstance(event,dict) or event.get('type') not in allowed for event in events):
            raise SourceVerificationError('Shepherd unknown unified event')
        anchors=[event for event in events if event.get('type')=='native_anchor']
        if len(anchors)>1:
            raise SourceVerificationError('Shepherd native anchor count invalid')
        if anchors and (len(events)<2 or events[1].get('type')!='native_anchor'
                or events[0].get('type')!='runtime'
                or set(events[0]) - {'type','model','effort','engine'}):
            raise SourceVerificationError('Shepherd native anchor position invalid')
        if anchors:
            try:
                self.archive._validate_native_anchor(anchors[0])
            except InvalidState as error:
                raise SourceVerificationError(str(error)) from error
        entries=[event.get('value') for event in events
                 if event['type'] not in ('runtime','native_anchor')]
        if any(not isinstance(entry,dict) for entry in entries):
            raise SourceVerificationError('Shepherd native event body invalid')
        if self.engine=='claude':
            from .claude_connector import ClaudeConnector
            validator=object.__new__(ClaudeConnector)
            validator.native_session_id=self.native_session_id
            validator.native_cwd=self.native_cwd
            validator.connector_version=self.native_codec
            validator._validate_source_entries(entries,production=True)
            if anchors and entries:
                first=next((entry for entry in entries
                            if isinstance(entry.get('uuid'),str) and entry.get('uuid')),None)
                if first is not None:
                    parent=(first.get('logicalParentUuid')
                            if (first.get('type'),first.get('subtype')) ==
                               ('system','compact_boundary') else first.get('parentUuid'))
                    if (parent or '') != anchors[0]['parent_uuid']:
                        raise SourceVerificationError('Shepherd Claude native anchor parent mismatch')
            # Runtime grounding is a known first block, never historical user authority.
            metadata=next((event for event in events
                            if event['type']=='runtime' and event.get('grounding_uuid')), {})
            if (anchors and metadata.get('grounding_uuid')
                    and anchors[0].get('grounding_uuid') != metadata['grounding_uuid']):
                raise SourceVerificationError('Shepherd Claude native anchor grounding mismatch')
            grounding=metadata.get('grounding_uuid','')
            anchor_grounding=anchors[0].get('grounding_uuid') if anchors else None
            if anchor_grounding and not grounding and entries:
                has_output=any(entry.get('type')=='assistant' for entry in entries)
                has_human_like_input=any(entry.get('type')=='user'
                    and (entry.get('origin') or {}).get('kind')!='task-notification'
                    for entry in entries)
                if has_output or has_human_like_input:
                    raise SourceVerificationError(
                        'Shepherd unified grounding runtime metadata missing')
            if grounding and entries:
                matches=[entry for entry in entries if entry.get('uuid')==grounding]
                if matches:
                    if len(matches)!=1 or not validator._grounding_entry(
                            matches[0],grounding,metadata.get('grounding_sha256','')):
                        raise SourceVerificationError('Shepherd unified grounding identity/hash mismatch')
                else:
                    has_output=any(entry.get('type')=='assistant' for entry in entries)
                    has_human_like_input=any(entry.get('type')=='user'
                        and (entry.get('origin') or {}).get('kind')!='task-notification'
                        for entry in entries)
                    if has_output or has_human_like_input:
                        raise SourceVerificationError('Shepherd unified grounding identity/hash mismatch')
                    # A Claude resident can crash before the real user frame is
                    # stored while still appending queue/hook/task-notification
                    # records. Keep that inert suffix recoverable; the durable
                    # TurnInputs record will be prepended by recovery.
                    grounding=''
            return claude_public(entries,version=2,grounding_uuid=grounding,grounding_sha256=metadata.get('grounding_sha256',''))
        if self.engine=='pi':
            from .pi_connector import PiConnector
            PiConnector.source_compatibility({'codec':self.native_codec,'entries':entries}, version=version)
            internal = {event.get('execution_review_prompt_sha256') for event in events
                        if event.get('type') == 'runtime'
                        and isinstance(event.get('execution_review_prompt_sha256'), str)}
            filtered = []
            for entry in entries:
                message = entry.get('message') if isinstance(entry, dict) else None
                content = message.get('content') if isinstance(message, dict) else None
                text = content if isinstance(content, str) else '\n'.join(
                    str(block.get('text') or '') for block in (content or [])
                    if isinstance(block, dict) and block.get('type') == 'text')
                if (message and message.get('role') == 'user' and text
                        and hashlib.sha256(text.encode()).hexdigest() in internal):
                    continue
                filtered.append(entry)
            return pi_public(filtered,version=version,allow_incomplete=True)
        # Model-side instructions and echoes of user input are regenerated by Desk.
        entries=[item for item in entries if not (item.get('type')=='message'
                 and item.get('role') in ('system','developer','user'))]
        public_entries=[]
        for item in entries:
            kind=item.get('type')
            if kind in ('compaction','compaction_trigger','context_compaction'):
                # Extra native diagnostic fields do not alter this projection.
                # Coverage is certified separately by the complete snapshot.
                if kind=='compaction' and not isinstance(item.get('encrypted_content'),str):
                    raise SourceVerificationError('Shepherd native compaction payload invalid')
                if item.get('encrypted_content') is not None and not isinstance(item['encrypted_content'],str):
                    raise SourceVerificationError('Shepherd opaque checkpoint invalid')
                # A raw compaction item does not declare the complete retained
                # prefix. Keep the native evidence; never guess its coverage or
                # expose opaque payloads to another model. Full archive remains
                # a valid restore path until a complete native snapshot exists.
                continue
            public_entries.append(item)
        return codex_public(public_entries,version=2,codec=self.native_codec)

    @staticmethod
    def source_compatibility(source):
        # Diagnostics contain types/counts only, never message text or opaque data.
        engine = (source.get('identity') or {}).get('engine', '')
        events = source.get('events', [])
        compact = {'summaries':set(), 'attachments':set()}
        if engine == 'claude':
            from .claude_connector import ClaudeConnector
            native = [event.get('value') for event in events
                      if event.get('type') == 'claude_entry']
            compact = ClaudeConnector._compact_segments(native)
        counts = {}
        claude_index = -1
        for event in events:
            value = event.get('value') or {}
            if event.get('type') == 'claude_entry':
                claude_index += 1
            if event.get('type') == 'codex_item' and value.get('type') == 'agent_message':
                key = ('projected_peer_message', 'agent_message')
            elif event.get('type') == 'runtime' and event.get('context_recovery'):
                key = ('rebuilt_working_context', 'native_snapshot')
            elif event.get('type') == 'claude_entry' and claude_index in compact['attachments']:
                attachment = value.get('attachment') or {}
                key = ('ignored_internal_entry',
                       'attachment/' + str(attachment.get('type') or 'unknown'))
            elif (event.get('type') == 'claude_entry'
                    and (value.get('type'), value.get('subtype')) ==
                        ('system', 'compact_boundary')):
                key = ('ignored_internal_entry', 'system/compact_boundary')
            elif event.get('type') == 'claude_entry' and claude_index in compact['summaries']:
                key = ('ignored_internal_entry', 'user/compact_summary')
            elif (engine == 'claude' and event.get('type') == 'claude_entry'
                    and isinstance(value, dict) and not ClaudeConnector._reviewed_kind(value)
                    and ClaudeConnector._unreviewed_metadata_shape(value)):
                # Session identity was enforced when the archive event was validated.
                key = ('tolerated_unreviewed_entry', 'unreviewed/' + value['type'])
            else:
                continue
            counts[key] = counts.get(key, 0) + 1
        return [{'engine': engine, 'action': action, 'kind': kind, 'count': count}
                for (action, kind), count in sorted(counts.items())]

    def terminal_locator(self, round_id, terminal, revision, source=None):
        version=self._projection_version(round_id)
        if source is None:
            source=self.archive.read(self.binding,round_id)['source']
        public=self._public(source,version=version)
        sealed=seal_incomplete_tools(public)
        if terminal=='completed' and sealed['unfinished_tool_ids']:
            raise SourceVerificationError('Completed native turn has unfinished tools')
        return {'turn_id':round_id,'terminal_state':terminal,'native_revision':revision,
                'projection_version':version,'unfinished_tool_ids':sealed['unfinished_tool_ids']}

    @staticmethod
    def _verify_anchor_attempt(source, expected_attempt_id):
        if not expected_attempt_id:
            return
        anchors=[event for event in source.get('events',[])
                 if event.get('type')=='native_anchor']
        if (source.get('identity') or {}).get('engine')=='claude' and len(anchors)!=1:
            raise SourceVerificationError('Shepherd native anchor missing for active attempt')
        if anchors and anchors[0].get('attempt_id') != expected_attempt_id:
            raise SourceVerificationError('Shepherd native anchor attempt mismatch')

    def verify_recovery_attempt(self, round_id, expected_attempt_id):
        row=self.archive.read(self.binding,round_id)
        self._verify_anchor_attempt(row['source'],expected_attempt_id)
        return row

    def recover_terminal_receipt(self, round_id, *, expected_attempt_id=None):
        """Finish the archive→receipt crash window without another native request."""
        self.verify()
        row=self.verify_recovery_attempt(round_id,expected_attempt_id)
        if row['terminal'] not in ('completed','failed_recoverable','interrupted_confirmed'):
            return None
        return self.receipts.write(self.binding,round_id,
            self.terminal_locator(round_id,row['terminal'],row['revision']))

    def recover_suffix(self, boundary, native_locator=None, trusted_public=None,
                       expected_attempt_id=None):
        turn_id=(native_locator or {}).get('turn_id')
        if turn_id and not str(turn_id).startswith(self.binding['conversation_id']+':'):
            raise SourceVerificationError('Shepherd recovery locator conversation mismatch')
        if not turn_id:
            turn_id=self.archive.suffix_turn(self.binding,boundary)
        self.archive.verify_recovery_boundary(self.binding, turn_id, boundary)
        row=self.archive.read(self.binding,turn_id)
        self._verify_anchor_attempt(row['source'],expected_attempt_id)
        version=self._projection_version(turn_id)
        public=self._public(row['source'],version=version)
        sealed=seal_incomplete_tools(public)
        locator={'turn_id':turn_id,'terminal_state':'unknown','native_revision':row['revision'],
                 'unfinished_tool_ids':sealed['unfinished_tool_ids'],'projection_version':version}
        return {'source':row['source'],'public':sealed['public'],'locator':locator,
                'unfinished_tool_ids':sealed['unfinished_tool_ids'],
                'compatibility':self.source_compatibility(row['source'])}

    def interrupted_locator(self, round_id, boundary, native_locator):
        return self.recover_suffix(boundary,{'turn_id':round_id})['locator']

    @staticmethod
    def _portable_binding(state, candidate):
        """Identify the exact producer Binding from native checkpoint identity."""
        native=(candidate.get('payload') or {}).get('native')
        for binding in (state.get('bindings') or {}).values():
            if not str(binding.get('connector_version','')).startswith(PREFIX):
                continue
            engine=binding.get('engine')
            if engine=='claude' and isinstance(native,list):
                sessions={row.get('sessionId') for row in native
                          if isinstance(row,dict) and row.get('sessionId')}
                if sessions=={binding.get('native_session_id')}:
                    return binding
            if engine=='pi' and isinstance(native,list) and native:
                header=native[0]
                if (isinstance(header,dict) and header.get('type')=='session'
                        and header.get('id')==binding.get('native_session_id')):
                    return binding
        return None

    @staticmethod
    def _checkpoint_boundary_hash(row):
        value = row.get('source_hash') or row.get('turn_hash')
        return value if isinstance(value, str) and len(value) == 64 else ''

    def _portable_context(self, state, *, persist=True):
        """Derive/freeze the newest certified cross-engine working context."""
        from .portable_checkpoint import SUPPORTED_CODECS, PROFILE, derive, codex_items
        rows=state.get('rounds') or []
        best=None
        # Frozen portable rows survive source Host rotation. Revalidate their
        # declared ledger boundary on every adoption; stored content alone never
        # grants coverage.
        for saved in self.archive.portable_candidates():
            through=saved.get('through_seq');portable=saved.get('payload') or {}
            if (portable.get('codec') not in SUPPORTED_CODECS or portable.get('profile')!=PROFILE
                    or portable.get('checkpoint_id')!=saved.get('checkpoint_id')
                    or portable.get('turn_id')!=saved.get('turn_id')
                    or portable.get('through_seq')!=through or not isinstance(through,int)
                    or through<1 or through>len(rows)
                    or rows[through-1].get('round_id')!=saved.get('turn_id')
                    or rows[through-1].get('outcome')!='completed'
                    or rows[through-1].get('history_state')!='available'
                    or rows[through-1].get('turn_hash')!=portable.get('covers_turn_hash')
                    or self._checkpoint_boundary_hash(rows[through-1])!=portable.get('source_hash')):
                continue
            producer=(state.get('bindings') or {}).get(portable.get('producer_binding_id')) or {}
            if (rows[through-1].get('producer_binding_id')!=portable.get('producer_binding_id')
                    or producer.get('engine')!=portable.get('producer_engine')
                    or producer.get('native_session_id')!=portable.get('producer_session_id')
                    or producer.get('connector_version')!=PREFIX+str(portable.get('producer_codec') or '')):
                continue
            try:items=codex_items(portable)
            except (InvalidState,SourceVerificationError):continue
            best={'turn_id':saved['turn_id'],'through_seq':through,
                  'payload':{'native':{'model':'portable','items':items,
                    'portable_checkpoint_id':portable['checkpoint_id'],
                    'estimated_tokens':portable['estimated_tokens']},
                    'source_hash':portable['source_hash']}}
            break
        for candidate in self.archive.checkpoint_candidates():
            through=candidate.get('through_seq')
            if best is not None and isinstance(through,int) and through<=best['through_seq']:
                break
            if (not isinstance(through,int) or through<1 or through>len(rows)
                    or rows[through-1].get('round_id')!=candidate.get('turn_id')
                    or rows[through-1].get('outcome')!='completed'
                    or rows[through-1].get('history_state')!='available'
                    or self._checkpoint_boundary_hash(rows[through-1])!=(candidate.get('payload') or {}).get('source_hash')):
                continue
            binding=self._portable_binding(state,candidate)
            if (binding is None
                    or rows[through-1].get('producer_binding_id')!=binding.get('binding_id')):
                continue
            try:
                portable=derive({**candidate,'covers_turn_hash':rows[through-1]['turn_hash']},
                    producer_engine=binding['engine'],producer_binding_id=binding['binding_id'],
                    session_id=binding['native_session_id'],native_cwd=binding['native_cwd'],
                    native_codec=binding['connector_version'][len(PREFIX):])
            except (InvalidState,SourceVerificationError):
                continue
            if persist:
                self.archive.portable_checkpoint(portable['checkpoint_id'],
                    turn_id=candidate['turn_id'],through_seq=through,payload=portable)
            if best is None or through>=best['through_seq']:
                best={'turn_id':candidate['turn_id'],'through_seq':through,
                      'payload':{'native':{'model':'portable',
                          'items':codex_items(portable),'portable_checkpoint_id':portable['checkpoint_id'],
                          'estimated_tokens':portable['estimated_tokens']},
                          'source_hash':portable['source_hash']}}
                break
        return best

    def context(self, options, *, read_only=False, cancelled=None):
        self.configure_target(options)
        from .source_resolver import SourceResolver
        from .projector import ReadProjector
        from .service import Service
        service=Service(self.store_root)
        state=service.coordinator.state(self.binding['conversation_id'])
        resolver=SourceResolver([service.connector(b) for b in state['bindings'].values()])
        projector=ReadProjector(service.store,resolver)
        # Working memory survives effort/window/prompt updates just as it does
        # in a resident native session. Current instructions are installed by
        # the runner, never taken from a historical prompt snapshot.
        scope={'engine':self.engine,'codec':self.native_codec,'cwd':self.native_cwd,
               'model':options.get('model',''),'provider':options.get('provider','')}
        projection_version=history_projection_version(self.engine)
        if projection_version:
            scope['history_projection']=projection_version
        key=digest(scope)
        legacy_scope={'engine':self.engine,'codec':self.native_codec,'cwd':self.native_cwd,
                      'model':options.get('model',''),'provider':options.get('provider',''),
                      'effort':options.get('effort',''),
                      'context_window':options.get('_context_window',0),
                      'system_hash':options.get('_system_hash','')}
        if projection_version:
            legacy_scope['history_projection']=projection_version
        legacy_key=digest(legacy_scope)
        if not options.get('model'):
            key=None  # An unresolved default model cannot identify a reusable checkpoint.
        rows=state['rounds']
        def verified_checkpoint(candidate):
            if candidate is None:
                return None
            through=candidate.get('through_seq')
            if (not isinstance(through,int) or through<1 or through>len(rows)
                    or rows[through-1].get('round_id')!=candidate.get('turn_id')):
                raise SourceVerificationError('Shepherd checkpoint coverage mismatch')
            boundary=rows[through-1]
            # Working Context is sealed before the Coordinator commit. If that
            # later commit is quarantined, the archive cache can legitimately
            # outlive its ledger authority. Treat a non-completed or unavailable
            # boundary as a cache miss; a hash mismatch on a completed,
            # available turn remains corruption and must fail closed.
            if (boundary.get('outcome')!='completed'
                    or boundary.get('history_state')!='available'):
                return None
            if ((candidate.get('payload') or {}).get('source_hash') !=
                    self._checkpoint_boundary_hash(boundary)):
                raise SourceVerificationError('Shepherd checkpoint source hash mismatch')
            return candidate
        checkpoint=verified_checkpoint(self.archive.checkpoint(key) if key else None)
        if checkpoint is None and key:
            checkpoint=verified_checkpoint(self.archive.checkpoint(legacy_key))
        if self.engine == 'codex' and key:
            # Model selection is not an archive reset. Native Codex accepts a
            # previous model's working history and validates opaque items itself.
            # Match provider/cwd/codec exactly; never adopt another engine's or
            # provider's encrypted state. Keep the producer model in the payload
            # and only save under the target key after a successful native turn.
            for candidate in self.archive.checkpoint_candidates():
                native=candidate['payload'].get('native')
                source_model=native.get('model') if isinstance(native,dict) else None
                if (not isinstance(source_model,str) or not source_model
                        or candidate['context_key'] != digest({**scope,'model':source_model})):
                    continue
                candidate=verified_checkpoint(candidate)
                if candidate is None:
                    continue
                if checkpoint is None or candidate['through_seq'] > checkpoint['through_seq']:
                    checkpoint=candidate
                break
        if checkpoint is None:
            from .migrated_context import migrated_context
            checkpoint=verified_checkpoint(migrated_context(self,service,state,options))
            native=(checkpoint or {}).get('payload',{}).get('native')
            borrowed=(self.engine=='codex' and isinstance(native,dict)
                      and native.get('model') != options.get('model'))
            if checkpoint is not None and key and not borrowed and not read_only:
                self.archive.checkpoint(key,turn_id=checkpoint['turn_id'],
                    through_seq=checkpoint['through_seq'],payload=checkpoint['payload'])
        # Codex-family can consume a certified public working checkpoint. Prefer
        # it only when it advances beyond the target's own native cache; equal
        # coverage keeps the richer native state.
        if self.engine == 'codex':
            portable=self._portable_context(state, persist=not read_only)
            if portable is not None and (checkpoint is None
                    or portable['through_seq']>checkpoint['through_seq']):
                checkpoint=portable
        checkpoint=verified_checkpoint(checkpoint)
        through=checkpoint['through_seq'] if checkpoint else 0
        rounds=[]  # Do not expand the archive before the admission decision.
        native=(checkpoint or {}).get('payload',{}).get('native')
        from .context_admission import admit
        from .context_budget import estimate_context_tokens
        # Scope includes provider/model/cwd/codec even when the model is unresolved;
        # unresolved checkpoints are still never reused.
        admission_key=digest(scope)
        learned=self.archive.context_limit(admission_key)
        rejected_through=0
        if learned:
            rejected_through=int(learned['turn_id'].rsplit(':',1)[1])
            if (rejected_through<1 or rejected_through>len(rows)
                    or rows[rejected_through-1]['round_id']!=learned['turn_id']):
                raise SourceVerificationError('Shepherd context rejection coverage mismatch')
            rejected_boundary=rows[rejected_through-1]
            if rejected_boundary.get('history_state')!='available':
                learned=None
                rejected_through=0
            elif (rejected_boundary.get('outcome')!='failed'
                    or self._checkpoint_boundary_hash(rejected_boundary)!=learned['source_hash']):
                # A context-limit receipt is evidence from a committed
                # failed_recoverable turn, unlike a Working Context checkpoint,
                # which is anchored to a completed turn.
                raise SourceVerificationError('Shepherd context rejection coverage mismatch')
        try:limit=int(options.get('_context_window') or 0)
        except (TypeError,ValueError):limit=0
        current=max(0,int(options.get('_current_tokens') or 0))
        # Missing checkpoints and prior rejections are decided BEFORE encoding a
        # potentially incompatible full history into the target protocol.
        # Successfully projected canonical rounds are a compatible context, not
        # a reason to discard tool outputs or images merely because this target
        # engine has not produced its own native checkpoint yet. Their encoded
        # cost is still measured below before native dispatch.
        policy=dict(window=limit,current=current,has_history=bool(state['rounds']),
            has_checkpoint=(checkpoint is not None or (through < len(rows) and limit > 0)
                            or not options.get('_context_admission')),
            required=bool(options.get('_require_context_budget')),
            learned=learned['history_tokens'] if learned else None,
            rejected=bool(learned and through<=rejected_through))
        plan=admit(history=0,**policy)
        cost=0
        projected_rounds=0
        if not plan.reason:
            if self.engine == 'codex':
                from .codex_working_context import budget_items
                history_items=budget_items(native)
            else:
                from .context_fallback import native_items
                history_items=list(native_items(checkpoint))
            cost=estimate_context_tokens(history_items) if history_items else 0
            plan=admit(history=cost,**policy)
            # Read bounded batches and stop as soon as the target cannot fit.
            # One indivisible turn can still be large; never split its tool pairs.
            for start in range(through+1,len(rows)+1,12):
                if plan.reason:
                    break
                batch=projector.project(self.binding['conversation_id'],start,
                    end_offset=min(start+11,len(rows)),cancelled=cancelled)
                projected_rounds+=len(batch)
                for row in batch:
                    items=self.encode(row)
                    rounds.append(row)
                    # Separate JSON-list envelopes conservatively overestimate
                    # the concatenated list; never remeasure the growing prefix.
                    cost+=estimate_context_tokens(items) if items else 0
                    plan=admit(history=cost,**policy)
                    if plan.reason:
                        break
            del history_items
        if plan.reason:
            rows=state['rounds']
            if not rows:
                raise Conflict('目标模型 Working Context 超过上下文窗口，未派发')
            from .context_fallback import degraded_checkpoint
            portable=self._portable_context(state, persist=not read_only)
            if portable is not None and (checkpoint is None or portable['through_seq']>checkpoint['through_seq']):
                checkpoint=portable
            recent=projector.project(self.binding['conversation_id'],max(1,len(rows)-11), cancelled=cancelled)
            projected_rounds+=len(recent)
            checkpoint=degraded_checkpoint(
                conversation_id=self.binding['conversation_id'],checkpoint=checkpoint,
                rounds=recent,total_rounds=len(rows),through_seq=len(rows),
                turn_id=rows[-1]['round_id'],source_hash=self._checkpoint_boundary_hash(rows[-1]),
                history_budget=plan.history_budget,encode=self.encode,
                target_engine=self.engine,target_model=options.get('model',''),
                retrieval_hint=options.get('_history_hint',''),
                retrieval_hint_in_system=options.get('_history_hint_in_system') is True)
            rounds=[]
            cost=estimate_context_tokens(checkpoint['payload']['native'].get('items') or [])
            if cost>plan.history_budget:
                raise Conflict('目标模型没有可用的降级上下文空间，历史未派发')
        self.admission={'version':1,'context_key':admission_key,'history_tokens':cost,'reason':plan.reason,
                        'rejection_reason':learned.get('reason','') if learned else '',
                        # This may be an explicit planning threshold, NOT a claim
                        # about the provider's actual model capacity.
                        'planning_window':limit,'current_tokens':current,
                        'checkpoint_through':through,'projected_rounds':projected_rounds,
                        'fallback_history_budget':plan.history_budget if plan.reason else None}
        return key,checkpoint,rounds
