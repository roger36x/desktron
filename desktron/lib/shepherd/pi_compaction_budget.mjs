/** Window-scaled automatic compaction budget for Shepherd-owned Pi hosts.
 *
 * The pinned Pi runtime ships fixed compaction defaults (reserve 16384, kept tail 20000)
 * sized for large windows. On a 32K route those defaults contradict each other:
 * compaction fires above 16384 tokens but must keep at least 20000 tokens of
 * recent history, so every compaction leaves the context above its own
 * threshold. Pi then clamps the request's max_tokens to
 * `contextWindow - estimated input - 4096`, which reaches the 1-token floor, and
 * the turn ends as a one-token length stop. Overflow recovery cannot help: the
 * next compaction finds nothing older than the kept tail and returns nothing.
 *
 * The budget below only ever lowers Pi's configured values, never raises them:
 * - for windows of at least 8K, the default reserve leaves at least 2048 output
 *   tokens beyond Pi's 4096-token safety margin;
 * - kept tail <= one third of the compaction threshold, so the system prompt,
 *   summary and kept tail fit back under the threshold after compaction.
 * Smaller windows are left unchanged because Pi's safety margin leaves no useful
 * automatic-compaction budget. Large windows (>= ~133K with defaults) are unchanged.
 */

export function fitCompactionBudget(contextWindow, reserveTokens, keepRecentTokens) {
  const window = Number.isSafeInteger(contextWindow) && contextWindow > 0 ? contextWindow : 0;
  if (!window || window < 8192) return { reserveTokens, keepRecentTokens, scaled: false };
  const outputReserve = Math.min(window - 1, 4096 + 2048);
  const reserve = Math.min(reserveTokens, Math.max(Math.floor(window / 2), outputReserve));
  const keep = Math.max(1, Math.min(keepRecentTokens, Math.floor((window - reserve) / 3)));
  return { reserveTokens: reserve, keepRecentTokens: keep,
    scaled: reserve !== reserveTokens || keep !== keepRecentTokens };
}
