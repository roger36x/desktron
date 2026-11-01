"""Inline image protocol shared by native readers and history writers.

Only durable bytes are portable: never fetch a URL or dereference a file path
found in native history. Manifest/receipts still contain hashes and locators,
not this materialized projection.
"""
import base64
import binascii
import copy
import struct
import zlib

from .store import InvalidState

MAX_IMAGE_BYTES = 12 * 1024 * 1024
MIME_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
MODEL_IMAGE_PATCH_EDGE = 32
MODEL_IMAGE_PATCH_LIMIT = 30000


class ImageInputError(InvalidState):
    status = 400
    code = 'invalid_image'


def _intact(raw, mime):
    if mime == 'image/png':
        offset, header, pixels = 8, False, False
        while offset + 12 <= len(raw):
            size = int.from_bytes(raw[offset:offset+4], 'big')
            end = offset + 12 + size
            if end > len(raw): return False
            kind = raw[offset+4:offset+8]
            if zlib.crc32(raw[offset+4:end-4]) != int.from_bytes(raw[end-4:end], 'big'):
                return False
            if offset == 8:
                if kind != b'IHDR' or size != 13: return False
                width, height = struct.unpack('>II',raw[offset+8:offset+16])
                header = bool(width and height)
            if kind == b'IDAT': pixels = True
            if kind == b'IEND': return header and pixels and size == 0 and end == len(raw)
            offset = end
        return False
    if mime == 'image/jpeg': return raw.endswith(b'\xff\xd9')
    if mime == 'image/gif': return raw.endswith(b';')
    return len(raw) >= 20 and int.from_bytes(raw[4:8], 'little') + 8 == len(raw)


def _dimensions(raw, mime):
    if mime == 'image/png' and len(raw) >= 24:
        return struct.unpack('>II', raw[16:24])
    if mime == 'image/gif' and len(raw) >= 10:
        return struct.unpack('<HH', raw[6:10])
    if mime == 'image/webp' and len(raw) >= 20:
        kind, payload = raw[12:16], raw[20:]
        if kind == b'VP8X' and len(payload) >= 10:
            return (1 + int.from_bytes(payload[4:7], 'little'),
                    1 + int.from_bytes(payload[7:10], 'little'))
        if kind == b'VP8L' and len(payload) >= 5 and payload[0] == 0x2f:
            bits = int.from_bytes(payload[1:5], 'little')
            return 1 + (bits & 0x3fff), 1 + ((bits >> 14) & 0x3fff)
        if kind == b'VP8 ' and len(payload) >= 10 and payload[3:6] == b'\x9d\x01\x2a':
            return (int.from_bytes(payload[6:8], 'little') & 0x3fff,
                    int.from_bytes(payload[8:10], 'little') & 0x3fff)
    if mime == 'image/jpeg':
        offset = 2
        while offset < len(raw):
            while offset < len(raw) and raw[offset] != 0xff:
                offset += 1
            while offset < len(raw) and raw[offset] == 0xff:
                offset += 1
            if offset >= len(raw):
                break
            marker = raw[offset]
            offset += 1
            if marker == 0x00:
                continue
            if marker in (0x01, 0xd8) or 0xd0 <= marker <= 0xd7:
                continue
            if marker in (0xd9, 0xda) or offset + 2 > len(raw):
                break
            size = int.from_bytes(raw[offset:offset + 2], 'big')
            if size < 2 or offset + size > len(raw):
                break
            if marker in {0xc0, 0xc1, 0xc2, 0xc3, 0xc5, 0xc6, 0xc7,
                          0xc9, 0xca, 0xcb, 0xcd, 0xce, 0xcf}:
                if size >= 7:
                    return (int.from_bytes(raw[offset + 5:offset + 7], 'big'),
                            int.from_bytes(raw[offset + 3:offset + 5], 'big'))
                break
            offset += size
    return 0, 0


