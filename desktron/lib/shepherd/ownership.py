"""Local ownership receipts for Shepherd-created native sessions."""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import json
import os
import stat
import tempfile

from durable_json import locked
from .store import Conflict, InvalidState, valid_id


class Ownership:
    def __init__(self, root):
        self.root = os.path.abspath(os.fspath(root))
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        try: os.chmod(self.root, 0o700)
        except OSError: pass

    def path(self, binding_id):
        return os.path.join(self.root, valid_id(binding_id, 'binding_id') + '.json')

    def issue(self, binding):
        required = ('binding_id', 'conversation_id', 'engine', 'native_session_id',
                    'native_cwd', 'connector_version', 'ownership_nonce')
        if not isinstance(binding, dict) or any(not binding.get(key) for key in required):
            raise InvalidState('Shepherd Binding 无法签发所有权证明')
        receipt = {key: binding[key] for key in required}
        if binding.get('prompt_snapshot_policy'):
            receipt['prompt_snapshot_policy'] = binding['prompt_snapshot_policy']
            receipt['prompt_snapshot_sha256'] = binding.get('prompt_snapshot_sha256')
        receipt['owner'] = 'shepherd'
        receipt['fingerprint'] = self._fingerprint(receipt)
        path = self.path(binding['binding_id'])
        with locked(path + '.coord'):
            if os.path.lexists(path):
                old = self.read(binding['binding_id'])
                if old != receipt:
                    raise Conflict('Shepherd Ownership Receipt 已绑定其他 Session')
                return old
            fd, temporary = tempfile.mkstemp(prefix='.ownership-', dir=self.root)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(receipt, stream, ensure_ascii=False, sort_keys=True)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                if os.path.exists(temporary): os.unlink(temporary)
        return receipt

    def verify(self, binding, ownership_nonce):
        try: receipt = self.read(binding.get('binding_id'))
        except (OSError, ValueError, InvalidState):
            raise Conflict('Shepherd Ownership Receipt 缺失或损坏') from None
        expected = {key: binding.get(key) for key in (
            'binding_id', 'conversation_id', 'engine', 'native_session_id',
            'native_cwd', 'connector_version', 'prompt_snapshot_policy',
            'prompt_snapshot_sha256')}
        actual = {key: receipt.get(key) for key in expected}
        if (actual != expected or receipt.get('owner') != 'shepherd'
                or receipt.get('ownership_nonce') != ownership_nonce):
            raise Conflict('Shepherd Ownership Receipt 与 Binding 不匹配')
        return True

    def read(self, binding_id):
        path = self.path(binding_id)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or oscompat.loose_mode(info.st_mode) or info.st_size > 65536:
                raise InvalidState('Shepherd Ownership Receipt 权限无效')
            with os.fdopen(fd, encoding='utf-8', closefd=False) as stream:
                value = json.load(stream)
        finally:
            os.close(fd)
        if not isinstance(value, dict) or value.get('fingerprint') != self._fingerprint(
                {key: item for key, item in value.items() if key != 'fingerprint'}):
            raise InvalidState('Shepherd Ownership Receipt 内容无效')
        return value

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
