"""jev 技能：请求形状、结构校验、答案对应、凭证不外泄。"""
import copy
import importlib.util
import io
import json
from pathlib import Path
import urllib.error

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / ".claude/skills/jev/scripts/jev_decide.py"
SPEC = importlib.util.spec_from_file_location("jev_skill", SCRIPT)
JEV = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(JEV)

QUESTIONS = {
    "is_bug": {"type": "noul", "instructions": "缺陷？", "criteria": {"true": "是", "false": "否"}},
    "team": {"type": "choice", "instructions": "归属？", "criteria": {"a": "甲", "b": "乙"}},
    "urgency": {"type": "score", "instructions": "紧急？", "criteria": ["低", "中", "高"]},
}


class Opener:
    def __init__(self, payload=None, error=None):
        self.payload, self.error, self.seen = payload, error, {}

    def open(self, req, timeout):
        self.seen.update(body=json.loads(req.data), auth=req.headers.get("Authorization"), url=req.full_url)
        if self.error:
            raise self.error
        return io.BytesIO(json.dumps(self.payload).encode())


def answers(names):
    fixtures = {'is_bug': {'type': 'noul', 'noul': 0.9},
                'team': {'type': 'choice', 'choice': 'a', 'confidence': 0.8,
                         'probabilities': {'a': 0.9, 'b': 0.1}},
                'urgency': {'type': 'score', 'score': 1.5, 'confidence': 0.5,
                            'probabilities': {'0': 0, '1': 0.5, '2': 0.5},
                            'legend': {'0': '低', '1': '中', '2': '高'}},
                'answer': {'type': 'choice', 'choice': '甲', 'confidence': 0.8,
                           'probabilities': {'甲': 0.9, '乙': 0.1}}}
    return {"model": "typesafe/jev-1.13-20260917", "provider": "TypeSafe",
            "answers": {n: copy.deepcopy(fixtures[n]) for n in names},
            "usage": {"input_tokens": 300, "output_tokens": 30, "cost": 0.0000126}}


