"""UTF-8 file and pipe contracts remain strict with Windows UTF-8 mode off."""
import ast
import json
from pathlib import Path
import subprocess
import sys

import pytest

import codex_protocol
import peer_context
import pi_children
from larkbot import onboarding
from memory_engine import project_memory
from engine_adapters.pi_subagent_view import PiSubagentViewMixin

ROOT = Path(__file__).resolve().parents[2]
TEXT = '合成协议内容：中文、emoji 🧪'


def test_codex_schema_documents_are_utf8_and_reject_invalid_bytes(tmp_path):
    for name in codex_protocol.BUNDLES:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(json.dumps({'description': TEXT}, ensure_ascii=False).encode('utf-8'))
    assert all(row['description'] == TEXT for row in codex_protocol.read_documents(tmp_path).values())
    first = tmp_path / next(iter(codex_protocol.BUNDLES))
    first.write_bytes(b'{"description":"\xff"}')
    with pytest.raises(UnicodeDecodeError):
        codex_protocol.read_documents(tmp_path)


def test_pi_child_evidence_roundtrips_utf8_without_replacement(tmp_path):
    path = tmp_path / 'delivery.json'
    row = {'result': TEXT, 'reason': '子代理无进展'}
    pi_children._save(path, row)
    assert json.loads(path.read_bytes().decode('utf-8')) == row
    assert pi_children._json(path) == row
    path.write_bytes(b'{"result":"\xff"}')
    with pytest.raises(UnicodeDecodeError):
        pi_children._json(path)


def test_onboarding_progress_is_utf8_across_save_read_and_listing(tmp_path, monkeypatch):
    monkeypatch.setattr(onboarding, 'ROOT', tmp_path)
    monkeypatch.setattr(onboarding, '_alive', lambda _: False)
    jid = 'a' * 32
    row = {'id': jid, 'name': TEXT, 'phase': 'complete'}
    onboarding._save(row)
    persisted = json.loads((tmp_path / (jid + '.json')).read_bytes().decode('utf-8'))
    assert persisted['name'] == TEXT
    assert onboarding.get(jid)['name'] == TEXT
    assert onboarding.listing()[0]['name'] == TEXT


@pytest.mark.parametrize('mode', ['0', '1'])
def test_peer_reader_owns_its_json_pipe_encoding_without_ambient_io_override(monkeypatch, mode):
    monkeypatch.setenv('PYTHONUTF8', mode)
    monkeypatch.delenv('PYTHONIOENCODING', raising=False)
    packet = peer_context.fetch({'sid': 'synthetic', 'engine': 'unsupported'}, TEXT)
    assert packet['ok'] is False and packet['code'] == 'history_unavailable'
    assert '不支持的会话引擎' in packet['error']


@pytest.mark.parametrize('valid', [True, False])
def test_memory_pi_pipe_is_real_utf8_and_invalid_bytes_fail_closed(tmp_path, monkeypatch, valid):
    script = tmp_path / 'synthetic_pi.py'
    script.write_text('''import json, sys
prompt = sys.stdin.buffer.read().decode('utf-8')
event = {'type': 'message_end', 'message': {'role': 'assistant',
         'content': [{'type': 'text', 'text': prompt}], 'usage': {}}}
wire = (json.dumps(event, ensure_ascii=False) + '\\n').encode('utf-8')
sys.stdout.buffer.write(wire if sys.argv[1] == 'valid' else b'\\xff\\n')
''', encoding='utf-8')
    run = subprocess.run
    def synthetic(argv, **kwargs):
        assert argv[0] == 'synthetic-pi'
        return run([sys.executable, str(script), 'valid' if valid else 'invalid'], **kwargs)
    monkeypatch.setattr(project_memory.subprocess, 'run', synthetic)
    answer, reason, _, _ = project_memory._run_pi('synthetic-pi', 'synthetic-model', TEXT, str(tmp_path))
    if valid:
        assert answer == TEXT + '\n' and not reason
    else:
        assert answer is None and reason == 'invalid-output'


def test_pi_child_sidebar_reads_native_utf8_sidecar(tmp_path):
    class View(PiSubagentViewMixin):
        dir = str(tmp_path)
        index = {'parent': {}}
        _live = {}
    directory = tmp_path / 'subagents/parent/child'
    directory.mkdir(parents=True)
    row = {'id': 'child', 'sessionID': 'parent', 'status': 'done',
           'started': 1, 'task': TEXT, 'last': '合成结果'}
    (directory / 'state.json').write_bytes(json.dumps(row, ensure_ascii=False).encode('utf-8'))
    projected = View().subagents('parent')
    assert len(projected) == 1 and projected[0]['task'] == TEXT
    assert projected[0]['last'] == '合成结果'


def test_ci_keeps_full_regression_in_both_utf8_modes():
    import yaml
    workflow = yaml.safe_load((ROOT / '.github/workflows/check.yml').read_text(encoding='utf-8'))
    windows = workflow['jobs']['windows-checks']
    assert windows['strategy']['matrix']['python-utf8'] == ['0', '1']
    step = next(s for s in windows['steps'] if s.get('name') == 'Windows offline full gate in both UTF-8 modes')
    assert 'bash scripts/dev/refactor-gate.sh full' in step['run']
    assert step['env']['PYTHONUTF8'] == '${{ matrix.python-utf8 }}'
    assert step['env']['PYTHONIOENCODING'] == 'utf-8'
    assert '$LASTEXITCODE' in step['run']


def test_json_engine_pipe_declarations_do_not_depend_on_locale():
    # These paths do not send model requests in this test. Existing recap suites
    # exercise their lifecycle; here retain the newly explicit wire contract.
    paths = ['engine_adapters/pi.py', 'engine_adapters/claude_transcript.py',
             'engine_adapters/recap.py', 'memory_engine/project_memory.py',
             'peer_context.py']
    inspected = 0
    for relative in paths:
        path = ROOT / 'desktron/lib' / relative
        for call in ast.walk(ast.parse(path.read_text(encoding='utf-8'))):
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                continue
            if call.func.attr not in ('run', 'Popen', 'run_with_success_marker'):
                continue
            keywords = {kw.arg: kw.value for kw in call.keywords}
            text = keywords.get('text')
            if not isinstance(text, ast.Constant) or text.value is not True:
                continue
            encoding = keywords.get('encoding')
            assert isinstance(encoding, ast.Constant) and encoding.value == 'utf-8', (relative, call.lineno)
            assert 'errors' not in keywords, 'Protocol decoding must not silently replace invalid bytes'
            inspected += 1
    assert inspected == 6
