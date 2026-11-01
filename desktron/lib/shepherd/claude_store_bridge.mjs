#!/usr/bin/env node
/** Claude Agent SDK SessionStore backed by a Shepherd-owned SQLite database. */
import { readFile } from 'node:fs/promises';
import { resolve, isAbsolute } from 'node:path';
import { pathToFileURL } from 'node:url';
import { ShepherdClaudeStore } from './claude_sqlite_store.mjs';

const [sdkPath, command, dbPath, sessionId, cwd] = process.argv.slice(2);
function fail(message) { throw new Error(message); }
if (!sdkPath || !command || !dbPath || !/^[0-9a-f-]{36}$/i.test(sessionId || '') || !isAbsolute(cwd || ''))
  fail('invalid bridge arguments');
const sdkRoot = pathToFileURL(resolve(sdkPath) + '/');
const pkg = JSON.parse(await readFile(new URL('package.json', sdkRoot), 'utf8'));
const sdk = await import(new URL('sdk.mjs', sdkRoot).href);
if (typeof sdk.getSessionMessages !== 'function') fail('Claude SDK lacks SessionStore readback');

const store = new ShepherdClaudeStore(dbPath, sessionId);
if (command === 'verify') {
  const messages = await sdk.getSessionMessages(sessionId, { dir: cwd, sessionStore: store, includeSystemMessages: true });
  const entries = await store.load({ projectKey: 'verified-by-sdk', sessionId });
  console.log(JSON.stringify({ storeContract:'desk-claude-store-v1', sdkVersion: pkg.version, entries: entries.length,
    messages: messages.length, revision: store.revision(),
    lastUuid: entries.findLast(entry => entry.uuid)?.uuid || '',
    shepherdIds: entries.map(entry => entry.shepherd?.round_id || entry.shepherd?.checkpoint_id).filter(Boolean) }));
} else fail('unsupported bridge command');
