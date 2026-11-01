"""Private, immutable, content-addressed image execution variants.

All paths below the supplied Shepherd root are fixed or SHA-256 names. Directory
handles and O_NOFOLLOW protect both reads and publication from symlink swaps.
Missing objects may be derived before dispatch; corrupt objects fail closed.
"""
import oscompat  # noqa: F401  Windows 上补齐 POSIX 语义(fcntl、进程组、O_NOFOLLOW 等)
import base64
import hashlib
import json
import os
import stat
import uuid
from contextlib import contextmanager

from .image_content import image_parts, _dimensions, ImageInputError
from .store import valid_id

MAX_RECORD_BYTES = 4_002_048


def variant_key(policy, mime, source_hash):
    return hashlib.sha256(json.dumps([policy, mime, source_hash],
        separators=(',', ':')).encode()).hexdigest()


class ImageVariants:
    def __init__(self, root, conversation_id):
        self.root = os.path.abspath(os.fspath(root))
        self.conversation_id = valid_id(conversation_id, 'conversation_id')

    @contextmanager
    def _directory(self):
        if oscompat.IS_WINDOWS:
            directory = self._windows_directory()
            try:
                yield directory
            except OSError as exc:
                # Same fail-closed mapping as the descriptor branch below, e.g. a
                # record replaced by a link is refused by O_NOFOLLOW.
                raise ImageInputError('图片派生存储不可安全读写，消息未派发') from exc
            return
        root = namespace = directory = None
        try:
            root = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.mkdir('image-variants', mode=0o700, dir_fd=root)
            except FileExistsError:
                pass
            namespace = os.open('image-variants', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=root)
            info = os.fstat(namespace)
            if info.st_uid != os.getuid() or oscompat.loose_mode(info.st_mode):
                raise ImageInputError('图片派生存储目录权限无效，消息未派发')
            try:
                os.mkdir(self.conversation_id, mode=0o700, dir_fd=namespace)
            except FileExistsError:
                pass
            directory = os.open(self.conversation_id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                dir_fd=namespace)
            info = os.fstat(directory)
            if info.st_uid != os.getuid() or oscompat.loose_mode(info.st_mode):
                raise ImageInputError('图片派生存储目录权限无效，消息未派发')
            yield directory
        except OSError as exc:
            raise ImageInputError('图片派生存储不可安全读写，消息未派发') from exc
        finally:
            if directory is not None: os.close(directory)
            if namespace is not None: os.close(namespace)
            if root is not None: os.close(root)

    def _windows_directory(self):
        """Windows 不能以目录句柄打开目录；改为按路径建立，并拒绝符号链接或 junction。"""
        try:
            if not os.path.isdir(self.root) or oscompat.is_link(self.root):
                raise FileNotFoundError(self.root)
            path = self.root
            for name in ('image-variants', self.conversation_id):
                path = os.path.join(path, name)
                try:
                    os.mkdir(path, 0o700)
                except FileExistsError:
                    pass
                if oscompat.is_link(path) or not os.path.isdir(path):
                    raise ImageInputError('图片派生存储目录权限无效，消息未派发')
            return path
        except OSError as exc:
            raise ImageInputError('图片派生存储不可安全读写，消息未派发') from exc

    @staticmethod
    def _open_in(directory, name, flags, mode=0o777):
        if isinstance(directory, str):
            return os.open(os.path.join(directory, name), flags, mode)
        return os.open(name, flags, mode, dir_fd=directory)

    @staticmethod
    def _read(directory, key, expected):
        fd = ImageVariants._open_in(directory, key + '.json',
                                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or oscompat.loose_mode(info.st_mode):
                raise ImageInputError('图片派生件权限无效，消息未派发')
            if info.st_size > MAX_RECORD_BYTES:
                raise ImageInputError('图片派生件记录超过上限，消息未派发')
            with os.fdopen(fd, 'rb', closefd=False) as stream:
                encoded = stream.read(MAX_RECORD_BYTES + 1)
            if len(encoded) > MAX_RECORD_BYTES:
                raise ImageInputError('图片派生件记录超过上限，消息未派发')
            value = json.loads(encoded.decode('utf-8'))
            if not isinstance(value, dict) or value.get('version') != 1:
                raise ValueError('version')
            receipt = value['receipt']
            if not isinstance(receipt, dict) or any(receipt.get(k) != v for k,v in expected.items()):
                raise ValueError('source identity')
            mime, data = image_parts({'image_url':'data:' + receipt['variant_mime'] +
                                     ';base64,' + value['data']})
            raw = base64.b64decode(data, validate=True)
            width, height = _dimensions(raw, mime)
            if (len(data) > 4_000_000 or len(raw) != receipt['variant_bytes']
                    or hashlib.sha256(raw).hexdigest() != receipt['variant_sha256']
                    or list((width, height)) != receipt['variant_size']
                    or not width or not height or max(width, height) > 2000
                    or receipt.get('variant_key') != key
                    or variant_key(receipt['policy'], receipt['source_mime'],receipt['source_sha256']) != key):
                raise ValueError('variant integrity')
            return (mime, raw), receipt
        except ImageInputError:
            raise
        except (ValueError, KeyError, TypeError, UnicodeError) as exc:
            raise ImageInputError('图片派生件内容或哈希不匹配，消息未派发') from exc
        finally:
            os.close(fd)

    def get(self, policy, mime, source_hash, source_size, source_bytes, create):
        key = variant_key(policy, mime, source_hash)
        expected = {'policy':policy, 'source_mime':mime, 'source_sha256':source_hash,
                    'source_size':list(source_size), 'source_bytes':source_bytes}
        with self._directory() as directory:
            try:
                return self._read(directory, key, expected)
            except FileNotFoundError:
                pass
            variant, receipt = create()
            receipt = {**receipt, 'variant_key':key}
            encoded = json.dumps({'version':1, 'receipt':receipt,
                'data':base64.b64encode(variant[1]).decode('ascii')},sort_keys=True).encode('utf-8')
            if len(encoded) > MAX_RECORD_BYTES:
                raise ImageInputError('图片派生件超过存储预算，消息未派发')
            temporary = '.variant-' + uuid.uuid4().hex
            fd = self._open_in(directory, temporary,
                               os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            windows = isinstance(directory, str)
            try:
                with os.fdopen(fd, 'wb') as stream:
                    stream.write(encoded); stream.flush(); os.fsync(stream.fileno())
                try:
                    if windows:
                        os.link(os.path.join(directory, temporary),
                                os.path.join(directory, key + '.json'))
                    else:
                        os.link(temporary, key + '.json', src_dir_fd=directory,
                                dst_dir_fd=directory, follow_symlinks=False)
                except FileExistsError:
                    pass  # A concurrent publisher won; verify its immutable object.
                if not windows:
                    os.fsync(directory)   # Windows 不能打开目录句柄；NTFS 元数据由日志保证
            except OSError as exc:
                raise ImageInputError('图片派生件保存失败，消息未派发') from exc
            finally:
                if windows:
                    os.unlink(os.path.join(directory, temporary))
                else:
                    os.unlink(temporary, dir_fd=directory)
            return self._read(directory, key, expected)
