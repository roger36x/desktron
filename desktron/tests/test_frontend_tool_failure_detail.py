"""失败摘要不重复铺开执行日志；独立错误仍可查阅。"""
import subprocess
import unittest
from test_frontend_recall import fn
from frontend_i18n_js import NODE_PRELUDE


class ToolFailureDetail(unittest.TestCase):
    def test_failure_summary_and_expanded_output(self):
        names = ('hsRet', 'hsStepDetail', 'hsStepDetailBase', 'hsToolFailureView',
                 'hsToolFailureExit', 'hsToolSig')
        source = '\n'.join(fn('app-chat-detail.js', name) for name in names)
        source += '\n' + fn('app-chat-tools.js', 'hsEsc')
        source += r'''
const assert = require('node:assert/strict');
function hsClip(s) { return hsEsc(s); }
function hsReturnDetail() { return ''; }
function hsToolDescription() { return ''; }
function hsCommandView() { return ''; }
const log = 'Warning: truncated output\nTotal output lines: 11\n' + 'pytest failure trace\n'.repeat(80);
const part = {tool:'bash',state:{status:'error',input:{},output:log,
  failure:{message:log.slice(0,500)}}};
// 旧投影：摘要是输出前缀，折叠时不展示，展开仅有一份日志。
assert.deepEqual(hsToolFailureView(part.state), {summary:'',detail:''});
assert.equal(hsStepDetail(part).split('Warning: truncated output').length, 2);
part.state.output = JSON.stringify({exit_code:1,output:log});
assert.deepEqual(hsToolFailureView(part.state), {summary:'',detail:''});
// 新投影：失败状态与结构化退出码保留，通用提示不另占一行。
part.state.failure = {message:'工具执行失败（退出码 1）',exit_code:1};
assert.equal(hsToolFailureExit(part.state), '（退出码 1）');
assert.equal(hsToolFailureView(part.state).summary, '');
const sig = hsToolSig(part);
part.state.failure.exit_code = 2;
assert.notEqual(hsToolSig(part), sig);
// 独立短报错仍显示；多行报错进入详情，即使没有普通输出也可展开。
part.state.failure.message = '权限不足';
assert.equal(hsToolFailureView(part.state).summary, '权限不足');
part.state.failure.message = '校验失败\n<script>alert(1)</script>';
part.state.output = '';
assert.equal(hsToolFailureView(part.state).summary, '');
assert.ok(hsStepDetail(part).includes('校验失败'));
assert.ok(hsStepDetail(part).includes('&lt;script&gt;'));
assert.ok(!hsStepDetail(part).includes('<script>'));
part.state.status = 'done';
assert.deepEqual(hsToolFailureView(part.state), {summary:'',detail:''});
assert.equal(hsToolFailureExit(part.state), '');
'''
        result = subprocess.run(['node', '-e', NODE_PRELUDE + source], capture_output=True, text=True, timeout=10, encoding='utf-8')
        self.assertEqual(result.returncode, 0, result.stderr)