def test_request_targets_pinned_jev_and_hides_key(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-secret-value")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    opener = Opener(answers(["answer"]))
    monkeypatch.setattr(JEV, "_opener", lambda: opener)
    monkeypatch.setattr(JEV, "_record_usage", lambda *_a: None)
    assert JEV.main(["--question", "选哪个", "--option", "甲", "--option", "乙"]) == 0
    assert opener.seen["url"] == "https://openrouter.ai/api/alpha/decisions"
    assert opener.seen["body"]["model"] == "typesafe/jev-1.13"
    assert opener.seen["body"]["questions"]["answer"] == {
        "type": "choice", "instructions": "选哪个", "criteria": {"甲": "甲", "乙": "乙"}}
    assert opener.seen["auth"] == "Bearer sk-test-secret-value"
    out = capsys.readouterr().out
    assert "sk-test-secret-value" not in out and json.loads(out)["usage"]["cost"] == 0.0000126


def test_all_three_question_types_pass_validation():
    body = JEV.validate({"state": "文本状态", "questions": QUESTIONS})
    assert body["state"] == "文本状态" and set(body["questions"]) == set(QUESTIONS)


@pytest.mark.parametrize("bad, message", [
    ({"questions": {}}, "非空对象"),
    ({"questions": {"q": {**QUESTIONS["is_bug"], "type": "rank"}}}, "type 必须是"),
    ({"questions": {"q": {**QUESTIONS["is_bug"], "criteria": {"yes": "a", "no": "b"}}}}, "true、false"),
    ({"questions": {"q": {**QUESTIONS["team"], "criteria": {"a": "甲"}}}}, "至少 2 个"),
    ({"questions": {"q": {**QUESTIONS["urgency"], "criteria": ["低"]}}}, "至少 2 个"),
    ({"questions": {"q": {**QUESTIONS["team"], "instructions": " "}}}, "instructions"),
    ({"state": 3, "questions": QUESTIONS}, "state"),
])
def test_incomplete_structure_fails_before_network(bad, message):
    with pytest.raises(JEV.JevError, match=message):
        JEV.validate(bad)


def test_oversized_request_is_rejected_not_truncated():
    with pytest.raises(JEV.JevError, match="超过"):
        JEV.validate({"state": "x" * JEV.MAX_REQUEST_CHARS, "questions": QUESTIONS})


def test_server_rejection_and_mismatched_answers_fail_loudly():
    err = urllib.error.HTTPError(JEV.ENDPOINT, 400, "bad", {}, io.BytesIO(b'{"error":"invalid_union"}'))
    body = JEV.validate({"questions": QUESTIONS})
    with pytest.raises(JEV.JevError, match="HTTP 400"):
        JEV.decide(body, key="k", opener=Opener(error=err))
    with pytest.raises(JEV.JevError, match="不对应"):
        JEV.decide(body, key="k", opener=Opener(answers(["is_bug"])))


def test_missing_key_and_dry_run(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    assert JEV.main(["--question", "q", "--option", "甲", "--option", "乙"]) == 1
    assert "未找到 OpenRouter 凭证" in capsys.readouterr().out
    monkeypatch.setattr(JEV, "_opener", lambda: pytest.fail("dry run must not open network"))
    assert JEV.main(["--dry-run", "--question", "q", "--option", "甲", "--option", "乙"]) == 0
    assert json.loads(capsys.readouterr().out)["model"] == "typesafe/jev-1.13"


def test_skill_doc_and_tracking():
    doc = (ROOT / ".claude/skills/jev/SKILL.md").read_text(encoding="utf-8")
    assert doc.startswith("---\nname: jev\n")
    for text in ("不得传入个人信息", "没有针对你的数据校准", "不会自动换用其他模型", "退出码 1"):
        assert text in doc
    assert "!.claude/skills/jev/" in (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert not (ROOT / ".claude/skills/system-one").exists()
    assert not (ROOT / "desktron/lib/system_one").exists()


@pytest.mark.parametrize('bad', [None, [], 0, 'unexpected'])
def test_non_object_response_is_structured_failure(bad, monkeypatch, capsys):
    monkeypatch.setattr(JEV, '_api_key', lambda: 'fixture-key')
    monkeypatch.setattr(JEV, '_opener', lambda: Opener(bad))
    assert JEV.main(['--question', 'q', '--option', '甲', '--option', '乙']) == 1
    assert 'error' in json.loads(capsys.readouterr().out)


@pytest.mark.parametrize('field,value', [
    ('is_bug', None), ('is_bug', {'type': 'noul', 'noul': float('nan')}),
    ('is_bug', {'type': 'noul', 'noul': True}),
    ('is_bug', {'type': 'noul', 'noul': 10**400}),
    ('team', {'type': 'noul', 'noul': 0.5}),
    ('team', {'type': 'choice', 'choice': 'outside', 'confidence': 0.8,
              'probabilities': {'a': 0.9, 'b': 0.1}}),
    ('team', {'type': 'choice', 'choice': 'a', 'confidence': 0.8,
              'probabilities': {'a': 0.9}}),
    ('urgency', {'type': 'score', 'score': 9, 'confidence': 0.8,
                 'probabilities': {'0': 0, '1': 0.5, '2': 0.5},
                 'legend': {'0': '低', '1': '中', '2': '高'}}),
])
def test_malformed_typed_answers_fail(field, value):
    payload = answers(QUESTIONS)
    payload['answers'][field] = value
    with pytest.raises(JEV.JevError):
        JEV.decide(JEV.validate({'questions': QUESTIONS}), key='fixture-key', opener=Opener(payload))


def test_valid_three_type_response_and_allowlisted_output():
    payload = answers(QUESTIONS); payload['private_echo'] = 'DO_NOT_DISPLAY'
    report = JEV.decide(JEV.validate({'questions': QUESTIONS}), key='fixture-key', opener=Opener(payload))
    assert report['answers'] == payload['answers']
    assert 'private_echo' not in report


@pytest.mark.parametrize('mode', ['http', 'json', 'network', 'success'])
def test_remote_echo_does_not_leak_credential(mode, monkeypatch, capsys):
    key = 'synthetic-echo-secret'
    if mode == 'http':
        opener = Opener(error=urllib.error.HTTPError(JEV.ENDPOINT, 401, 'error', {}, io.BytesIO(key.encode())))
    elif mode == 'json':
        opener = Opener({'error': key})
    elif mode == 'network':
        opener = Opener(error=urllib.error.URLError(key))
    else:
        payload = answers(['answer']); payload['provider'] = key
        payload['answers']['answer']['debug'] = key
        opener = Opener(payload)
    monkeypatch.setattr(JEV, '_api_key', lambda: key)
    monkeypatch.setattr(JEV, '_opener', lambda: opener)
    monkeypatch.setattr(JEV, '_record_usage', lambda _: None)
    code = JEV.main(['--question', 'q', '--option', '甲', '--option', '乙'])
    captured = capsys.readouterr()
    assert code == (0 if mode == 'success' else 1)
    assert key not in captured.out + captured.err


@pytest.mark.parametrize('location', ['https://other.invalid/path', 'http://openrouter.ai/path',
                                     'https://openrouter.ai/other'])
@pytest.mark.parametrize('code', [301, 302, 303, 307, 308])
def test_redirects_rejected_before_authorization_can_be_forwarded(location, code, monkeypatch, tmp_path):
    monkeypatch.setenv('PI_CODING_AGENT_DIR', str(tmp_path))
    opener = JEV._opener()
    handler = next(h for h in opener.handlers if isinstance(h, JEV.NoRedirect))
    request = JEV.urllib.request.Request(JEV.ENDPOINT, data=b'{}', headers={'Authorization': 'Bearer fixture'})
    with pytest.raises(JEV.JevError, match='重定向'):
        handler.redirect_request(request, None, code, 'redirect', {}, location)


def test_usage_false_return_is_visible_warning(monkeypatch, capsys):
    from types import SimpleNamespace
    import sys
    monkeypatch.setitem(sys.modules, 'usage_stats', SimpleNamespace(record_sidecar=lambda *a, **k: False))
    JEV._record_usage({'usage': {'input_tokens': 12, 'output_tokens': 3}})
    assert '用量记录失败' in capsys.readouterr().err


def test_large_usage_integer_is_rejected_without_traceback():
    payload = answers(QUESTIONS); payload['usage']['input_tokens'] = 10**400
    with pytest.raises(JEV.JevError, match='用量无效'):
        JEV.decide(JEV.validate({'questions': QUESTIONS}), key='fixture', opener=Opener(payload))


def test_usage_root_and_cli_recording_without_existing_pythonpath(tmp_path):
    import subprocess, sys, sqlite3, os
    assert JEV.REPO_ROOT == ROOT
    environment = {**os.environ, 'DESKTRON_DATA_HOME': str(tmp_path), 'PYTHONPATH': ''}
    code = ('import runpy; module=runpy.run_path(' + repr(str(SCRIPT)) + '); '
            'module["_record_usage"]({"usage":{"input_tokens":12,"output_tokens":3}})')
    result = subprocess.run([sys.executable, '-I', '-c', code], env=environment,
                            cwd=tmp_path, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0 and not result.stderr
    database = next(tmp_path.rglob('*.sqlite3'))
    with sqlite3.connect(database) as connection:
        row = connection.execute('select kind from sidecar').fetchone()
    assert row == ('jev',)
