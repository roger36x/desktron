"""Model admission estimates, separate from archive/JSON byte budgets.

Vision usage depends on the provider's preprocessing and model. Reserve 16k
tokens per validated image for local planning; this is neither a billing
estimate nor a universal upper bound. Native admission remains authoritative.
Only protocol image blocks lose their transport bytes in this temporary view.
"""
from .coordinator import estimate_tokens
from .image_content import from_native
from .store import InvalidState


IMAGE_TOKEN_RESERVE = 16384


def estimate_context_tokens(value):
    """Count text normally and images separately without changing the input.

Walk message envelopes/content only. JSON inside tool arguments, text, or
custom metadata is ordinary text even if it resembles an image block.
"""
    images = 0

    def content(blocks):
        nonlocal images
        if not isinstance(blocks, list):
            return blocks
        result = []
        for block in blocks:
            if not isinstance(block, dict):
                result.append(block)
                continue
            kind = block.get('type')
            if kind in ('input_image', 'image'):
                engine = ('codex' if kind == 'input_image' else
                          'claude' if 'source' in block else 'pi')
                images += 1
                # Only validated inline bytes receive a transport discount.
                # Native checkpoints may contain opaque image locators. Keep
                # those (and malformed blocks) fully charged, without fetching
                # or adding a new validation policy to native restoration.
                try:
                    from_native(block, engine)
                except InvalidState:
                    result.append(block)
                    continue
                if engine == 'codex':
                    block = {**block, 'image_url': ''}
                elif engine == 'claude':
                    block = {**block, 'source': {**block['source'], 'data': ''}}
                else:
                    block = {**block, 'data': ''}
            elif kind == 'tool_result':
                block = {**block, 'content': content(block.get('content'))}
            result.append(block)
        return result

    def messages(node):
        if isinstance(node, list):
            return [messages(item) for item in node]
        if not isinstance(node, dict):
            return node
        result = dict(node)
        if node.get('role') in ('user', 'assistant', 'system', 'tool', 'toolResult'):
            if 'content' in node:
                result['content'] = content(node['content'])
        if node.get('type') in ('function_call_output', 'custom_tool_call_output'):
            result['output'] = content(node.get('output'))
        if isinstance(node.get('message'), dict) and node.get('type') in ('user', 'assistant', 'message'):
            result['message'] = messages(node['message'])
        if node.get('type') == 'prompt' and isinstance(node.get('message'), str) and 'images' in node:
            result['images'] = content(node['images'])
        return result

    # Runner's budget envelope; its system/grounding strings stay byte-counted.
    if isinstance(value, dict) and set(value) == {'system', 'input', 'grounding'}:
        view = {**value, 'input': messages(value['input'])}
    else:
        view = messages(value)
    return estimate_tokens(view) + images * IMAGE_TOKEN_RESERVE
