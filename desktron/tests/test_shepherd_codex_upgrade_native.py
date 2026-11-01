"""Engine upgrade gates through the product dispatcher, using only loopback models."""
import base64
import hashlib
from pathlib import Path
import tempfile
import shlex
import shutil
import subprocess
from unittest import mock

import pytest

from shepherd.coordinator import digest
from shepherd.turn_inputs import TurnInputs
from shepherd.projector import ReadProjector
from shepherd.source_resolver import SourceResolver
from shepherd_product_fixture import ProductFixture
from test_shepherd_images import fixture_png


@pytest.mark.native
@pytest.mark.parametrize('engine', ['codex','claude'])
def test_unknown_version_uses_actual_protocol_not_release_number(tmp_path, engine):
    from codex_protocol import availability
    binary = shutil.which(engine)
    assert binary
    wrapper = tmp_path / engine
    wrapper.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "codex-cli 99.0.0"; '
                       'else exec ' + shlex.quote(binary) + ' "$@"; fi\n')
    wrapper.chmod(0o700)
    assert '99.0.0' in subprocess.check_output([str(wrapper), '--version'], text=True)
    if engine == 'claude':
        from shepherd.claude_connector import ClaudeConnector
        sdk = Path.home()/'.local/share/desktron/runtime/claude-agent-sdk/node_modules/@anthropic-ai/claude-agent-sdk'
        assert ClaudeConnector.availability(sdk, str(wrapper))['available']
    else:
        assert availability(str(wrapper), engine=engine)['available']


@pytest.mark.native
def test_codex_send_image_switch_compact_and_reopen():
    with tempfile.TemporaryDirectory() as root:
        product = ProductFixture(root)
        try:
            png = fixture_png(32, 32)
            upload = Path(root) / 'upload.png'
            upload.write_bytes(base64.b64decode(png))
            image_hash = hashlib.sha256(upload.read_bytes()).hexdigest()
            ctx = product.context('codex', 'first image question')
            ctx['saved'] = [('image', str(upload), 'upload.png', 'image/png', png)]
            assert product.dispatch('codex', ctx) is None
            assert '_projection_failures' not in ctx
            assert any(image_hash in hashes for hashes in product.model.observations)
            state = product.adapter.service.coordinator.state(product.sid)
            first = state['turns'][0]
            binding = state['bindings'][first['producer_binding_id']]
            trusted = TurnInputs(product.adapter.service.store.root).read(first['input_ref'])
            source = product.adapter.service.connector(binding).resolve_source(
                first['source_ref'], trusted_public=trusted)
            assert digest(source['source']) == first['source_hash']
            # Canonical history also includes the durable user input when the
            # native notification stream contains only the assistant response.
            reader = ReadProjector(product.adapter.service.store,
                SourceResolver([product.adapter.service.connector(binding)]))
            public = reader._public(first)
            assert digest(public) == first['public_hash']
            upload.unlink()
            # A cold model/engine path must read old evidence, not replay tools or
            # depend on the original upload or a warm app-server connection.
            for engine in ('pi', 'claude', 'codex', 'codex'):
                start = len(product.model.observations)
                error, ctx = product.turn(engine, 'continue with historical image')
                assert error is None
                assert '_projection_failures' not in ctx
                assert any(image_hash in hashes for hashes in product.model.observations[start:]), engine
            result, ctx = product.compact('codex')
            assert result['ok'], result
            assert ctx['_shepherd_runner'].checkpoint is not None
            product.reopen()
            error, ctx = product.turn('codex', 'continue after compact and reopen')
            assert error is None
            assert '_projection_failures' not in ctx
            service = product.adapter.service
            resolved = service.connector(binding).resolve_source(first['source_ref'], trusted_public=trusted)
            assert resolved == source
            reader = ReadProjector(service.store, SourceResolver([service.connector(binding)]))
            assert reader._public(first) == public
            state = service.coordinator.state(product.sid)
            assert state.get('active_turn') is None
            assert all(row['outcome'] == 'completed' for row in state['turns'])
            assert len(state['turns']) == 8
        finally:
            product.close()
