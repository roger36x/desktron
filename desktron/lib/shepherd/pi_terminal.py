"""Pi assistant terminal classification shared by legacy and unified runners."""

OUTPUT_LIMIT_MESSAGE = 'Pi 输出达到 max_tokens 上限，回答可能已被截断'
OUTPUT_LIMIT_STOPS = frozenset(('length', 'max_tokens'))


def assistant_terminal(message):
    """Map a Pi assistant message_end to Shepherd's coarse terminal states.

    Pi reports output-budget exhaustion as stopReason='length'. That is a
    confirmed native terminal, but not a successful completion: the visible
    answer may be a prefix and should not clear the turn as completed.
    """
    if not isinstance(message, dict) or message.get('role') != 'assistant':
        return ''
    stop = message.get('stopReason')
    if stop == 'aborted':
        return 'interrupted_confirmed'
    if stop == 'error' or stop in OUTPUT_LIMIT_STOPS or message.get('errorMessage'):
        return 'failed_recoverable'
    return ''


def is_output_limited(message):
    return isinstance(message, dict) and message.get('stopReason') in OUTPUT_LIMIT_STOPS


# Pi clamps max_tokens to `contextWindow - estimated input - 4096` with a floor
# of 1. A length stop after only a handful of output tokens therefore means the
# context window was exhausted, not that the answer was long.
CONTEXT_EXHAUSTED_OUTPUT_TOKENS = 256
CONTEXT_EXHAUSTED_MESSAGE = ('Pi 上下文窗口已满，自动压缩未能腾出输出空间，回答被截断；'
                             '请继续发送消息重试，仍失败时改用更大窗口的模型或新会话')


def output_limit_failure(message):
    """Return (code, user message) for a confirmed Pi length stop."""
    usage = message.get('usage') if isinstance(message, dict) else None
    output = usage.get('output') if isinstance(usage, dict) else None
    if isinstance(output, int) and not isinstance(output, bool) \
            and 0 <= output < CONTEXT_EXHAUSTED_OUTPUT_TOKENS:
        return 'CONTEXT_EXHAUSTED', CONTEXT_EXHAUSTED_MESSAGE
    return 'OUTPUT_LIMIT', OUTPUT_LIMIT_MESSAGE
