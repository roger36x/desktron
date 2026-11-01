"""SDK 恢复视图只去掉不再指向末尾的 last-prompt 接续提示。

Claude Code 2.1.291 恢复会话时优先采用最后一条 last-prompt 的 leafUuid。Desk 在原生
记录之后追加可移植回合，却不改写这条元数据；过期提示会让新回合从旧节点分叉，归档随后
以 anchor parent mismatch 拒收(2026-10-06 实见)。存储本身不能改动，只有交给 SDK 的
load() 视图过滤过期提示；Desk 自己的 load() 调用方仍读到原样数据。
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'desktron/lib/shepherd/claude_sdk_options.mjs'

SCRIPT = """
import {claudeResumeStore, withoutStaleLeafHints} from %s;
const rows = [
  {type:'user', uuid:'u1', parentUuid:null},
  {type:'last-prompt', lastPrompt:'old', leafUuid:'u1'},
  {type:'assistant', uuid:'a1', parentUuid:'u1'},
  {type:'mode', mode:'default'},
];
const current = [...rows, {type:'last-prompt', lastPrompt:'new', leafUuid:'a1'}];
const raw = {calls:0, async load() { this.calls++; return rows; }, size() { return rows.length; }};
const view = claudeResumeStore(raw);
console.log(JSON.stringify({
  stale: withoutStaleLeafHints(rows).map(r => r.type),
  current: withoutStaleLeafHints(current).map(r => r.leafUuid || r.uuid || r.type),
  viewed: (await view.load({})).map(r => r.type),
  rawUntouched: (await raw.load()).length,
  delegated: view.size(),
}));
"""


@unittest.skipUnless(shutil.which('node'), 'node required')
class ClaudeResumeView(unittest.TestCase):
    def run_script(self):
        code = SCRIPT % json.dumps(MODULE.as_uri())
        done = subprocess.run(['node', '--input-type=module', '-e', code],
                              capture_output=True, text=True, timeout=30, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_stale_leaf_hint_is_hidden_from_sdk_only(self):
        report = self.run_script()
        self.assertEqual(report['stale'], ['user', 'assistant', 'mode'])
        self.assertEqual(report['viewed'], ['user', 'assistant', 'mode'])
        self.assertEqual(report['rawUntouched'], 4)
        self.assertEqual(report['delegated'], 4)

    def test_hint_naming_the_tail_is_kept(self):
        self.assertEqual(self.run_script()['current'], ['u1', 'a1', 'mode', 'a1'])

    def test_every_sdk_resume_uses_the_view(self):
        for name in ('claude_resident_runner.mjs', 'claude_runner.mjs', 'claude_recap.mjs'):
            source = (ROOT / 'desktron/lib/shepherd' / name).read_text(encoding='utf-8')
            self.assertIn('sessionStore:claudeResumeStore(store)', source.replace(' ', ''), name)


if __name__ == '__main__':
    unittest.main()
