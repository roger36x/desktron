import test from 'node:test';
import assert from 'node:assert/strict';
import {mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, symlinkSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {dirname,join} from 'node:path';
import {ChildJoin, JOIN_MESSAGE} from '../lib/engine_adapters/pi_child_join.mjs';

function fixture(t, options = {}) {
  const store = mkdtempSync(join(tmpdir(), 'desk-child-join-'));
  t.after(() => rmSync(store, {recursive: true, force: true}));
  const deps = new ChildJoin({store, sid: 'parent', pollMs: 5, ...options});
  const add = (id = 'child', extra = {}) => {
    const dir = join(store, 'parent', '_calls', id);
    mkdirSync(dir, {recursive: true});
    const meta = {id, sessionID: 'parent', started: Date.now(), deliveryPolicy: 'join', parentInvocationId: deps.parentId, ...extra};
    writeFileSync(join(dir, 'launch.json'), JSON.stringify(meta));
    deps.track(meta);
    return meta;
  };
  const finish = (id = 'child', extra = {}) => writeFileSync(deps.path(id, 'delivery.json'), JSON.stringify({
    id, sessionID: 'parent', status: 'done', outcome: 'completed', result: 'fixture-evidence', ...extra}));
  const ledger = (id = 'child') => JSON.parse(readFileSync(deps.path(id, 'join.json'), 'utf8'));
  return {store, deps, add, finish, ledger};
}

test('wait blocks until terminal receipt, with no relaunch and with progress', async t => {
  const {deps, add, finish} = fixture(t); add();
  const updates = [];
  let resolved = false;
  const waiting = deps.wait('child', {onUpdate: value => updates.push(value)}).then(row => {resolved = true; return row;});
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(resolved, false);
  assert.equal(updates[0].details.state, 'waiting_dependency');
  finish();
  assert.equal((await waiting).result, 'fixture-evidence');
});

test('early-ready results preserve every sibling outcome', t => {
  const {deps, add, finish, ledger} = fixture(t);
  add('ok'); add('bad'); finish('ok'); finish('bad', {outcome: 'failed', result: 'partial output'});
  const message = deps.message(deps.pending().map(id => deps.result(id)));
  assert.equal(message.customType, JOIN_MESSAGE);
  assert.deepEqual(message.details.joined_run_ids, ['ok', 'bad']);
  assert.match(message.content, /partial output/);
  assert.match(message.content, /"outcome":"failed"/);
  assert.equal(ledger('bad').state, 'offered');
  assert.deepEqual(deps.pending(), []);
});

test('offered is only observed in memory, and consumed only after exact durable readback', t => {
  const {deps, add, finish, ledger} = fixture(t); add(); finish();
  const message = deps.message([deps.result('child')]);
  deps.acknowledge([{id: 'wrong', type: 'custom_message', ...message,
    details: {...message.details, join_batch_id: 'different'}}]);
  assert.equal(ledger().state, 'offered');
  const entry = {id: 'proof', type: 'custom_message', ...message};
  deps.acknowledge([entry]);
  assert.equal(ledger().state, 'observed');
  assert.equal(ledger().archive_confirmed, false);
  const archive = deps.path('child', 'fixture-session.jsonl');
  writeFileSync(archive, JSON.stringify({...entry, content: 'different'}) + '\n');
  deps.acknowledge([entry], archive);
  assert.equal(ledger().state, 'observed');
  writeFileSync(archive, JSON.stringify(entry) + '\n');
  deps.acknowledge([entry], archive);
  assert.equal(ledger().state, 'consumed');
  assert.equal(ledger().entry_id, 'proof');
  assert.throws(() => deps.message([deps.result('child')]), /不能重复投递/);
});

test('tool result acknowledgement requires tool identity and name', t => {
  const {deps, add, finish, ledger} = fixture(t); add(); finish();
  deps.offer([deps.result('child')], {channel: 'tool', id: 'wait-call'});
  const entry = {id: 'proof', type: 'message', message: {role: 'toolResult', toolName: 'subagent',
    toolCallId: 'other', details: {joined_run_ids: ['child']}}};
  deps.acknowledge([entry]); assert.equal(ledger().state, 'offered');
  entry.message.toolCallId = 'wait-call';
  deps.acknowledge([entry]); assert.equal(ledger().state, 'observed');
  const archive = deps.path('child', 'fixture-session.jsonl');
  writeFileSync(archive, JSON.stringify(entry) + '\n');
  deps.acknowledge([entry], archive); assert.equal(ledger().state, 'consumed');
});

test('restart leaves uncertain offers held, with no automatic injection into a new invocation', t => {
  const {store, deps, add, finish} = fixture(t); const meta = add(); finish();
  deps.message([deps.result('child')]);
  const restored = new ChildJoin({store, sid: 'parent'});
  assert.deepEqual(restored.pending(), []);
  assert.notEqual(restored.parentId, meta.parentInvocationId);
  assert.equal(restored.track(meta).state, 'offered');
  assert.deepEqual(restored.pending(), []);
});

test('new invocation does not adopt previous pending dependencies automatically', t => {
  const {deps, add} = fixture(t); const meta = add();
  deps.begin();
  assert.notEqual(deps.parentId, meta.parentInvocationId);
  assert.deepEqual(deps.pending(), []);
  assert.equal(deps.track(meta).state, 'waiting'); // explicit recovery only
});

test('abort ends waiting promptly without consuming or claiming that the child stopped', async t => {
  const {deps, add, ledger} = fixture(t); add();
  const controller = new AbortController();
  const waiting = deps.wait('child', {signal: controller.signal});
  controller.abort();
  await assert.rejects(waiting, {name: 'AbortError'});
  assert.equal(ledger().state, 'waiting');
});

test('durable stop and cancellation fences beat even a completed result', async t => {
  for (const name of ['_cancel.json', '_stop.json']) {
    const {store, deps, add, finish, ledger} = fixture(t); const meta = add(); finish();
    writeFileSync(join(store, 'parent', name), JSON.stringify({through_ms: meta.started}));
    await assert.rejects(deps.wait('child'), {code: 'ABORTED'});
    assert.equal(ledger().state, 'waiting');
  }
});

test('missing identity/receipt has a finite blocked outcome, not fabricated process exit', async t => {
  const {deps, add} = fixture(t, {totalMs: 10, graceMs: 1, now: () => 1000}); add('child', {started: 1});
  const result = await deps.wait('child');
  assert.equal(result.outcome, 'blocked');
  assert.equal(result.error_code, 'DEPENDENCY_TIMEOUT');
  assert.equal(result.stop_confirmed, false);
});

test('notification-owned or foreign-session runs cannot be adopted', t => {
  const {deps, add} = fixture(t);
  assert.throws(() => add('notify', {deliveryPolicy: 'notify'}), /不能抢占通知交付/);
  assert.throws(() => add('foreign', {sessionID: 'other'}), /不能抢占通知交付/);
  assert.throws(() => deps.path('../escape', 'launch.json'), /无效 run_id/);
});

test('a forged or symlinked receipt is rejected, not consumed', t => {
  const {deps, add, finish} = fixture(t); add(); finish('child', {sessionID: 'other'});
  assert.throws(() => deps.result('child'), /归属不匹配/);
  rmSync(deps.path('child', 'delivery.json'));
  // Junctions are name-surrogate links too and need no Windows symlink privilege.
  symlinkSync(process.platform==='win32' ? dirname(deps.path('child', 'launch.json')) : deps.path('child', 'launch.json'),
    deps.path('child', 'delivery.json'), process.platform==='win32' ? 'junction' : 'file');
  assert.throws(() => deps.result('child'), /符号链接/);
});

test('result text is bounded but sibling statuses and full-result locations survive', t => {
  const {deps, add, finish} = fixture(t);
  for (let i = 0; i < 24; i++) {add('run' + i); finish('run' + i, {outcome: i % 2 ? 'failed' : 'completed', result: 'x'.repeat(100000)});}
  const message = deps.message(deps.pending().map(id => deps.result(id)));
  assert.ok(message.content.length < 65000);
  assert.equal(message.details.joined_run_ids.length, 24);
  assert.match(message.content, /正文截断/);
  assert.match(message.content, /run23/);
  assert.throws(() => add('overflow'), /最多接受24批/);
});
