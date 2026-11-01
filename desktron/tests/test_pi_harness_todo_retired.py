"""Retiring the private todo tool must not alter remaining model-gated reminders."""
from pathlib import Path
import shutil
import subprocess

import pytest


def test_todo_retired_without_changing_remaining_hooks():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required for Pi extension behavior tests")
    source = Path(__file__).parents[1] / "lib/engine_adapters/pi_harness_loop.ts"
    script = r'''
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { stripTypeScriptTypes } from 'node:module';
const source = readFileSync(process.argv[1], 'utf8');
assert(!source.includes('promptGuidelines'));
assert(!source.includes('loop-todo'));
assert(!source.includes('typebox'));
const url = 'data:text/javascript;base64,' + Buffer.from(stripTypeScriptTypes(source)).toString('base64');
const { default: install } = await import(url);
delete process.env.DESKTRON_PI_HARNESS_MODELS;

function setup(provider, id) {
  const hooks = {}, messages = [], registered = [];
  const oldEntries = [{ type: 'custom', customType: 'loop-todo',
    data: { todos: [{ id: 1, text: 'Old unrelated task', done: false }], nextId: 2 } }];
  const snapshot = JSON.stringify(oldEntries);
  install({
    on(event, callback) { hooks[event] = callback; },
    registerTool(tool) { registered.push(tool); },
    appendEntry() { assert.fail('no private todo persistence'); },
    sendMessage(message, opts) { messages.push({ message, opts }); },
  });
  const ctx = { model: { provider, id }, sessionManager: {
    getBranch() { assert.fail('retired todo history must not be restored'); },
  } };
  hooks.session_start({}, ctx);
  hooks.model_select({}, ctx);
  assert.deepEqual(registered, []);
  assert.equal(JSON.stringify(oldEntries), snapshot);
  return { hooks, ctx, messages };
}

for (const [provider, id] of [
  ['example-gw', 'gpt-5.6-sol:gpt-5.6-sol__max'],
  ['ollama-remote', 'qwen3.8:27b'], ['other', 'model'],
]) {
  const { hooks, ctx, messages } = setup(provider, id);
  for (let turnIndex = 0; turnIndex < 3; turnIndex++) hooks.turn_start({ turnIndex }, ctx);
  const context = { messages: [] };
  assert.equal(hooks.context(context, ctx), undefined);
  assert.deepEqual(context.messages, []);
  assert.equal(hooks.tool_result({ toolName: 'edit', content: [] }, ctx), undefined);
  hooks.agent_end({}, ctx);
  assert.deepEqual(messages, []);
}

{
  const { hooks, ctx, messages } = setup('deepseek', 'deepseek-v4-pro');
  for (let turnIndex = 0; turnIndex < 3; turnIndex++) hooks.turn_start({ turnIndex }, ctx);
  const context = { messages: [] };
  hooks.context(context, ctx);
  assert.equal(context.messages.length, 1, 'existing quiet-turn reminder preserved');
  hooks.tool_call({}, ctx);
  assert.equal(hooks.context({ messages: [] }, ctx).messages.length, 0);
  const result = hooks.tool_result({ toolName: 'write', content: [] }, ctx);
  assert(result.content.some(block => block.text.includes('验证')));
  hooks.agent_end({}, ctx);
  hooks.agent_end({}, ctx);
  assert.equal(messages.length, 1, 'existing bounded verification follow-up preserved');
  assert.equal(messages[0].message.customType, 'harness-verify-nudge');
  assert.equal(messages[0].opts.deliverAs, 'followUp');
}

{
  const { hooks, ctx, messages } = setup('deepseek', 'deepseek-v4-pro');
  hooks.turn_start({ turnIndex: 0 }, ctx);
  hooks.tool_result({ toolName: 'edit', content: [] }, ctx);
  hooks.turn_start({ turnIndex: 1 }, ctx);
  hooks.tool_result({ toolName: 'bash', input: { command: 'pytest tests' } }, ctx);
  hooks.agent_end({}, ctx);
  assert.deepEqual(messages, [], 'already verified edits do not trigger follow-up');
}
console.log('todo retired; history untouched; remaining model gates and reminders preserved');
'''
    result = subprocess.run([node, "--input-type=module", "-e", script, str(source)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr
