"""Capability evolution tests based on an independent native schema export."""
import copy
import json
from pathlib import Path
import subprocess
from unittest import mock

import pytest

import codex_protocol as probe
import codex_protocol_contract as contract
from shepherd.codex_connector import CodexFamilyConnector
from shepherd.store import Conflict


@pytest.fixture
def documents():
    return json.loads((Path(__file__).parent / 'fixtures/codex-protocol-0.155.json').read_text())


def params(documents, name):
    doc = documents['ClientRequest.json']
    item = next(x for x in doc['oneOf'] if name in x['properties']['method']['enum'])
    return contract.branches(item['properties']['params'], doc)[0]


def test_native_surface_satisfies_contract(documents):
    assert contract.assess(documents) == []


def test_unused_reasoning_summary_is_not_an_execution_requirement(documents):
    params(documents, 'turn/start')['properties'].pop('summary', None)
    assert contract.assess(documents) == []


def test_additive_api_and_optional_fields_are_compatible(documents):
    documents['ClientRequest.json']['oneOf'].append({'properties': {
        'method': {'enum': ['future/operation']}, 'params': {'type': 'object'}}})
    for name in contract.REQUESTS:
        params(documents, name)['properties']['futureOptionalField'] = {'type': 'boolean'}
    documents['ServerNotification.json']['definitions']['Turn']['properties']['diagnostic'] = {'type': 'string'}
    assert contract.assess(documents) == []


@pytest.mark.parametrize('method', list(contract.REQUESTS))
def test_removing_required_method_is_incompatible(documents, method):
    documents['ClientRequest.json']['oneOf'] = [x for x in documents['ClientRequest.json']['oneOf']
        if method not in x['properties']['method']['enum']]
    assert method in contract.assess(documents)


@pytest.mark.parametrize('mutation', ['required_input', 'input_type', 'missing_field',
                                      'terminal_optional', 'terminal_type', 'terminal_enum',
                                      'approval_cancel', 'context_kind'])
def test_consumed_breaking_changes_are_rejected(documents, mutation):
    start = params(documents, 'turn/start')
    terminal = documents['ServerNotification.json']['definitions']['Turn']
    if mutation == 'required_input':
        start['properties']['newRequirement'] = {'type': 'string'}
        start['required'].append('newRequirement')
    if mutation == 'input_type': start['properties']['threadId'] = {'type': 'integer'}
    if mutation == 'missing_field': del start['properties']['additionalContext']
    if mutation == 'terminal_optional': terminal['required'].remove('status')
    if mutation == 'terminal_type': terminal['properties']['id'] = {'type': ['string', 'null']}
    if mutation == 'terminal_enum':
        documents['ServerNotification.json']['definitions']['TurnStatus']['enum'].append('mystery')
    if mutation == 'approval_cancel':
        doc = documents['CommandExecutionRequestApprovalResponse.json']
        decision = doc['definitions']['CommandExecutionApprovalDecision']
        decision['oneOf'] = [x for x in decision['oneOf'] if x.get('enum') != ['cancel']]
    if mutation == 'context_kind':
        documents['ClientRequest.json']['definitions']['AdditionalContextKind']['enum'] = ['untrusted']
    assert contract.assess(documents)


def test_unknown_consumed_constraint_is_unproved_not_accepted(documents):
    params(documents, 'turn/start')['properties']['threadId']['pattern'] = '^new:'
    with pytest.raises(contract.Unproved): contract.assess(documents)


def image_branch(documents):
    user_input = documents['ClientRequest.json']['definitions']['UserInput']['oneOf']
    return next(b for b in user_input if b['properties']['type'].get('enum') == ['image'])


def combine_image_source(branch):
    """Codex 0.156 shape: own properties plus anyOf(url | fileId)."""
    branch['properties'].pop('url', None)
    branch['required'] = ['type']
    branch['anyOf'] = [
        {'properties': {'url': {'type': 'string'}}, 'required': ['url'], 'type': 'object'},
        {'properties': {'fileId': {'type': 'string'}}, 'required': ['fileId'], 'type': 'object'}]


def test_unsent_combined_input_branch_does_not_block_admission(documents):
    # 2026-09-24: Codex 0.156.1 auto-upgraded; every Codex turn failed admission
    # with "协议声明无法按当前契约确认" although Desk never sends `image` inputs.
    combine_image_source(image_branch(documents))
    assert contract.assess(documents) == []


def test_combined_branch_that_may_accept_desk_input_stays_unproved(documents):
    branch = image_branch(documents)
    combine_image_source(branch)
    branch['properties']['type']['enum'] = ['image', 'text']  # oneOf exclusivity no longer provable
    with pytest.raises(contract.Unproved): contract.assess(documents)


def test_combined_schema_in_consumed_server_output_stays_unproved(documents):
    doc = documents['ClientRequest.json']
    branch = image_branch(documents)
    combine_image_source(branch)
    with pytest.raises(contract.Unproved):
        contract.matches(contract.obj(('type',), type=contract.TEXT), branch, doc, outgoing=False)


