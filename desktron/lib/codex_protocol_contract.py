"""Desk's consumed app-server surface, not a Codex release allowlist.

Direction matters: clients may send only supported inputs; servers must promise
required outputs. Additive, unconsumed fields do not change this contract.
This is a deliberately bounded structural proof, not a general JSON Schema
validator. Unrecognised constraints on consumed fields produce no proof.
"""
from dataclasses import dataclass, field, replace


@dataclass(frozen=True)
class Shape:
    types: frozenset = frozenset()
    fields: dict = field(default_factory=dict)
    required: frozenset = frozenset()
    values: frozenset = frozenset()
    items: object = None
    choices: tuple = ()


def scalar(*types, values=()):
    return Shape(frozenset(types), values=frozenset(values))


def obj(required=(), **fields):
    return Shape(frozenset({'object'}), fields, frozenset(required))


def array(items):
    return Shape(frozenset({'array'}), items=items)


def choice(*shapes):
    return Shape(choices=shapes)


TEXT = scalar('string')
BOOL = scalar('boolean')
NULL_TEXT = scalar('string', 'null')
ANY = Shape()  # Raw Responses items: verified by the native source codec.
THREAD_ID = obj(('threadId',), threadId=TEXT)
APPROVAL = scalar('string', values=('never', 'on-request', 'untrusted'))
SANDBOX = scalar('string', values=('read-only', 'workspace-write', 'danger-full-access'))
INPUT = choice(obj(('type', 'text'), type=scalar('string', values=('text',)), text=TEXT),
               obj(('type', 'path'), type=scalar('string', values=('localImage',)), path=TEXT))
TURN = obj(('id', 'status'), id=TEXT,
           status=scalar('string', values=('completed', 'interrupted', 'failed', 'inProgress')))
THREAD = obj(('id',), id=TEXT, path=NULL_TEXT)

# Request.required means fields Desk ALWAYS provides (not merely sometimes uses).
REQUESTS = {
    'initialize': obj(('clientInfo', 'capabilities'),
        clientInfo=obj(('name', 'title', 'version'), name=TEXT, title=TEXT, version=TEXT),
        capabilities=obj(('experimentalApi',), experimentalApi=BOOL)),
    'thread/start': obj((), cwd=TEXT, model=TEXT, approvalPolicy=APPROVAL, sandbox=SANDBOX,
        experimentalRawEvents=BOOL, ephemeral=BOOL, baseInstructions=NULL_TEXT,
        developerInstructions=NULL_TEXT, runtimeWorkspaceRoots=array(TEXT),
        config=Shape(frozenset({'object'}), items=ANY)),
    'thread/resume': obj(('threadId',), threadId=TEXT, cwd=TEXT, model=TEXT,
        approvalPolicy=APPROVAL, sandbox=SANDBOX, developerInstructions=NULL_TEXT,
        runtimeWorkspaceRoots=array(TEXT)),
    'thread/read': obj(('threadId',), threadId=TEXT, includeTurns=BOOL),
    'thread/inject_items': obj(('threadId', 'items'), threadId=TEXT, items=array(ANY)),
    'thread/compact/start': THREAD_ID,
    'turn/start': obj(('threadId', 'input', 'collaborationMode'), threadId=TEXT, input=array(INPUT),
        collaborationMode=obj(('mode', 'settings'), mode=scalar('string', values=('default', 'plan')),
            settings=obj(('model',), model=TEXT, developer_instructions=NULL_TEXT,
                reasoning_effort=scalar('string', values=('low', 'medium', 'high')))),
        approvalPolicy=APPROVAL,
        additionalContext=Shape(frozenset({'object'}), items=obj(('kind', 'value'),
            kind=scalar('string', values=('application',)), value=TEXT))),
    'turn/interrupt': obj(('threadId', 'turnId'), threadId=TEXT, turnId=TEXT),
}
NOTIFICATIONS = {
    'turn/started': obj(('threadId', 'turn'), threadId=TEXT, turn=TURN),
    'turn/completed': obj(('threadId', 'turn'), threadId=TEXT, turn=TURN),
}
SERVER_REQUESTS = {
    name: obj(('threadId', 'turnId', 'itemId'), threadId=TEXT, turnId=TEXT, itemId=TEXT)
    for name in ('item/commandExecution/requestApproval', 'item/fileChange/requestApproval')
}
RESPONSES = {
    'v2/ThreadStartResponse.json': obj(('thread',), thread=THREAD),
    'v2/ThreadResumeResponse.json': obj(('thread',), thread=THREAD),
    'v2/ThreadReadResponse.json': obj(('thread',), thread=THREAD),
    'v2/TurnStartResponse.json': obj(('turn',), turn=TURN),
    'CommandExecutionRequestApprovalResponse.json': obj(('decision',),
        decision=scalar('string', values=('accept', 'cancel'))),
    'FileChangeRequestApprovalResponse.json': obj(('decision',),
        decision=scalar('string', values=('accept', 'cancel'))),
}
BUNDLES = ('ClientRequest.json', 'ServerNotification.json', 'ServerRequest.json', *RESPONSES)
CONTRACT_ID = 'desk-codex-app-server-v1'


