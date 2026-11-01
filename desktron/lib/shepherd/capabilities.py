"""Connector capability matrix; unverified engines remain unavailable."""
import copy

from .claude_codec import CODEC_VERSION
from .claude_connector import ClaudeConnector
from .codex_connector import CodexFamilyConnector
from .codex_codec import CODEC_VERSIONS
from .pi_connector import PiConnector


_CAPABILITIES = {
    'claude': {
        'connector_version': CODEC_VERSION,
        'native_persistence': True,
        'incremental_append': True,
        'readback_verify': True,
        'portable_checkpoint': True,
        'tool_history': True,
    },
    'codex': {'connector_version': CODEC_VERSIONS['codex'],
              'native_persistence': True, 'incremental_append': True,
              'readback_verify': True, 'portable_checkpoint': True,
              'tool_history': True},
    'pi': {'connector_version': PiConnector.connector_version, 'native_persistence': True,
           'incremental_append': True, 'readback_verify': True,
           'portable_checkpoint': True, 'tool_history': True},
}


def matrix(claude_paths=None, pi_paths=None, codex_paths=None, *, probe=True):
    result = copy.deepcopy(_CAPABILITIES)
    if not probe:
        for item in result.values():
            item['availability'] = {'available': False, 'reason': 'not_checked',
                                    'assurance': 'declaration-only'}
            item['enabled'] = False
        return result
    result['claude']['availability'] = ClaudeConnector.availability(**(claude_paths or {}))
    result['claude']['enabled'] = bool(result['claude']['availability']['available'])
    result['pi']['availability'] = PiConnector.availability(**(pi_paths or {}))
    result['pi']['enabled'] = bool(result['pi']['availability']['available'])
    for engine, paths in (('codex', codex_paths),):
        result[engine]['availability'] = CodexFamilyConnector.availability(engine, **(paths or {}))
        result[engine]['enabled'] = bool(result[engine]['availability']['available'])
    return result
