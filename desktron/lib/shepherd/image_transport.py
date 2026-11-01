"""Claude execution views, never archive mutations or network image fetches.

The conservative Claude Code policy applies regardless of account/provider.
Originals stay in TurnInputs/source; only disposable native execution views are
transformed. Derivation receipts contain hashes/sizes, never image contents.
"""
import base64
import copy
import hashlib
import io
import json
import warnings
from collections import OrderedDict

from .image_content import image_parts, from_native, _dimensions, ImageInputError

POLICY_VERSION = 'claude-code-images-v1-pillow-12.1.1'
MAX_ENCODED_IMAGE_BYTES = 4_000_000  # < the observed 5 MB Code limit
MAX_RAW_VARIANT_BYTES = 3_000_000
MAX_LONG_EDGE = 2000  # also safe for many-image API requests
MAX_DECODE_PIXELS = 32_000_000
MIN_RESIZE_LONG_EDGE = 1024
MAX_REQUEST_BYTES = 24_000_000  # room for native tools/envelope below 32 MB
MAX_REQUEST_IMAGES = 100
# Older history images may be re-encoded only when the whole request is over
# budget. A distinct policy keeps these cache keys apart from per-image variants.
HISTORY_POLICY_VERSION = POLICY_VERSION + ':history-budget-1024-q80'
HISTORY_LONG_EDGE = MIN_RESIZE_LONG_EDGE
HISTORY_JPEG_QUALITY = 80
# Planning estimate in raw bytes per output pixel for a history variant. Concept
# art measured at most 0.253; the encoded size is always checked afterwards.
HISTORY_ESTIMATE_BYTES_PER_PIXEL = 0.30


class ImageTransportError(ImageInputError):
    status = 413
    code = 'image_transport_too_large'


def validate_receipt(receipt):
    from .image_variants import variant_key
    if (not isinstance(receipt, dict) or set(receipt) != {'policy', 'derivations'}
            or receipt['policy'] != POLICY_VERSION or not isinstance(receipt['derivations'], list)
            or not 1 <= len(receipt['derivations']) <= MAX_REQUEST_IMAGES):
        raise ImageInputError('图片派生清单无效，消息未派发')
    fields = {'policy', 'source_sha256', 'variant_sha256', 'source_mime', 'variant_mime',
              'source_bytes', 'variant_bytes', 'source_size', 'variant_size',
              'jpeg_quality', 'variant_key'}
    for r in receipt['derivations']:
        if (not isinstance(r, dict) or set(r) != fields
                or r['policy'] not in (POLICY_VERSION, HISTORY_POLICY_VERSION)):
            raise ImageInputError('图片派生清单项无效，消息未派发')
        for key in ('source_sha256', 'variant_sha256', 'variant_key'):
            value = r[key]
            if not isinstance(value, str) or len(value) != 64 or any(c not in '0123456789abcdef' for c in value):
                raise ImageInputError('图片派生清单哈希无效，消息未派发')
        if (r['source_mime'] not in ('image/png','image/jpeg','image/webp','image/gif')
                or r['variant_mime'] not in ('image/png','image/jpeg')
                or type(r['source_bytes']) is not int or not 0 < r['source_bytes'] <= 12 * 1024 * 1024
                or type(r['variant_bytes']) is not int or not 0 < r['variant_bytes'] <= MAX_RAW_VARIANT_BYTES
                or r['variant_key'] != variant_key(r['policy'], r['source_mime'], r['source_sha256'])):
            raise ImageInputError('图片派生清单预算或身份无效，消息未派发')
        for key in ('source_size', 'variant_size'):
            size = r[key]
            if not isinstance(size, list) or len(size) != 2 or any(type(n) is not int or n <= 0 for n in size):
                raise ImageInputError('图片派生清单尺寸无效，消息未派发')
        if (max(r['variant_size']) > MAX_LONG_EDGE or r['source_size'][0] * r['source_size'][1] > MAX_DECODE_PIXELS
                or (r['jpeg_quality'] is not None and r['jpeg_quality'] not in (95,90,85,80))):
            raise ImageInputError('图片派生清单尺寸或质量超过预算，消息未派发')


def _sha(raw):
    return hashlib.sha256(raw).hexdigest()


def _json_size(value):
    # Same encoding as the request measurement, so block sizes add up exactly.
    return len(json.dumps(value, ensure_ascii=False, sort_keys=True).encode('utf-8'))