def requests_for(engine):
    if engine == 'codex':
        return REQUESTS
    raise ValueError('unknown app-server engine')


class Unproved(ValueError):
    pass


@dataclass(frozen=True)
class Combined:
    """A branch whose own constraints intersect a nested anyOf/oneOf.

    Only the own-level part is kept. It can prove that Desk's value is rejected
    (for example a different `type` discriminator); it never proves acceptance.
    """
    node: dict


def branches(node, document, seen=(), *, partial=False):
    """Expand alternatives. `partial` is for outgoing proofs only: a combined
    branch becomes `Combined` instead of aborting the whole surface.
    Codex 0.156 made ImageUserInput `properties + anyOf(url|fileId)`, a branch
    Desk never sends; rejecting all of UserInput for it blocked every turn."""
    if node is True:
        return [{}]
    if not isinstance(node, dict):
        raise Unproved('schema object required')
    if '$ref' in node:
        ref = node['$ref']
        if not isinstance(ref, str) or not ref.startswith('#/') or ref in seen:
            raise Unproved('unsupported or recursive schema reference')
        if set(node) - {'$ref', 'description', 'title', 'default'}:
            raise Unproved('constrained reference')
        target = document
        for segment in ref[2:].split('/'):
            target = target[segment.replace('~1', '/').replace('~0', '~')]
        return branches(target, document, (*seen, ref), partial=partial)
    for key in ('anyOf', 'oneOf', 'allOf'):
        if key in node:
            if set(node) - {key, 'description', 'title', 'default'}:
                if partial and key != 'allOf' and not {'anyOf', 'oneOf', 'allOf'} & (set(node) - {key}):
                    return [Combined({k: v for k, v in node.items() if k != key})]
                raise Unproved('combined schema constraints')
            if key == 'allOf' and len(node[key]) != 1:
                raise Unproved('schema intersection')
            return [part for child in node[key]
                    for part in branches(child, document, seen, partial=partial)]
    return [node]


def rejects(shape, node, document):
    """Prove that `node` rejects every value of `shape` Desk sends.

    Failing to prove acceptance is not rejection (undeclared properties are
    allowed), so only a disjoint top-level type or a required discriminator
    whose sent values fall outside the branch's enum counts."""
    kind = node.get('type')
    if kind is not None:
        types = {kind} if isinstance(kind, str) else set(kind)
        if shape.types and not shape.types & types:
            return True
    properties = node.get('properties', {})
    for name, child in shape.fields.items():
        if name not in shape.required or not child.values or name not in properties:
            continue
        prop, seen = properties[name], ()
        while isinstance(prop, dict) and set(prop) <= {'$ref', 'description', 'title', 'default'} \
                and isinstance(prop.get('$ref'), str) and prop['$ref'].startswith('#/') \
                and prop['$ref'] not in seen:
            seen, target = (*seen, prop['$ref']), document
            for segment in prop['$ref'][2:].split('/'):
                target = target.get(segment, {}) if isinstance(target, dict) else {}
            prop = target
        enum = prop.get('enum') if isinstance(prop, dict) else None
        if isinstance(enum, list) and child.values.isdisjoint(enum):
            return True
    return False


