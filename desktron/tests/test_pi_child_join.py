"""Joined dependencies keep their original parent ownership across recovery."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest
from test_pi_children import bridge, launch
from pi_children import PiChildren, _save


@pytest.mark.parametrize('state,outcome', [('complete', 'completed'), ('failed', 'failed'), ('partial', 'failed')])
def test_joined_result_is_projected_but_never_notified(bridge, state, outcome):
    meta, _ = launch(bridge, state=state)
    meta.update(deliveryPolicy='join', parentInvocationId='parent-invocation')
    _save(bridge._path('parent', meta['id']).parent / 'launch.json', meta)
    bridge.owner.prompt_async = lambda *a, **k: pytest.fail('joined dependency must not enqueue another parent')
    bridge.refresh()
    row = bridge._read('parent', meta['id'])
    assert row['outcome'] == outcome
    assert row['delivery'] == 'joined_dependency'
    assert row['parentInvocationId'] == 'parent-invocation'
    assert 'fixture result' in row['result']
    restored = PiChildren(bridge.adapter)
    try:
        restored.refresh()
        assert not restored.pending
        assert restored._read('parent', meta['id'])['delivery'] == 'joined_dependency'
    finally:
        restored.close()


def test_uncertain_join_consumption_is_not_converted_to_notification(bridge):
    meta, _ = launch(bridge, state='complete')
    meta.update(deliveryPolicy='join', parentInvocationId='old-parent')
    directory = bridge._path('parent', meta['id']).parent
    _save(directory / 'launch.json', meta)
    _save(directory / 'join.json', {'state': 'offered', 'parentInvocationId': 'old-parent'})
    bridge.owner.prompt_async = lambda *a, **k: pytest.fail('uncertain consumption must remain held')
    bridge.refresh(); bridge.refresh()
    assert json.loads((directory / 'join.json').read_text())['state'] == 'offered'
    assert not bridge.pending
    # Even a direct caller cannot bypass the ownership gate.
    bridge.deliver(bridge._read('parent', meta['id']))
    assert '不能转为独立通知' in bridge._read('parent', meta['id'])['delivery_error']


def test_independent_notification_is_unaffected_by_joined_sibling(bridge):
    meta, _ = launch(bridge, rid='joined', state='complete')
    meta.update(deliveryPolicy='join', parentInvocationId='parent-invocation')
    _save(bridge._path('parent', meta['id']).parent / 'launch.json', meta)
    launch(bridge, rid='independent', state='complete')
    delivered = []
    bridge.owner.prompt_async = lambda *a, **k: delivered.append(k) or {'status': 'finished'}
    bridge.owner.request_status = lambda *a: {'status': 'finished'}
    bridge.refresh(); bridge.refresh()
    assert len(delivered) == 1
    assert delivered[0]['client_request_id'] == 'pi-child-independent'
    assert bridge._read('parent', 'joined')['delivery'] == 'joined_dependency'


def test_node_dependency_ledger_regressions():
    node = shutil.which('node')
    if not node:
        pytest.skip('Node required for the Pi dependency ledger')
    result = subprocess.run([node, '--test', str(Path(__file__).with_name('pi_child_join.test.mjs'))],
        capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


def test_user_cancellation_stops_joined_child_without_late_notification(bridge):
    meta, native = launch(bridge)
    meta.update(deliveryPolicy='join', parentInvocationId='parent-invocation')
    _save(bridge._path('parent', meta['id']).parent / 'launch.json', meta)
    bridge.owner.prompt_async = lambda *a, **k: pytest.fail('cancelled joined run must not resume a parent')
    bridge.refresh()
    bridge.cancel('parent', suppress_pending=True)
    assert list((native / 'control' / 'stop-requests').glob('*.json'))
    row = json.loads((native / 'status.json').read_text()); row['state'] = 'stopped'
    _save(native / 'status.json', row)
    bridge.refresh()
    assert bridge._read('parent', meta['id'])['delivery'] == 'suppressed'
