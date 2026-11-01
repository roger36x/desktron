"""Immutable user-input sources for turns whose native suffix is unavailable."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import copy
import hashlib
import json
import os
import stat
import tempfile

from durable_json import locked
from .store import Conflict, InvalidState, valid_id


# A turn is a multimodal projection, not a single image. Budget the serialized
# UTF-8 public input as a whole, including base64, JSON escaping and separators.
# IDs are bounded to 160 characters; 1 KiB covers the v1 reference envelope.
MAX_INPUT_PUBLIC_BYTES = 64 * 1024 * 1024
MAX_INPUT_RECORD_BYTES = MAX_INPUT_PUBLIC_BYTES + 1024
# The transport envelope (parts, options, names and correlation fields) has
# its own finite budget. It is checked before HTTP reads or JSON allocation.
MAX_PROMPT_BODY_BYTES = MAX_INPUT_PUBLIC_BYTES + 1024 * 1024


class InputTooLarge(InvalidState):
    status = 413
    code = 'turn_input_too_large'


class InputPermissionError(InvalidState):
    code = 'turn_input_permissions_invalid'


def has_native_user_input(public, source):
    """Pi context messages use user roles but cannot replace the actual request.

    Decide from verified native entry types, never from a textual prefix that
    user content could imitate. Other engines and old projections keep their
    established input inclusion rule.
    """
    has_user = any(isinstance(item, dict) and item.get('type') == 'message'
                   and item.get('role') == 'user' for item in public)
    if not has_user:
        return False
    codec = source.get('codec', '') if isinstance(source, dict) else ''
    if codec.startswith('unified-v1:claude-'):
        entries = [event.get('value') for event in source.get('events', [])
                   if isinstance(event, dict) and event.get('type') == 'claude_entry']
        return any(isinstance(entry, dict) and entry.get('type') == 'user'
                   and ((entry.get('origin') or {}).get('kind') == 'human'
                        or isinstance(entry.get('shepherd'), dict)
                        and entry['shepherd'].get('round_id'))
                   for entry in entries)
    if codec not in ('pi-0.85.1-v1', 'unified-v1:pi-0.85.1-v1'):
        return True
    entries = (source.get('entries', []) if codec == 'pi-0.85.1-v1' else
               [event.get('value') for event in source.get('events', [])
                if isinstance(event, dict) and event.get('type') == 'pi_entry'])
    semantic = any(isinstance(entry, dict) and (
        entry.get('type') in ('branch_summary', 'custom_message') or
        (entry.get('type') == 'message' and isinstance(entry.get('message'), dict)
         and entry['message'].get('role') in ('branchSummary', 'custom'))) for entry in entries)
    if not semantic:
        return True
    return any(isinstance(entry, dict) and entry.get('type') == 'message'
               and isinstance(entry.get('message'), dict)
               and entry['message'].get('role') == 'user' for entry in entries)


class TurnInputs:
    def __init__(self, root):
        self.root = os.path.abspath(os.fspath(root))

    def path(self, conversation_id, turn_id):
        """Legacy v1 path; retained for reading already-persisted references."""
        tid = valid_id(turn_id, 'turn_id')
        name = ('turn-' + hashlib.sha256(tid.encode('utf-8')).hexdigest()
                if oscompat.IS_WINDOWS and ':' in tid else tid)
        return os.path.join(self.root, 'inputs', valid_id(conversation_id, 'conversation_id'),
                            name + '.json')

    def _content_path(self, conversation_id, turn_id, input_hash):
        input_hash = str(input_hash or '')
        if len(input_hash) != 64 or any(char not in '0123456789abcdef' for char in input_hash):
            raise InvalidState('Shepherd Turn Input Hash 无效')
        tid = valid_id(turn_id, 'turn_id')
        # Turn IDs contain ':' (conversation:sequence), which is not a legal
        # Windows filename. Keep IDs in the record/reference, not the filename.
        # One full digest binds both identities without doubling the filename
        # length; directory enumeration/cleanup must also work under MAX_PATH.
        name = ('turn-' + hashlib.sha256((tid + '\0' + input_hash).encode('utf-8')).hexdigest()
                if oscompat.IS_WINDOWS else tid + '.' + input_hash)
        return os.path.join(self.root, 'inputs', valid_id(conversation_id, 'conversation_id'),
                            name + '.json')

    def reference_path(self, reference):
        if not isinstance(reference, dict):
            raise InvalidState('Shepherd Turn Input 引用无效')
        cid = valid_id(reference.get('conversation_id'), 'conversation_id')
        tid = valid_id(reference.get('turn_id'), 'turn_id')
        expected = str(reference.get('input_hash') or '')
        current = self._content_path(cid, tid, expected)
        if os.path.lexists(current): return current
        if oscompat.IS_WINDOWS:
            # Preserve the prior Windows double-digest layout and safe v1 names.
            prior_name = 'turn-' + hashlib.sha256(tid.encode('utf-8')).hexdigest()
            old = os.path.join(self.root, 'inputs', cid, prior_name + '.' + expected + '.json')
            if os.path.lexists(old): return old
            if ':' not in tid:
                old = os.path.join(self.root, 'inputs', cid, tid + '.' + expected + '.json')
                if os.path.lexists(old): return old
        prior = os.path.join(self.root,'inputs',cid,tid,expected+'.json')
        return prior if os.path.lexists(prior) else self.path(cid, tid)

    @staticmethod
    def _hash(public):
        return hashlib.sha256(json.dumps(public, ensure_ascii=False, sort_keys=True,
            separators=(',', ':')).encode('utf-8')).hexdigest()

    @staticmethod
    def public_input(text, images=(), context=''):
        content = [{'type':'input_text','text':str(text or '')}]
        if context:
            content.append({'type':'input_text','text':str(context)})
        content.extend(images)
        return [{'type':'message','role':'user','content':content}]

    @staticmethod
    def validate_public(public):
        """Shared admission/write/read quota; never infer size from image count."""
        if not isinstance(public, list) or not public:
            raise InvalidState('Shepherd Turn Input 必须是非空公共投影')
        size = len(json.dumps(public, ensure_ascii=False, sort_keys=True).encode('utf-8'))
        if size > MAX_INPUT_PUBLIC_BYTES:
            raise InputTooLarge('Shepherd 本轮输入（含图片编码）超过 64 MiB 上限；'
                                '请减少附件或分轮发送')
        return size

    def write(self, conversation_id, turn_id, public):
        cid = valid_id(conversation_id, 'conversation_id')
        tid = valid_id(turn_id, 'turn_id')
        public = copy.deepcopy(public)
        self.validate_public(public)
        value = {'version':1, 'conversation_id':cid, 'turn_id':tid,
                 'public':public, 'input_hash':self._hash(public)}
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8')
        if len(encoded) > MAX_INPUT_RECORD_BYTES:
            raise InputTooLarge('Shepherd Turn Input 存储记录超过上限')
        path = self._content_path(cid, tid, value['input_hash'])
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try: os.chmod(os.path.dirname(path), 0o700)
        except OSError: pass
        with locked(path + '.coord'):
            if os.path.exists(path):
                old = self.read({'conversation_id':cid,'turn_id':tid,
                                 'input_hash':value['input_hash']})
                if old != public:
                    raise Conflict('Shepherd Turn Input 已绑定其他内容')
                return self.reference(value)
            fd, temporary = tempfile.mkstemp(prefix='.input-', dir=os.path.dirname(path))
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encoded)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                try: os.unlink(temporary)
                except FileNotFoundError: pass
        return self.reference(value)

    def read(self, reference):
        if not isinstance(reference, dict):
            raise InvalidState('Shepherd Turn Input 引用无效')
        cid = valid_id(reference.get('conversation_id'), 'conversation_id')
        tid = valid_id(reference.get('turn_id'), 'turn_id')
        expected = str(reference.get('input_hash') or '')
        path = self.reference_path(reference)
        if not os.path.isfile(path) or oscompat.is_link(path):
            raise InputPermissionError('Shepherd Turn Input 文件类型无效')
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or oscompat.loose_mode(info.st_mode)):
                raise InputPermissionError('Shepherd Turn Input 权限无效')
            if info.st_size > MAX_INPUT_RECORD_BYTES:
                raise InputTooLarge('Shepherd Turn Input 存储记录超过上限')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                encoded = stream.read(MAX_INPUT_RECORD_BYTES + 1)
            if len(encoded) > MAX_INPUT_RECORD_BYTES:
                raise InputTooLarge('Shepherd Turn Input 存储记录超过上限')
            value = json.loads(encoded.decode('utf-8'))
        finally:
            os.close(fd)
        public = value.get('public') if isinstance(value, dict) else None
        if (not isinstance(value, dict) or value.get('version') != 1 or value.get('conversation_id') != cid
                or value.get('turn_id') != tid or not isinstance(public, list)
                or value.get('input_hash') != self._hash(public)
                or value.get('input_hash') != expected):
            raise Conflict('Shepherd Turn Input 与引用不匹配')
        self.validate_public(public)
        return copy.deepcopy(public)

    def discard(self, reference):
        """Remove only an exact uncommitted input object after pre-dispatch failure."""
        if not isinstance(reference, dict):
            return False
        cid = valid_id(reference.get('conversation_id'), 'conversation_id')
        tid = valid_id(reference.get('turn_id'), 'turn_id')
        expected = str(reference.get('input_hash') or '')
        path = self._content_path(cid, tid, expected)
        with locked(path + '.coord'):
            if not os.path.exists(path):
                return False
            # Re-read through the normal verifier before removing anything.
            self.read(reference)
            os.unlink(path)
            return True

    @staticmethod
    def reference(value):
        return {key:value[key] for key in ('conversation_id','turn_id','input_hash')}
