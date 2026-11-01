/** Verify actual native user-message content; never treat a host flag as proof. */
import {createHash} from 'node:crypto';
export function verifyFallback(messages, receipt) {
  if (!receipt) return undefined;
  if (typeof receipt.checkpoint_id !== 'string' || !receipt.checkpoint_id.startsWith('degraded_')
      || !/^[a-f0-9]{64}$/.test(receipt.text_sha256)) throw new Error('Invalid fallback receipt');
  let matches=0;
  for (const entry of messages) {
    const message=entry.message || entry;
    if (message.role !== 'user') continue;
    const content=message.content;
    const text=typeof content === 'string' ? content : (Array.isArray(content) ? content : [])
      .filter(block=>['text','input_text'].includes(block.type)).map(block=>block.text || '').join('');
    if (createHash('sha256').update(text).digest('hex')===receipt.text_sha256) matches++;
  }
  if (matches!==1) throw new Error('Native fallback text missing, duplicated or changed');
  return {checkpoint_id:receipt.checkpoint_id,text_sha256:receipt.text_sha256};
}
