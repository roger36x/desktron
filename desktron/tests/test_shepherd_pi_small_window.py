"""Small-window Pi routes must keep output room after automatic compaction.

The pinned Pi runtime's fixed compaction defaults (reserve 16384, kept tail 20000) contradict
each other on a 32K window: compaction cannot shrink the context below its own
threshold, Pi clamps max_tokens to the 1-token floor and the turn ends as a
one-token length stop. Shepherd's launcher fits the budget to the live window.
"""
import http.server
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest

ROOT = Path(__file__).resolve().parents[2]
LIB = ROOT / 'desktron' / 'lib'
sys.path.insert(0, str(LIB))
from shepherd.pi_connector import pi_sdk_root
from shepherd.pi_terminal import (CONTEXT_EXHAUSTED_MESSAGE, OUTPUT_LIMIT_MESSAGE,
                                  output_limit_failure)

BUDGET = LIB / 'shepherd' / 'pi_compaction_budget.mjs'


def _fit(window, reserve=16384, keep=20000):
    script = (f"import {{fitCompactionBudget}} from {json.dumps(BUDGET.as_uri())};"
              f"console.log(JSON.stringify(fitCompactionBudget({json.dumps(window)},{reserve},{keep})))")
    out = subprocess.run(['node', '--input-type=module', '-e', script],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)


class Budget(unittest.TestCase):
    def test_large_windows_keep_native_defaults(self):
        for window in (131072, 262144, 1000000, 60000 + 16384):
            self.assertEqual(_fit(window), {'reserveTokens': 16384, 'keepRecentTokens': 20000,
                                            'scaled': False}, window)

    def test_small_windows_fit_tail_under_threshold(self):
        for window in (8192, 16384, 32768, 65536, 60000 + 16383):
            row = _fit(window)
            threshold = window - row['reserveTokens']
            self.assertTrue(row['scaled'], window)
            self.assertLess(row['reserveTokens'], window)
            self.assertLessEqual(row['keepRecentTokens'] * 3, threshold)
            # Pi's request clamp keeps 4096 tokens of safety on top of the input.
            self.assertGreaterEqual(window - threshold - 4096, 2048, window)
        self.assertEqual(_fit(8192), {'reserveTokens': 6144, 'keepRecentTokens': 682,
                                      'scaled': True})
        self.assertEqual(_fit(32768), {'reserveTokens': 16384, 'keepRecentTokens': 5461,
                                       'scaled': True})

    def test_unknown_or_unsupported_window_is_untouched(self):
        for window in (0, None, -1, 1.5, 8191):
            self.assertEqual(_fit(window), {'reserveTokens': 16384, 'keepRecentTokens': 20000,
                                            'scaled': False})

    def test_never_raises_configured_values(self):
        self.assertEqual(_fit(32768, reserve=4096, keep=2000),
                         {'reserveTokens': 4096, 'keepRecentTokens': 2000, 'scaled': False})


class Terminal(unittest.TestCase):
    def test_tiny_length_stop_is_context_exhaustion(self):
        message = {'stopReason': 'length', 'usage': {'input': 24157, 'output': 1}}
        self.assertEqual(output_limit_failure(message),
                         ('CONTEXT_EXHAUSTED', CONTEXT_EXHAUSTED_MESSAGE))

    def test_long_answer_is_output_limit(self):
        for message in ({'stopReason': 'length', 'usage': {'output': 16384}},
                        {'stopReason': 'length'}, {'stopReason': 'length', 'usage': {'output': True}},
                        None):
            self.assertEqual(output_limit_failure(message), ('OUTPUT_LIMIT', OUTPUT_LIMIT_MESSAGE))