def _count_images(blocks):
    # Count only protocol blocks, not image-looking JSON in tool arguments.
    return sum(1 if b.get('type') == 'image' else
               _count_images(b.get('content')) if b.get('type') == 'tool_result' else 0
               for b in (blocks if isinstance(blocks, list) else []) if isinstance(b, dict))


def _omitted_block(width, height, source_hash):
    return {'type': 'text', 'text': (
        '[Desk：这张较早的历史图片未随本次请求发送，以满足 Claude 请求传输预算；'
        f'原图仍保存在会话存档中（{width}×{height}，SHA-256 {source_hash[:12]}）。'
        '如仍需查看，可重新读取原文件。]')}


class _HistoryImage:
    """One history image slot while fitting a request; never persisted."""
    def __init__(self, blocks, index, block, decoded):
        self.blocks, self.index, self.block = blocks, index, block
        self.mime, self.data, self.raw, self.width, self.height = decoded
        self.hash = _sha(self.raw)
        self.oversized = (len(self.data) > MAX_ENCODED_IMAGE_BYTES
                          or max(self.width, self.height) > MAX_LONG_EDGE)
        self.decodable = self.width * self.height <= MAX_DECODE_PIXELS
        self.sendable = not self.oversized or self.decodable
        self.placeholder = _omitted_block(self.width, self.height, self.hash)
        self.placeholder_size = _json_size(self.placeholder)
        overhead = _json_size(block) - len(self.data)
        # Unknown per-image variants are bounded by MAX_ENCODED_IMAGE_BYTES.
        self.base_upper = overhead + (MAX_ENCODED_IMAGE_BYTES if self.oversized else len(self.data))
        scale = min(1.0, HISTORY_LONG_EDGE / max(self.width, self.height))
        pixels = max(1, round(self.width * scale)) * max(1, round(self.height * scale))
        estimate = overhead + -(-int(pixels * HISTORY_ESTIMATE_BYTES_PER_PIXEL) * 4 // 3)
        self.estimate = min(self.base_upper, estimate)
        self.final = self.final_size = None
        self.receipt = self.policy = None
        self.compressed = False
        self.compressible = self.decodable

    def cost(self, size):
        return size - self.placeholder_size


def _pillow():
    try:
        from PIL import Image, ImageOps, __version__
    except ImportError as exc:
        raise ImageInputError('图片预处理依赖 Pillow 缺失；请运行 desktron prepare，消息未派发') from exc
    if __version__ != '12.1.1':
        raise ImageInputError('图片预处理依赖版本不匹配；请运行 desktron prepare，消息未派发')
    return Image, ImageOps


class ClaudeImageTransport:
    """Bounded invocation-local reuse; no global cache, files, or URL access."""
    def __init__(self, store_root=None, conversation_id=None):
        from .image_variants import ImageVariants
        self.variants = ImageVariants(store_root, conversation_id) if store_root is not None else None
        self.cache = OrderedDict()
        self.derivations = OrderedDict()
        self.entry_sources = set()
        self._collecting_entries = False
        # Last fit_entries() outcome, for the user-visible notice only.
        self.history_compressed = 0
        self.omitted = 0

    @staticmethod
    def _decode(block):
        mime, data = image_parts(from_native(block, 'claude'))
        raw = base64.b64decode(data, validate=True)
        width, height = _dimensions(raw, mime)
        if not width or not height:
            raise ImageInputError('图片尺寸无效，消息未派发')
        return mime, data, raw, width, height

    def _variant(self, policy, raw, mime, width, height, create):
        """Return (cache key, variant, receipt) without recording a derivation."""
        source_hash = _sha(raw)
        key = (policy, mime, source_hash)
        if key in self.cache:
            self.cache.move_to_end(key)
            variant, receipt = self.cache[key]
        else:
            variant, receipt = (self.variants.get(policy, mime, source_hash,
                (width, height), len(raw), create) if self.variants else create())
            self.cache[key] = (variant, receipt)
            if len(self.cache) > 8:
                self.cache.popitem(last=False)
        return key, variant, receipt

    @staticmethod
    def _variant_block(block, variant):
        return {**block, 'source': {'type': 'base64', 'media_type': variant[0],
                                  'data': base64.b64encode(variant[1]).decode('ascii')}}

    def image(self, block, *, transform=True):
        mime, data, raw, width, height = self._decode(block)
        oversized = len(data) > MAX_ENCODED_IMAGE_BYTES or max(width, height) > MAX_LONG_EDGE
        if not oversized:
            return copy.deepcopy(block)
        if not transform:
            raise ImageTransportError('旧私有 Claude 历史图片超过传输预算；需从统一历史重建，未派发')
        if width * height > MAX_DECODE_PIXELS:
            raise ImageTransportError('图片解码超过 3200 万像素安全预算；请裁剪后发送，消息未派发')
        key, variant, receipt = self._variant(POLICY_VERSION, raw, mime, width, height,
            lambda: self._compress(raw, mime, width, height))
        if self._collecting_entries:
            self.entry_sources.add(key[2])
        self.derivations[key] = receipt
        return self._variant_block(block, variant)

    def _compress(self, raw, mime, width, height):
        Image, ImageOps = _pillow()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as opened:
                    opened.seek(0)  # Claude supports only the first animation frame.
                    opened.load()
                    image = ImageOps.exif_transpose(opened).convert('RGBA')
            original_size = image.size
            long_edge = min(max(image.size), MAX_LONG_EDGE)
            floor = min(long_edge, MIN_RESIZE_LONG_EDGE)
            while True:
                scale = long_edge / max(original_size)
                size = tuple(max(1, round(n * scale)) for n in original_size)
                resized = image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image
                # Lossless first for text/screenshots. Metadata is not copied.
                out = io.BytesIO(); resized.save(out, format='PNG', optimize=True)
                candidate, kind, quality = out.getvalue(), 'image/png', None
                if len(candidate) > MAX_RAW_VARIANT_BYTES:
                    rgb = Image.new('RGB', resized.size, 'white')
                    rgb.paste(resized, mask=resized.getchannel('A'))
                    for q in (95, 90, 85, 80):
                        out = io.BytesIO(); rgb.save(out, format='JPEG', quality=q,
                                                   subsampling=0, optimize=False)
                        candidate, kind, quality = out.getvalue(), 'image/jpeg', q
                        if len(candidate) <= MAX_RAW_VARIANT_BYTES:
                            break
                if len(candidate) <= MAX_RAW_VARIANT_BYTES:
                    receipt = {'policy': POLICY_VERSION, 'source_sha256': _sha(raw),
                        'variant_sha256': _sha(candidate), 'source_mime': mime,
                        'variant_mime': kind, 'source_bytes': len(raw),
                        'variant_bytes': len(candidate), 'source_size': [width, height],
                        'variant_size': list(size), 'jpeg_quality': quality}
                    return (kind, candidate), receipt
                if long_edge == floor:
                    break
                long_edge = max(floor, int(long_edge * 0.85))
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageInputError('图片无法安全解码或压缩，消息未派发') from exc
        raise ImageTransportError('无法在保留基本分辨率的同时满足图片预算；请裁剪或分轮发送，未派发')

    def _compress_history(self, raw, mime, width, height):
        """Smallest bounded rendition of an older history image (long edge 1024)."""
        Image, ImageOps = _pillow()
        try:
            with warnings.catch_warnings():
                warnings.simplefilter('error', Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw)) as opened:
                    opened.seek(0)  # Claude supports only the first animation frame.
                    opened.load()
                    alpha = 'A' in opened.getbands() or 'transparency' in opened.info
                    image = ImageOps.exif_transpose(opened).convert('RGBA')
            scale = min(1.0, HISTORY_LONG_EDGE / max(image.size))
            size = tuple(max(1, round(n * scale)) for n in image.size)
            resized = image.resize(size, Image.Resampling.LANCZOS) if size != image.size else image
            rgb = Image.new('RGB', resized.size, 'white')
            rgb.paste(resized, mask=resized.getchannel('A'))
            out = io.BytesIO(); rgb.save(out, format='JPEG', quality=HISTORY_JPEG_QUALITY)
            candidate, kind, quality = out.getvalue(), 'image/jpeg', HISTORY_JPEG_QUALITY
            if mime == 'image/png':
                # Screenshots are often both smaller and sharper as PNG.
                out = io.BytesIO(); (resized if alpha else rgb).save(out, format='PNG')
                if len(out.getvalue()) < len(candidate):
                    candidate, kind, quality = out.getvalue(), 'image/png', None
        except (OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageInputError('图片无法安全解码或压缩，消息未派发') from exc
        if len(candidate) > MAX_RAW_VARIANT_BYTES:
            raise ImageTransportError('历史图片压缩后仍超过单图预算，未派发')
        receipt = {'policy': HISTORY_POLICY_VERSION, 'source_sha256': _sha(raw),
            'variant_sha256': _sha(candidate), 'source_mime': mime, 'variant_mime': kind,
            'source_bytes': len(raw), 'variant_bytes': len(candidate),
            'source_size': [width, height], 'variant_size': list(size), 'jpeg_quality': quality}
        return (kind, candidate), receipt

    def _base_history(self, slot):
        """History image as sent without budget pressure (per-image policy only)."""
        if not slot.oversized:
            slot.final, slot.receipt, slot.policy = copy.deepcopy(slot.block), None, None
        else:
            key, variant, slot.receipt = self._variant(POLICY_VERSION, slot.raw, slot.mime,
                slot.width, slot.height,
                lambda: self._compress(slot.raw, slot.mime, slot.width, slot.height))
            slot.final, slot.policy = self._variant_block(slot.block, variant), key[0]
        slot.final_size = _json_size(slot.final)

    def _shrink_history(self, slot):
        """Replace slot.final with the history variant when that is smaller."""
        slot.compressible = False
        try:
            key, variant, receipt = self._variant(HISTORY_POLICY_VERSION, slot.raw, slot.mime,
                slot.width, slot.height,
                lambda: self._compress_history(slot.raw, slot.mime, slot.width, slot.height))
        except ImageTransportError:
            return False
        block = self._variant_block(slot.block, variant)
        size = _json_size(block)
        # Compare with what would be sent otherwise; an unknown per-image
        # variant of an oversized original is assumed larger.
        current = slot.final_size if slot.final is not None else (
            None if slot.oversized else slot.base_upper)
        if current is not None and size >= current:
            return False
        slot.final, slot.final_size, slot.receipt, slot.policy = block, size, receipt, key[0]
        slot.compressed = True
        return True

    def fit_entries(self, entries, frame, *, prompt='', system='', grounding=''):
        """Prepare history so the whole Claude request fits the transport budget.

        Current-turn images and every non-image block are left unchanged. Older
        history images are planned newest-first with a conservative compressed
        size estimate; an image that would not fit even compressed is omitted
        together with every older image, before any encoding. Kept images are
        then compressed oldest-first only until the request fits, and the exact
        size decides the rest: compress the next kept image, otherwise omit the
        oldest kept one. Originals in the archive are never rewritten.
        """
        prepared = copy.deepcopy(list(entries))
        self.entry_sources = set(); self.history_compressed = 0; self.omitted = 0
        slots = []

        def collect(blocks):
            if not isinstance(blocks, list):
                return
            for index, block in enumerate(blocks):
                if not isinstance(block, dict):
                    continue
                if block.get('type') == 'image':
                    slots.append(_HistoryImage(blocks, index, block, self._decode(block)))
                elif block.get('type') == 'tool_result':
                    collect(block.get('content'))
        for entry in prepared:
            message = entry.get('message') if isinstance(entry, dict) else None
            if isinstance(message, dict) and message.get('role') in ('user', 'assistant'):
                collect(message.get('content'))

        frame_images = _count_images(frame['message'].get('content')) if frame is not None else 0
        image_room = MAX_REQUEST_IMAGES - frame_images
        if image_room < 0:
            raise ImageTransportError('Claude 请求图片超过 100 张安全预算；请压缩历史或分轮发送，未派发')
        whole = self.request_size(prepared, frame, prompt=prompt, system=system, grounding=grounding)
        # The request with every history image replaced by its placeholder.
        fixed = whole - sum(s.cost(_json_size(s.block)) for s in slots)
        room = MAX_REQUEST_BYTES - fixed
        if room < 0:
            raise ImageTransportError('Claude 整请求超过 24 MB 传输安全预算；省略全部历史图片后仍超出，'
                                      '请减少本轮附件或压缩历史，未派发')

        # 1. Plan by estimate: keep newest images while even their compressed
        #    estimate fits; the first that does not is omitted with all older ones.
        kept, planned = [], 0
        for slot in reversed(slots):
            if not slot.sendable:
                continue
            if len(kept) >= image_room or planned + slot.cost(slot.estimate) > room:
                break
            kept.append(slot); planned += slot.cost(slot.estimate)
        kept.reverse()

        # 2. Compress kept images oldest-first only while the upper bound is
        #    exceeded; unknown per-image variants count at their maximum.
        bound = sum(s.cost(s.base_upper) for s in kept)
        for slot in kept:
            if bound <= room:
                break
            if self._shrink_history(slot):
                bound += slot.final_size - slot.base_upper
        for slot in list(kept):
            if slot.final is None:
                try:
                    self._base_history(slot)
                except ImageTransportError:
                    kept.remove(slot)  # cannot be sent at any size; omit it

        # 3. Exact sizes decide: compress the next kept image, else omit the oldest.
        used = sum(s.cost(s.final_size) for s in kept)
        while used > room and kept:
            slot = next((s for s in kept if s.compressible), None)
            if slot is None:
                slot = kept.pop(0)
                used -= slot.cost(slot.final_size)
                continue
            before = slot.final_size
            if self._shrink_history(slot):
                used += slot.final_size - before

        retained = {id(s) for s in kept}
        for slot in slots:
            if id(slot) in retained:
                slot.blocks[slot.index] = slot.final
                if slot.receipt is not None:
                    self.derivations[(slot.policy, slot.mime, slot.hash)] = slot.receipt
                if slot.compressed:
                    self.history_compressed += 1
                    self.entry_sources.add('history:' + slot.hash)
                elif slot.receipt is not None:
                    self.entry_sources.add(slot.hash)
            else:
                slot.blocks[slot.index] = slot.placeholder
                self.omitted += 1
                self.entry_sources.add('omitted:' + slot.hash)
        self.validate_request(prepared, frame, prompt=prompt, system=system, grounding=grounding)
        return prepared

    def content(self, blocks, *, transform=True):
        if not isinstance(blocks, list):
            return copy.deepcopy(blocks)
        out = []
        for block in blocks:
            if not isinstance(block, dict):
                out.append(copy.deepcopy(block)); continue
            if block.get('type') == 'image':
                out.append(self.image(block, transform=transform))
            elif block.get('type') == 'tool_result':
                out.append({**copy.deepcopy(block),
                    'content': self.content(block.get('content'), transform=transform)})
            else:
                out.append(copy.deepcopy(block))
        return out

    def entries(self, entries, *, transform=True):
        out = []
        self.entry_sources = set()
        self._collecting_entries = True
        try:
            for entry in entries:
                result = copy.deepcopy(entry)
                message = result.get('message') if isinstance(result, dict) else None
                if isinstance(message, dict) and message.get('role') in ('user', 'assistant'):
                    message['content'] = self.content(message.get('content'), transform=transform)
                out.append(result)
            return out
        finally:
            self._collecting_entries = False

    def frame(self, frame):
        if frame is None:
            return None
        result = copy.deepcopy(frame)
        if not isinstance(result, dict) or not isinstance(result.get('message'), dict):
            raise ImageInputError('Claude 图片用户帧无效，消息未派发')
        message = result['message']
        message['content'] = self.content(message.get('content'))
        return result

    @staticmethod
    def request_size(entries, frame, *, prompt='', system='', grounding=''):
        return _json_size({'entries': entries, 'input': frame if frame is not None else prompt,
                           'system': system, 'grounding': grounding})

    def validate_request(self, entries, frame, *, prompt='', system='', grounding=''):
        messages = [e.get('message') for e in entries if isinstance(e, dict)
                    and isinstance(e.get('message'), dict)]
        if frame is not None:
            messages.append(frame['message'])
        if sum(_count_images(m.get('content')) for m in messages) > MAX_REQUEST_IMAGES:
            raise ImageTransportError('Claude 请求图片超过 100 张安全预算；请压缩历史或分轮发送，未派发')
        size = self.request_size(entries, frame, prompt=prompt, system=system, grounding=grounding)
        if size > MAX_REQUEST_BYTES:
            raise ImageTransportError('Claude 整请求超过 24 MB 传输安全预算；请减少附件或压缩历史，未派发')
        return size

    def receipt(self):
        return {'policy': POLICY_VERSION, 'derivations': list(self.derivations.values())}
