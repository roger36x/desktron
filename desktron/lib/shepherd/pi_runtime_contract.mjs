/** Only the SessionManager operations consumed by Desk, not a Pi release gate. */
export const PI_STORE_CONTRACT = 'desk-pi-store-v1';
export class CapabilityMismatch extends Error {}
export function requireMethods(value, names, label) {
  for (const name of names) {
    if (typeof value?.[name] !== 'function') throw new CapabilityMismatch(`${label}.${name}`);
  }
}
export function verifyPiSession(session) {
  requireMethods(session, ['subscribe', 'compact', 'abort', 'abortCompaction', 'bindExtensions', 'dispose'], 'AgentSession');
  if (!Array.isArray(session.messages)) throw new CapabilityMismatch('AgentSession.messages');
}
export function verifyPiStore(SessionManager, cwd) {
  requireMethods(SessionManager, ['inMemory', 'create', 'open'], 'SessionManager');
  const id = 'b3ff4ce0-9295-47b4-836d-6b98f8a8a01f';
  const sm = SessionManager.inMemory(cwd, {id}, []);
  requireMethods(sm, ['getEntries', 'getHeader', 'getSessionId', 'getCwd',
    'getSessionFile', 'getLeafId', 'buildSessionContext', 'appendMessage',
    'appendCustomEntry', 'appendCustomMessageEntry', 'appendCompaction', '_persist'], 'SessionManager');
  if (sm.getSessionId() !== id) throw new CapabilityMismatch('SessionManager.session-id');
  if (sm.getCwd() !== cwd) throw new CapabilityMismatch('SessionManager.cwd');
  if (sm.getHeader()?.version !== 3) throw new CapabilityMismatch('SessionManager.format-v3');
  const archived = [];
  sm._persist = entry => archived.push(entry);
  const message = {role:'user', content:'desk protocol probe', timestamp:0};
  sm.appendMessage(message);
  sm.appendCustomEntry('desk-probe', {value:1});
  const boundary = sm.appendCustomMessageEntry('desk-boundary', 'retained boundary', false);
  sm.appendCompaction('probe summary', boundary, 1, {}, true);
  const entries = sm.getEntries();
  const restored = SessionManager.inMemory(cwd, {id}, [sm.getHeader(), ...entries]);
  if (archived.length !== 4 || entries[0]?.message?.content !== message.content
      || entries[2]?.type !== 'custom_message' || entries[2]?.display !== false
      || entries[3]?.firstKeptEntryId !== boundary
      || JSON.stringify(restored.getEntries()) !== JSON.stringify(entries)
      || !sm.buildSessionContext().messages.some(x => x.role === 'compactionSummary'))
    throw new CapabilityMismatch('SessionManager.archive-and-restore');
  return sm;
}
