// Real SDK lifecycle fixture: one parent prompt, actual background child, no
// paid/external requests. All result consumption must precede agent_settled.
import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {join} from 'node:path';
import {pathToFileURL} from 'node:url';
const sdk = await import(pathToFileURL(join(process.env.PI_SDK, 'dist/index.js')));
const services = await sdk.createAgentSessionServices({cwd: process.cwd(), settingsManager: sdk.SettingsManager.inMemory({}),
  resourceLoaderOptions: {noExtensions: true, noSkills: true, noContextFiles: true,
    additionalExtensionPaths: [process.env.PROVIDER, process.env.WRAPPER]}});
assert.deepEqual(services.resourceLoader.getExtensions().errors, []);
const model = services.modelRuntime.getModels().find(row => row.provider === 'desk-trae');
assert.ok(model);
const manager = process.env.CASE === 'file-backed'
  ? sdk.SessionManager.create(process.cwd(), join(process.cwd(), 'parent-session'))
  : sdk.SessionManager.inMemory(process.cwd());
const {session} = await sdk.createAgentSessionFromServices({services, model, thinkingLevel: 'off', sessionManager: manager});
const errors = [];
let settled = 0;
let finalAtSettle = '';
const timer = setInterval(() => {
  if (existsSync(join(process.cwd(), 'cancel-parent'))) session.abort().catch(() => {});
}, 100);
try {
  await session.bindExtensions({mode: 'print', onError: error => errors.push(String(error.error))});
  session.subscribe(event => {
    if (event.type === 'agent_settled') {settled++; finalAtSettle = session.getLastAssistantText();}
  });
  await session.prompt('Start required async work, read fixture.txt independently, then integrate child evidence before final completion.');
  assert.deepEqual(errors, []);
  assert.equal(settled, 1);
  if (process.env.CASE !== 'cancel') assert.match(finalAtSettle, /joined-final/);
  const joined = session.messages.filter(message => message.role === 'custom' && message.customType === 'desk-subagent-join');
  const waitResults = session.messages.filter(message => message.role === 'toolResult' && message.toolName === 'subagent' && message.details?.joined_run_ids);
  if (process.env.CASE === 'wait') {
    const original = waitResults[0];
    const tool = session.extensionRunner.getToolDefinition('subagent');
    const ctx = session.extensionRunner.createContext();
    const params = {action: 'wait', run_id: original.details.run_id};
    const replay = await tool.execute(original.toolCallId, params, undefined, () => {}, ctx);
    assert.deepEqual(replay.details, original.details);
    assert.deepEqual(replay.content, original.content);
    await assert.rejects(tool.execute('different-wait-consumer', params, undefined, () => {}, ctx), /不能重复投递|不重复投递/);
  }
  console.log(JSON.stringify({settled, final: finalAtSettle, joined: joined.length, waits: waitResults.length,
    joinedText: joined.map(message => message.content),
    outcomes: waitResults.map(message => message.details.outcome),
    branch: manager.getBranch().map(entry => ({type: entry.type, customType: entry.customType}))}));
} finally {clearInterval(timer); await session.dispose();}
process.exit(0);
