// Materialize only an admitted working context in disposable storage. No model runtime.
import {readFileSync, writeFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';
import {verifyPiStore} from './pi_runtime_contract.mjs';
import {verifyFallback} from './fallback_receipt.mjs';

const [root, input, output] = process.argv.slice(2);
const {SessionManager} = await import(pathToFileURL(join(root, 'dist/core/session-manager.js')).href);
const state = JSON.parse(readFileSync(input, 'utf8'));
verifyPiStore(SessionManager, process.cwd());
const manager = SessionManager.inMemory(process.cwd(), {id: state.id}, state.entries || undefined);
for (const message of state.messages) {
  if (message.type === 'custom') manager.appendCustomEntry(message.customType, message.data);
  else manager.appendMessage(message);
}
const receipt = state.fallbackReceipt
  ? verifyFallback(manager.buildSessionContext().messages, state.fallbackReceipt) : null;
writeFileSync(output, [manager.getHeader(), ...manager.getEntries()]
  .map(entry => JSON.stringify(entry)).join('\n') + '\n', {mode: 0o600, flag: 'wx'});
// Verify the on-disk input which the CLI will fork, not just the in-memory plan.
const restored = SessionManager.open(output, undefined, process.cwd());
if (JSON.stringify(restored.buildSessionContext().messages) !==
    JSON.stringify(manager.buildSessionContext().messages)) throw new Error('Pi peer context readback mismatch');
console.log(JSON.stringify({type: 'prepared', fallbackReceipt: receipt}));
