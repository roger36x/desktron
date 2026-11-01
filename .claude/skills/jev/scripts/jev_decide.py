#!/usr/bin/env python3
"""jev：经 OpenRouter 调用 TypeSafe 的 Jev 决策模型，对结构化问题直接给出带概率的判定。

题型：noul（是否题，返回“是”的概率）、choice（单选，返回选项及各项概率）、
score（有序打分，返回按概率加权的位置）。一次请求可问多道独立的题。

凭证只从环境变量 OPENROUTER_API_KEY 或 Pi 的本机 openrouter 登录读取，不进入命令行和输出；
网络代理沿用 Pi 设置里的 httpProxy。失败直接报错，不换用其他模型。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

MODEL = "typesafe/jev-1.13"
ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
TYPES = ("noul", "choice", "score")
MAX_QUESTIONS = 16
MAX_REQUEST_CHARS = 40_000   # Jev 上下文 3.2 万 token；超长直接报错，不截断
TIMEOUT = 60
SKILL_DIR = Path(__file__).resolve().parents[1]
REPO_ROOT = SKILL_DIR.parents[2]


class JevError(RuntimeError):
    pass


def _agent_dir():
    return Path(os.environ.get("PI_CODING_AGENT_DIR") or Path.home() / ".pi" / "agent")


def _api_key():
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if key:
        return key
    try:
        key = json.loads((_agent_dir() / "auth.json").read_text(encoding="utf-8"))["openrouter"]["key"]
    except (OSError, ValueError, KeyError, TypeError):
        key = ""
    if not isinstance(key, str) or not key.strip():
        raise JevError("未找到 OpenRouter 凭证：请在 Pi 中登录 openrouter，或设置环境变量 OPENROUTER_API_KEY")
    return key.strip()


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise JevError('OpenRouter 请求发生重定向，已拒绝；消息未转发')


def _opener():
    """与 Pi 一致：设置了 httpProxy 就走它，否则沿用环境与系统代理。"""
    try:
        settings = json.loads((_agent_dir() / "settings.json").read_text(encoding="utf-8"))
        proxy = str(settings.get("httpProxy") or "").strip()
    except (OSError, ValueError, AttributeError):
        proxy = ""
    if proxy:
        return urllib.request.build_opener(urllib.request.ProxyHandler({"http": proxy, "https": proxy}), NoRedirect())
    return urllib.request.build_opener(NoRedirect())


def _nonempty(value):
    return isinstance(value, str) and value.strip()


def validate(spec):
    """本地只查结构是否完整；细节以服务端校验为准，服务端拒绝时原样报错。"""
    if not isinstance(spec, dict):
        raise JevError("输入必须是 JSON 对象：{state, questions}")
    state = spec.get("state", {})
    if not isinstance(state, (dict, str)):
        raise JevError("state 必须是对象或文本")
    questions = spec.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise JevError("questions 必须是非空对象，键为题目名")
    if len(questions) > MAX_QUESTIONS:
        raise JevError(f"一次最多 {MAX_QUESTIONS} 道题")
    for name, q in questions.items():
        if not _nonempty(name) or not isinstance(q, dict):
            raise JevError("每道题需要非空名称和对象定义")
        kind, criteria = q.get("type"), q.get("criteria")
        if kind not in TYPES:
            raise JevError(f"{name}: type 必须是 {' / '.join(TYPES)}")
        if not _nonempty(q.get("instructions")):
            raise JevError(f"{name}: instructions 不能为空")
        if kind == "noul":
            if not isinstance(criteria, dict) or set(criteria) != {"true", "false"} \
                    or not all(_nonempty(v) for v in criteria.values()):
                raise JevError(f"{name}: noul 的 criteria 需要且只需要 true、false 两项说明")
        elif kind == "choice":
            if not isinstance(criteria, dict) or len(criteria) < 2 \
                    or not all(_nonempty(k) and _nonempty(v) for k, v in criteria.items()):
                raise JevError(f"{name}: choice 的 criteria 需要至少 2 个“选项: 说明”")
        elif not isinstance(criteria, list) or len(criteria) < 2 or not all(_nonempty(v) for v in criteria):
            raise JevError(f"{name}: score 的 criteria 需要至少 2 个按顺序排列的等级说明")
    body = {"model": MODEL, "state": state, "questions": questions}
    if len(json.dumps(body, ensure_ascii=False)) > MAX_REQUEST_CHARS:
        raise JevError(f"请求超过 {MAX_REQUEST_CHARS} 字符；请先摘出判定所需的部分，不要整段传入")
    return body


def _number(value, upper=1):
    if type(value) not in (int, float) or not 0 <= value <= upper:
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _checked_answers(body, answers):
    if not isinstance(answers, dict) or set(answers) != set(body['questions']):
        raise JevError('Jev 返回的答案与提问不对应')
    checked = {}
    for name, question in body['questions'].items():
        answer = answers[name]
        kind = question['type']
        if not isinstance(answer, dict) or answer.get('type') != kind:
            raise JevError('Jev 返回的答案题型无效')
        if kind == 'noul':
            if not _number(answer.get('noul')):
                raise JevError('Jev 返回的是否题概率无效')
            checked[name] = {'type': kind, 'noul': answer['noul']}
            continue
        labels = (set(question['criteria']) if kind == 'choice' else
                  {str(i) for i in range(len(question['criteria']))})
        probabilities = answer.get('probabilities')
        if (not isinstance(probabilities, dict) or set(probabilities) != labels
                or not all(_number(v) for v in probabilities.values())
                or abs(sum(probabilities.values()) - 1) > 0.05
                or not _number(answer.get('confidence'))):
            raise JevError('Jev 返回的概率分布或置信度无效')
        result = {'type': kind, 'confidence': answer['confidence'],
                  'probabilities': probabilities}
        if kind == 'choice':
            if not isinstance(answer.get('choice'), str) or answer['choice'] not in labels:
                raise JevError('Jev 返回的选择不在选项中')
            result['choice'] = answer['choice']
        else:
            legend = {str(i): value for i, value in enumerate(question['criteria'])}
            if not _number(answer.get('score'), len(legend) - 1) or answer.get('legend') != legend:
                raise JevError('Jev 返回的有序打分或等级说明无效')
            result.update(score=answer['score'], legend=legend)
        checked[name] = result
    return checked


def _redact(value, key):
    if isinstance(value, str):
        return value.replace(key, '[redacted]') if key else value
    if isinstance(value, dict):
        return {_redact(k, key): _redact(v, key) for k, v in value.items()}
    return value


def decide(body, key=None, opener=None):
    key = key or _api_key()
    opener = opener or _opener()
    req = urllib.request.Request(ENDPOINT, data=json.dumps(body).encode("utf-8"), headers={
        "Authorization": "Bearer " + key, "Content-Type": "application/json",
        "X-Title": "Desktron jev"})
    try:
        with opener.open(req, timeout=TIMEOUT) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise JevError(f"OpenRouter 返回 HTTP {exc.code}；错误正文未输出，请核对请求或服务状态") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise JevError('无法连接 OpenRouter；网络错误详情未输出') from None
    except ValueError:
        raise JevError("OpenRouter 返回的不是 JSON") from None
    if not isinstance(result, dict):
        raise JevError('OpenRouter 返回的 JSON 不是对象')
    if result.get('error'):
        raise JevError('OpenRouter 返回错误；错误正文未输出')
    answers = _checked_answers(body, result.get('answers'))
    usage = result.get('usage') or {}
    if not isinstance(usage, dict):
        raise JevError('Jev 返回的用量无效')
    safe_usage = {}
    for field in ('input_tokens', 'output_tokens', 'cost'):
        if field in usage:
            value = usage[field]
            if not _number(value, float('inf')) or (field.endswith('_tokens') and type(value) is not int):
                raise JevError('Jev 返回的用量无效')
            safe_usage[field] = value
    model = result.get('model') or MODEL
    provider = result.get('provider') or ''
    if not isinstance(model, str) or not isinstance(provider, str):
        raise JevError('Jev 返回的模型或供应商无效')
    return _redact({'model': model, 'provider': provider, 'answers': answers,
                    'usage': safe_usage}, key)


def _record_usage(report):
    try:
        lib = str(REPO_ROOT / "desktron" / "lib")
        if lib not in sys.path:
            sys.path.insert(0, lib)
        from usage_stats import record_sidecar
        data_home = os.environ.get("DESKTRON_DATA_HOME") or str(Path.home() / ".local/share/desktron/data")
        usage = report.get("usage") or {}
        recorded = record_sidecar(data_home, "jev", engine="openrouter", model=MODEL,
                                  usage={"input": usage.get("input_tokens"), "output": usage.get("output_tokens")},
                                  outcome="ok")
        if not recorded:
            print('Jev 用量记录失败；判定结果保留。', file=sys.stderr)
    except Exception:
        print('Jev 用量记录失败；判定结果保留，未记录敏感错误详情。', file=sys.stderr)


def main(argv=None):
    ap = argparse.ArgumentParser(description="经 OpenRouter 调用 Jev 做结构化判定")
    ap.add_argument("--json", metavar="FILE", help="从文件或 - (标准输入) 读取 {state, questions}")
    ap.add_argument("--question", help="单选题简写：题目")
    ap.add_argument("--option", action="append", default=[], help="单选题简写：选项，可重复")
    ap.add_argument("--state-file", help="单选题简写：状态材料文件（UTF-8 文本）")
    ap.add_argument("--dry-run", action="store_true", help="只校验并打印请求体，不发请求")
    a = ap.parse_args(argv)
    try:
        if a.json:
            raw = sys.stdin.read() if a.json == "-" else Path(a.json).read_text(encoding="utf-8")
            spec = json.loads(raw)
        else:
            if not _nonempty(a.question):
                raise JevError("请用 --json 传入 {state, questions}，或用 --question 加多个 --option")
            state = Path(a.state_file).read_text(encoding="utf-8") if a.state_file else {}
            options = [o.strip() for o in a.option]
            if len(set(options)) != len(options):
                raise JevError("选项不能重复")
            spec = {"state": state, "questions": {"answer": {
                "type": "choice", "instructions": a.question, "criteria": {o: o for o in options}}}}
        body = validate(spec)
        if a.dry_run:
            print(json.dumps(body, ensure_ascii=False, indent=1))
            return 0
        report = decide(body)
    except (JevError, ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 1
    _record_usage(report)
    print(json.dumps(report, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
