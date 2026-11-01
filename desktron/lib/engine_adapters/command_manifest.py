"""Resolved slash-command contract shared by the backend and chat UI."""
import copy
import re


VERSION = 1
_NAME = re.compile(r'^/[A-Za-z0-9_:-]+$')
# A message is command-shaped only when its first token is a whole command
# name. This mirrors the chat UI test /^\/[\w:-]+(?=\s|$)/ (JS \w is ASCII):
# a lone "/", "/ text", absolute paths such as "/tmp/a.txt" and "//note" stay
# ordinary text.
_TOKEN = re.compile(r'/[A-Za-z0-9_:-]+(?=\s|$)')


def command_token(text):
    """Return the leading command name of a message, or '' for ordinary text."""
    match = _TOKEN.match(str(text or '').lstrip())
    return match.group(0) if match else ''


COMMON = (
    ('/new', '新建一段对话', 'ui', 'none', '', False),
    ('/fork', '从当前另开一支', 'endpoint', 'none', 'fork', False),
    ('/copy', '复制上一条回复', 'ui', 'none', '', False),
    ('/task', '记一条任务到当前工作流', 'endpoint', 'none', '', False),
    ('/interrupt', '打断当前回合', 'endpoint', 'none', '', True),
    ('/unsend', '撤回刚发出的那句', 'endpoint', 'none', '', True),
)

ENGINE = {
    'claude': (
        ('/mode', '切换权限模式', 'options', 'none', '', False),
        ('/model', '切换模型', 'options', 'none', '', False),
        ('/effort', '切换 Effort', 'options', 'none', '', False),
        ('/compact', '压缩当前上下文', 'endpoint', 'turn', 'compact', False),
        ('/clear', '清空输入框', 'ui', 'none', '', False),
        ('/login', '重新登录（开终端办理）', 'terminal', 'none', '', False),
        ('/logout', '退出登录（开终端办理）', 'terminal', 'none', '', False),
    ),
    'codex': (
        ('/mode', '切换工作模式', 'options', 'none', '', False),
        ('/plan', '切换到 Plan', 'options', 'none', '', False),
        ('/permissions', '切换沙箱权限', 'options', 'none', '', False),
        ('/model', '切换模型', 'options', 'none', '', False),
        ('/effort', '切换 Effort', 'options', 'none', '', False),
        ('/compact', '压缩当前上下文', 'endpoint', 'turn', 'compact', False),
        ('/clear', '新建空上下文', 'ui', 'none', '', False),
    ),
    'pi': (
        ('/mode', '切换权限模式', 'options', 'none', '', False),
        ('/model', '切换模型', 'options', 'none', '', False),
        ('/effort', '切换 Effort', 'options', 'none', '', False),
        ('/compact', '压缩当前上下文', 'endpoint', 'turn', 'compact', False),
        ('/clear', '新建空上下文', 'ui', 'none', '', False),
    ),
}


def _desk(row):
    name, description, transport, history, capability, control = row
    out = {'name':name, 'description':description, 'owner':'desk',
           'transport':transport, 'history':history, 'portable':True}
    if capability: out['requires']=[capability]
    if control: out['control']=True
    if name == '/compact': out['result_scope']='engine_model_checkpoint'
    return out


def resolve(*, engine, shepherd, caps, native_commands=()):
    """Return only commands executable in this resolved UI/session context."""
    engine = str(engine or '')
    caps = dict(caps or {})
    commands = []
    for row in (*COMMON, *ENGINE.get(engine, ())):
        item = _desk(row)
        if any(not caps.get(key) for key in item.get('requires', ())):
            continue
        commands.append(item)
    occupied = {row['name'] for row in commands}
    if not shepherd:
        for raw in native_commands or ():
            if isinstance(raw, str):
                name, description = raw, ''
            elif isinstance(raw, dict):
                name, description = raw.get('n') or raw.get('name'), raw.get('d') or raw.get('description') or ''
            else:
                continue
            name = '/' + str(name or '').lstrip('/')
            if not _NAME.fullmatch(name) or name in occupied:
                continue
            occupied.add(name)
            commands.append({'name':name, 'description':str(description),
                'owner':engine, 'transport':'native_prompt',
                'history':'engine_defined', 'portable':False})
    return {'version':VERSION, 'engine':engine, 'shepherd':bool(shepherd),
            'commands':copy.deepcopy(commands)}
