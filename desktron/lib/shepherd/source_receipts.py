"""Durable native-range locators for executed Shepherd rounds.

Receipts contain identifiers and boundaries only, never transcript bodies.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import hashlib
import json
import os
import stat
import tempfile

from durable_json import locked
from .store import Conflict, InvalidState, valid_id


class SourceReceipts:
    def __init__(self, root):
        self.root = os.path.abspath(os.fspath(root))

    def path(self, binding_id, entry_id):
        bid = valid_id(binding_id, 'binding_id')
        eid = valid_id(entry_id, 'entry_id')
        old = os.path.join(self.root, 'sources', bid, eid + '.json')
        if not oscompat.IS_WINDOWS or os.path.lexists(old):
            return old
        # The authenticated entry ID can contain ':'; only its filename changes.
        name = 'entry-' + hashlib.sha256(eid.encode('utf-8')).hexdigest() + '.json'
        return os.path.join(self.root, 'sources', bid, name)

    @staticmethod
    def _locator(locator):
        if not isinstance(locator, dict) or not locator:
            raise InvalidState('Shepherd native locator 无效')
        allowed = {'start_seq', 'end_seq', 'turn_id', 'terminal_uuid',
                   'terminal_state', 'native_revision', 'projection_version',
                   'unfinished_tool_ids', 'grounding_uuid', 'grounding_sha256'}
        if (set(locator) - allowed
                or any(isinstance(value, dict) for value in locator.values())
                or any(isinstance(value, list) and key != 'unfinished_tool_ids'
                       for key, value in locator.items())):
            raise InvalidState('Shepherd native locator 不能保存正文')
        grounding_uuid = locator.get('grounding_uuid')
        grounding_sha256 = locator.get('grounding_sha256')
        if grounding_uuid is not None:
            try:
                import uuid
                if str(uuid.UUID(str(grounding_uuid))) != str(grounding_uuid).lower():
                    raise ValueError
            except (ValueError, AttributeError, TypeError):
                raise InvalidState('Shepherd Grounding locator UUID 无效') from None
        if bool(grounding_uuid) != bool(grounding_sha256):
            raise InvalidState('Shepherd Grounding locator 认证不完整')
        if grounding_sha256 is not None and (not isinstance(grounding_sha256, str)
                or len(grounding_sha256) != 64
                or any(char not in '0123456789abcdef' for char in grounding_sha256)):
            raise InvalidState('Shepherd Grounding locator摘要无效')
        unfinished = locator.get('unfinished_tool_ids', [])
        if (not isinstance(unfinished, list)
                or any(not isinstance(value, str) or not value for value in unfinished)
                or len(set(unfinished)) != len(unfinished)):
            raise InvalidState('Shepherd native locator 未完成工具列表无效')
        return dict(locator)

    def write(self, binding, entry_id, locator):
        locator = self._locator(locator)
        receipt = {key: binding.get(key) for key in (
            'binding_id', 'conversation_id', 'engine', 'native_session_id',
            'connector_version', 'ownership_nonce')}
        if any(not value for value in receipt.values()):
            raise InvalidState('Shepherd native locator 身份缺失')
        receipt.update(entry_id=valid_id(entry_id, 'entry_id'), locator=locator)
        receipt['fingerprint'] = self._fingerprint(receipt)
        path = self.path(binding['binding_id'], entry_id)
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        try: os.chmod(os.path.dirname(path), 0o700)
        except OSError: pass
        with locked(path + '.coord'):
            if os.path.exists(path):
                old = self.read(binding, entry_id)
                if old != receipt:
                    raise Conflict('Shepherd native locator 已绑定其他来源')
                return old
            fd, temporary = tempfile.mkstemp(prefix='.source-', dir=os.path.dirname(path))
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(receipt, stream, ensure_ascii=False, sort_keys=True)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                try: os.unlink(temporary)
                except FileNotFoundError: pass
        return receipt

    def seal(self, binding, entry_id, locator):
        """Monotonically add terminal metadata to an existing boundary receipt."""
        locator = self._locator(locator)
        path = self.path(binding['binding_id'], entry_id)
        with locked(path + '.coord'):
            old = self.read(binding, entry_id, required=False)
            prior = (old or {}).get('locator') or {}
            for key, value in prior.items():
                if key in locator and locator[key] != value:
                    raise Conflict('Shepherd native locator 封口改变了既有边界')
            merged = {**locator, **prior}
            # Existing terminal semantics are immutable. Boundary-only receipts
            # may gain a terminal state and unknown-tool metadata exactly once.
            if prior.get('terminal_state') and prior != merged:
                raise Conflict('Shepherd native locator 已有终态，不能扩写')
            receipt = {key: binding.get(key) for key in (
                'binding_id', 'conversation_id', 'engine', 'native_session_id',
                'connector_version', 'ownership_nonce')}
            receipt.update(entry_id=valid_id(entry_id, 'entry_id'), locator=merged)
            receipt['fingerprint'] = self._fingerprint(receipt)
            fd, temporary = tempfile.mkstemp(prefix='.source-', dir=os.path.dirname(path))
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                    json.dump(receipt, stream, ensure_ascii=False, sort_keys=True)
                    stream.flush(); os.fsync(stream.fileno())
                os.replace(temporary, path)
            finally:
                try: os.unlink(temporary)
                except FileNotFoundError: pass
            return receipt

    def read(self, binding, entry_id, required=True):
        path = self.path(binding['binding_id'], entry_id)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            if required:
                raise Conflict('Shepherd native locator 不存在') from None
            return None
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or oscompat.loose_mode(info.st_mode) or info.st_size > 65536):
                raise InvalidState('Shepherd native locator 权限无效')
            with os.fdopen(fd, encoding='utf-8', closefd=False) as stream:
                value = json.load(stream)
        finally:
            os.close(fd)
        expected = {key: binding.get(key) for key in (
            'binding_id', 'conversation_id', 'engine', 'native_session_id',
            'connector_version', 'ownership_nonce')}
        actual = {key: value.get(key) for key in expected}
        if (actual != expected or value.get('entry_id') != entry_id
                or value.get('fingerprint') != self._fingerprint(
                    {key: item for key, item in value.items() if key != 'fingerprint'})):
            raise Conflict('Shepherd native locator 与 Binding 不匹配')
        return value

    @staticmethod
    def _fingerprint(value):
        return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                         separators=(',', ':')).encode()).hexdigest()