def test_opaque_item_schema_cannot_hide_new_history_requirements(documents):
    params(documents, 'thread/inject_items')['properties']['items']['items'] = {'type': 'object'}
    assert 'thread/inject_items' in contract.assess(documents)


def fake_export(documents):
    def export(_binary, destination, home):
        assert home.is_dir()
        for name, doc in documents.items():
            path = destination / name; path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(doc))
        return 0
    return export


def test_admission_does_not_consult_release_numbers_and_rechecks_same_launcher(tmp_path, documents):
    binary = tmp_path / 'codex'; binary.touch()
    with mock.patch.object(probe, 'export_schema', side_effect=fake_export(documents)) as export, \
         mock.patch('subprocess.run', side_effect=AssertionError('no version probe')):
        assert CodexFamilyConnector.availability('codex', str(binary))['available']
        params(documents, 'turn/start')['required'].append('newRequirement')
        status = CodexFamilyConnector.availability('codex', str(binary))
        assert status['reason'] == 'codex_protocol_incompatible'
        assert status['missing_capabilities'] == ['turn/start']
        assert export.call_count == 2  # Stable npm launcher is not a protocol cache key.


@pytest.mark.parametrize('mode,reason', [('missing', 'codex_missing'),
    ('timeout', 'codex_probe_timeout'), ('oserror', 'codex_probe_failed'),
    ('exit', 'codex_protocol_unverified'), ('malformed', 'codex_protocol_unverified')])
def test_probe_failures_have_actionable_distinct_states(tmp_path, mode, reason):
    binary = tmp_path / 'codex'
    if mode != 'missing': binary.touch()
    error = {'timeout': subprocess.TimeoutExpired('schema', 15), 'oserror': PermissionError()}.get(mode)
    with mock.patch.object(probe, 'export_schema', side_effect=error,
                           return_value=1 if mode == 'exit' else 0):
        status = probe.availability(str(binary))
    assert status['reason'] == reason
    assert not status['available']
    assert '消息未派发' in status['message']


def test_unverifiable_cli_never_starts_writer_or_dispatches(tmp_path):
    from shepherd.service import Service
    from shepherd.unified_runners import UnifiedRunner
    binary = tmp_path / 'codex'; binary.touch()
    service = Service(tmp_path / 'state', storage='unified')
    service.create('conversation', 'codex', tmp_path)
    runner = UnifiedRunner(service.connector(service.active('conversation')['binding']))
    dispatch = mock.Mock()
    with mock.patch.object(probe, 'availability', return_value={
            'available': False, 'reason': 'codex_protocol_incompatible', 'message': '缺少 turn/start'}), \
         mock.patch('codex_appserver.run_turn') as run, \
         mock.patch.object(CodexFamilyConnector, '_seed_private_config') as seed, \
         mock.patch.object(CodexFamilyConnector, '_connection') as connection:
        with pytest.raises(Conflict, match='缺少 turn/start'):
            runner._codex('hello', [], None, dispatch, mock.Mock())
        with pytest.raises(Conflict, match='缺少 turn/start'):
            CodexFamilyConnector.create_native('codex', str(tmp_path), store_root=tmp_path / 'legacy',
                                              binding_id='fixture', binary=str(binary))
    dispatch.assert_not_called(); run.assert_not_called(); seed.assert_not_called(); connection.assert_not_called()


def test_export_is_isolated_and_never_uses_model_or_user_config(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    process = mock.Mock(); process.wait.return_value = 0; process.poll.return_value = 0
    with mock.patch.object(probe.subprocess, 'Popen', return_value=process) as popen, \
         mock.patch.object(probe, 'protect'), \
         mock.patch.dict('os.environ', {'OPENAI_API_KEY': 'synthetic-secret', 'CODEX_HOME': '/user-home'}):
        assert probe.export_schema('/cli/codex', tmp_path / 'out', home) == 0
    argv = popen.call_args.args[0]; kwargs = popen.call_args.kwargs
    assert 'generate-json-schema' in argv and 'turn/start' not in argv
    assert kwargs['env']['CODEX_HOME'] == str(home)
    assert 'OPENAI_API_KEY' not in kwargs['env']
    assert kwargs['cwd'] == home


def test_export_timeout_stops_owned_child(tmp_path):
    process = mock.Mock()
    process.wait.side_effect = [subprocess.TimeoutExpired('schema', 15), 0]
    process.poll.return_value = None
    with mock.patch.object(probe.subprocess, 'Popen', return_value=process), \
         mock.patch.object(probe, 'protect') as protect:
        with pytest.raises(subprocess.TimeoutExpired):
            probe.export_schema('/cli/codex', tmp_path / 'out', tmp_path)
    protect.assert_called_once_with(process)
    process.kill.assert_called_once()