def _model_image_info(raw, mime):
    width, height = _dimensions(raw, mime)
    if not width or not height:
        raise InvalidState('Shepherd 图片尺寸无效')
    patches = ((width + MODEL_IMAGE_PATCH_EDGE - 1) // MODEL_IMAGE_PATCH_EDGE
               * ((height + MODEL_IMAGE_PATCH_EDGE - 1) // MODEL_IMAGE_PATCH_EDGE))
    return {'width': width, 'height': height, 'patches': patches,
            'limit': MODEL_IMAGE_PATCH_LIMIT}


def model_image_info(data, mime):
    canonical = image_block(data, mime)
    raw = base64.b64decode(canonical['image_url'].split(',', 1)[1], validate=True)
    return _model_image_info(raw, mime)


def model_patch_limited(engine, model='', provider=''):
    if engine == 'codex':
        return True
    if engine != 'pi':
        return False
    provider = str(provider or '').strip()
    model = str(model or '').strip()
    if '/' in model:
        provider = model.split('/', 1)[0]
    return provider == 'openai'


def image_block(data, mime):
    if not isinstance(mime, str) or mime not in MIME_TYPES or not isinstance(data, str) or not data:
        raise InvalidState('Shepherd 图片须为 PNG、JPEG、GIF 或 WebP 的 base64 内容')
    if len(data) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise InvalidState('Shepherd 单张图片不能超过 12MB')
    try:
        raw = base64.b64decode(data, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise InvalidState('Shepherd 图片 base64 无效') from exc
    matches = {
        'image/png': raw.startswith(b'\x89PNG\r\n\x1a\n'),
        'image/jpeg': raw.startswith(b'\xff\xd8\xff'),
        'image/gif': raw.startswith((b'GIF87a', b'GIF89a')),
        'image/webp': raw[:4] == b'RIFF' and raw[8:12] == b'WEBP',
    }
    if len(raw) > MAX_IMAGE_BYTES or not matches[mime] or not _intact(raw, mime):
        raise InvalidState('Shepherd 图片内容与格式不符，或超过 12MB')
    return {'type': 'input_image',
            'image_url': 'data:' + mime + ';base64,' + base64.b64encode(raw).decode('ascii')}


def image_parts(block):
    url = block.get('image_url') if isinstance(block, dict) else None
    if not isinstance(url, str) or not url.startswith('data:') or ';base64,' not in url:
        raise InvalidState('Shepherd 图片必须包含持久化字节，不接受外部 URL 或本地路径')
    mime, data = url[5:].split(';base64,', 1)
    canonical = image_block(data, mime)
    return mime, canonical['image_url'].split(',', 1)[1]


def from_native(block, engine):
    if engine == 'claude':
        source = block.get('source') or {}
        if not isinstance(source, dict) or source.get('type') != 'base64':
            raise InvalidState('Shepherd Claude 图片缺少持久化 base64 来源')
        return image_block(source.get('data'), source.get('media_type'))
    if engine == 'pi':
        return image_block(block.get('data'), block.get('mimeType'))
    mime, data = image_parts(block)
    return image_block(data, mime)


def restore_native_images(items, trusted_public):
    """Replace Codex-family user-image locators with immutable Turn Input bytes.

    The engine may persist a remote locator even when Desk submitted a local image.  The
    locator is evidence that an image slot existed, but never authority for its
    bytes: do not fetch it.  The ordered, hash-bound Turn Input is the sole byte
    source, and every native user-image slot must match it one-for-one. Tool
    outputs have no Turn Input sidecar, so their native bytes must already be
    inline; a locator remains fail-closed in public_content().
    """
    trusted = []
    for item in trusted_public or []:
        if not isinstance(item, dict) or item.get('type') != 'message' or item.get('role') != 'user':
            continue
        for block in item.get('content') or []:
            if isinstance(block, dict) and block.get('type') == 'input_image':
                mime, data = image_parts(block)
                trusted.append(image_block(data, mime))
    slots = []
    for item in items:
        if not isinstance(item, dict) or item.get('type') != 'message' or item.get('role') != 'user':
            continue
        for block in item.get('content') or []:
            if isinstance(block, dict) and block.get('type') == 'input_image':
                slots.append(block)
    if not trusted:
        return items
    if len(slots) != len(trusted):
        raise InvalidState('Shepherd 原生图片槽位与持久化 Turn Input 不匹配')
    restored = copy.deepcopy(items)
    index = 0
    for item in restored:
        if not isinstance(item, dict) or item.get('type') != 'message' or item.get('role') != 'user':
            continue
        for offset, block in enumerate(item.get('content') or []):
            if isinstance(block, dict) and block.get('type') == 'input_image':
                item['content'][offset] = {**trusted[index],
                                           **({'detail': block['detail']} if block.get('detail') else {})}
                index += 1
    return restored


def native_content(content, engine, *, model_image_limit=False):
    """Convert an ordered multimodal user message; text-only codecs stay v1."""
    out = []
    for block in content:
        if not isinstance(block, dict):
            raise InvalidState('Shepherd 图片消息内容无效')
        kind = block.get('type')
        if kind in ('text', 'input_text'):
            out.append({'type': 'input_text' if engine == 'codex' else 'text',
                        'text': str(block.get('text') or '')})
        elif kind == 'input_image':
            mime, data = image_parts(block)
            info = _model_image_info(base64.b64decode(data, validate=True), mime)
            if model_image_limit and info['patches'] > MODEL_IMAGE_PATCH_LIMIT:
                marker = ('[图片因尺寸超过目标模型限制未带入工作上下文；原图仍保留在会话历史。'
                          f'原始尺寸 {info["width"]}×{info["height"]}。]')
                out.append({'type': 'input_text' if engine == 'codex' else 'text',
                            'text': marker})
            elif engine == 'claude':
                out.append({'type': 'image', 'source': {
                    'type': 'base64', 'media_type': mime, 'data': data}})
            elif engine == 'pi':
                out.append({'type': 'image', 'mimeType': mime, 'data': data})
            else:
                # Codex otherwise resizes large injected images and break
                # exact marker readback. Native originals remain authoritative.
                out.append({**image_block(data, mime), 'detail': 'original'})
        else:
            raise InvalidState('Shepherd 图片消息包含不支持的内容')
    return out


def has_images(content, kind='input_image'):
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get('type') == kind for block in content)


def public_content(content, engine):
    out = []
    for block in content:
        if not isinstance(block, dict):
            raise InvalidState('Shepherd 原生图片消息内容无效')
        if block.get('type') in ('text', 'input_text'):
            out.append({'type': 'input_text', 'text': str(block.get('text') or '')})
        elif block.get('type') == ('input_image' if engine == 'codex' else 'image'):
            out.append(from_native(block, engine))
        else:
            raise InvalidState('Shepherd 原生图片消息包含不支持的内容')
    return out