def matches(shape, node, document, *, outgoing):
    """Prove the consumed structure in the appropriate variance direction."""
    if shape == ANY:
        # An opaque schema is acceptable only if it truly permits arbitrary data.
        return node is True or node == {}
    alternatives = branches(node, document, partial=outgoing)
    wanted = shape.choices or (shape,)
    if outgoing:
        # Enum and primitive alternatives can be represented as separate anyOf
        # branches. Every value Desk may send needs at least one accepting branch.
        wanted = tuple(replace(want, types=frozenset({kind}),
                               values=frozenset({value}) if value is not None else frozenset())
                       for want in wanted for kind in want.types
                       for value in (want.values or (None,)))
        known = [part for part in alternatives if not isinstance(part, Combined)]
        # A combined branch is excluded only when its own constraints already
        # reject every value Desk sends; otherwise oneOf exclusivity is unproved.
        for part in alternatives:
            if isinstance(part, Combined) and not all(
                    rejects(want, part.node, document) for want in wanted):
                raise Unproved('combined schema constraints')
        return all(any(match_branch(want, part, document, outgoing=True)
                       for part in known) for want in wanted)
    return all(any(match_branch(want, part, document, outgoing=False)
                   for want in wanted) for part in alternatives)


def match_branch(shape, node, document, *, outgoing):
    kind = node.get('type', [])
    types = {kind} if isinstance(kind, str) else set(kind)
    # Nullable inputs accept Desk's non-null values; outputs cannot silently
    # become nullable unless the consumer explicitly supports that.
    if not types or not (shape.types <= types if outgoing else types <= shape.types):
        return False
    structural = {'type', 'properties', 'required', 'items', 'enum', 'additionalProperties',
                  '$schema', 'definitions', 'title', 'description', 'default', 'format',
                  'minLength', 'maxLength'}
    if set(node) - structural:
        raise Unproved('new constraint on consumed field: ' + ', '.join(sorted(set(node) - structural)))
    if outgoing and ('minLength' in node or 'maxLength' in node):
        if not shape.values or any(not isinstance(value, str)
                or len(value) < node.get('minLength', 0)
                or len(value) > node.get('maxLength', float('inf')) for value in shape.values):
            return False
    if shape.values:
        values = set(node.get('enum', ()))
        if outgoing and values and not shape.values <= values:
            return False
        if not outgoing and (not values or not values <= shape.values):
            return False
    elif outgoing and 'enum' in node:
        return False  # Desk supplies arbitrary strings, not a closed enumeration.
    if 'object' in shape.types:
        required = set(node.get('required', ()))
        if not (required <= shape.required if outgoing else shape.required <= required):
            return False
        fields = node.get('properties', {})
        for name, child in shape.fields.items():
            if name not in fields or not matches(child, fields[name], document, outgoing=outgoing):
                return False
    if shape.items is not None:
        key = 'additionalProperties' if 'object' in shape.types else 'items'
        return matches(shape.items, node.get(key), document, outgoing=outgoing)
    return True


def assess(documents, engine='codex'):
    """Return stable, content-free failed capability names; no version decisions."""
    failures = []
    for filename, required, outgoing in (
            ('ClientRequest.json', requests_for(engine), True),
            ('ServerNotification.json', NOTIFICATIONS, False),
            ('ServerRequest.json', SERVER_REQUESTS, False)):
        document = documents[filename]
        methods = {}
        for entry in document.get('oneOf', []):
            props = entry.get('properties', {})
            for name in props.get('method', {}).get('enum', []):
                if name in methods:
                    raise Unproved('duplicate method schema')
                methods[name] = props.get('params')
        for name, shape in required.items():
            if name not in methods or not matches(shape, methods[name], document, outgoing=outgoing):
                failures.append(name)
    for filename, shape in RESPONSES.items():
        document = documents[filename]
        outgoing = 'ApprovalResponse' in filename
        if not matches(shape, document, document, outgoing=outgoing):
            failures.append(filename.removesuffix('.json'))
    return failures