class _Model:
    """Anthropic-messages fixture; a 1-token budget yields a one-token length stop."""

    def __init__(self):
        self.requests = []
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                text = json.dumps(body, ensure_ascii=False)
                tokens = len(text) // 4
                outer.requests.append({'max_tokens': body.get('max_tokens'), 'input': tokens,
                                       'summary': 'context checkpoint' in text})
                out = 1 if (body.get('max_tokens') or 0) <= 1 else 20
                events = [
                    {'type': 'message_start', 'message': {
                        'id': 'm', 'type': 'message', 'role': 'assistant', 'content': [],
                        'model': 'fixture', 'stop_reason': None, 'stop_sequence': None,
                        'usage': {'input_tokens': tokens, 'output_tokens': 0}}},
                    {'type': 'content_block_start', 'index': 0, 'content_block': {'type': 'text', 'text': ''}},
                    {'type': 'content_block_delta', 'index': 0,
                     'delta': {'type': 'text_delta', 'text': 'The' if out == 1 else '## Goal\nfixture'}},
                    {'type': 'content_block_stop', 'index': 0},
                    {'type': 'message_delta', 'usage': {'output_tokens': out},
                     'delta': {'stop_reason': 'max_tokens' if out == 1 else 'end_turn', 'stop_sequence': None}},
                    {'type': 'message_stop'}]
                raw = ''.join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@unittest.skipUnless(pi_sdk_root(), 'pinned Pi SDK required')
class LauncherKeepsOutputRoom(unittest.TestCase):
    def test_32k_route_never_sends_one_token_requests(self):
        model = _Model()
        self.addCleanup(model.close)
        tmp = Path(tempfile.mkdtemp())
        agent = tmp / 'agent'; agent.mkdir()
        home = tmp / 'home'; home.mkdir()
        (agent / 'models.json').write_text(json.dumps({'providers': {'fixture': {
            'baseUrl': model.url, 'api': 'anthropic-messages', ('api' + 'Key'): 'fixture',
            'models': [{'id': 'small', 'name': 'Small', 'reasoning': False, 'input': ['text'],
                        'contextWindow': 32768, 'maxTokens': 16384,
                        'cost': {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0}}]}}}))
        # Model overrides outrank ordinary compaction settings in Pi 1.0.2. The
        # launcher must fit and rewrite the effective model-specific values.
        (agent / 'settings.json').write_text(json.dumps({'compaction': {
            'reserveTokens': 12000, 'keepRecentTokens': 6000,
            'modelOverrides': {'fixture/small': {
                'reserveTokens': 16384, 'keepRecentTokens': 20000}}}}))
        boot = tmp / 'boot.json'
        boot.write_text(json.dumps({'id': 'small-window', 'messages': [], 'entries': None, 'compact': False}))
        proc = subprocess.Popen(
            ['node', str(LIB / 'shepherd' / 'pi_memory_rpc.mjs'), pi_sdk_root(), str(boot),
             '--mode', 'rpc', '--offline', '--no-extensions', '--no-tools', '--model', 'fixture/small'],
            cwd=tmp, env={**os.environ, 'PI_CODING_AGENT_DIR': str(agent), 'HOME': str(home)},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        rows = []
        threading.Thread(target=lambda: [rows.append(json.loads(line)) for line in proc.stdout
                                         if line.startswith('{')], daemon=True).start()

        def wait(start, kind):
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                if any(r.get('type') == kind for r in rows[start:]):
                    return
                time.sleep(0.05)
            self.fail(proc.stderr.read() if proc.poll() is not None else f'timeout waiting {kind}')

        pad = ' '.join(f'w{i}' for i in range(5200))
        try:
            wait(0, 'protocol_ready')
            for turn in range(6):
                start = len(rows)
                proc.stdin.write(json.dumps({'type': 'desk_compaction', 'threshold': 0,
                                             'minHeadroom': 65536}) + '\n')
                proc.stdin.write(json.dumps({'type': 'prompt', 'message': f'turn {turn}: {pad}'}) + '\n')
                proc.stdin.flush()
                wait(start, 'agent_end')
                time.sleep(0.3)
        finally:
            proc.stdin.close()
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
            proc.stdout.close(); proc.stderr.close()
        frame = [r for r in rows if r.get('type') == 'desk_compaction'][-1]
        self.assertEqual((frame['status'], frame['reserveTokens'], frame['keepRecentTokens']),
                         ('fitted', 16384, 5461))
        self.assertTrue(any(r.get('type') == 'compaction_end' for r in rows))
        main = [r for r in model.requests if not r['summary']]
        self.assertGreaterEqual(len(main), 6)
        self.assertGreaterEqual(min(r['max_tokens'] for r in main), 2048, main)


if __name__ == '__main__':
    unittest.main()
