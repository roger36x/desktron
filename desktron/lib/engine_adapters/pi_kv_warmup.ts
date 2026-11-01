/** Optional idle-time warmup. No prompt mutation, transcript cache or request replay. */
import { mkdirSync, readdirSync, rmdirSync } from "node:fs";
import { createHash } from "node:crypto";
import { homedir } from "node:os";
import { join } from "node:path";
import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";

const ON = new Set(["1", "true", "on", "yes"]);
const MAX_PREFIX_BYTES = 64 * 1024;

function log(outcome: string) {
  // Only fixed outcomes/status codes: never credentials, prompt, URL or exception text.
  try { process.stderr.write("[pi-kv-warmup] " + outcome + "\n"); } catch { /* optional */ }
}

function bounded(value: string | undefined, fallback: number, min: number, max: number) {
  const number = Number(value);
  return value && Number.isFinite(number) ? Math.min(max, Math.max(min, number)) : fallback;
}

/** One attempt per endpoint/model per fixed window, shared by local Pi processes.
 * Empty directories contain no context. A crash consumes only the current window;
 * no stale PID stealing, background writer or unbounded retained window history.
 */
export function claimWindow(root: string, identity: string, now: number, windowMs: number): boolean {
  const target = join(root, createHash("sha256").update(identity).digest("hex"));
  const bucket = Math.floor(now / windowMs);
  try {
    mkdirSync(target, { recursive: true, mode: 0o700 });
    mkdirSync(join(target, String(bucket)), { mode: 0o700 });
  } catch { return false; }
  try {
    for (const name of readdirSync(target)) {
      if (/^\d+$/.test(name) && Number(name) < bucket - 1) {
        try { rmdirSync(join(target, name)); } catch { /* only remove empty old markers */ }
      }
    }
  } catch { /* cleanup never affects requests */ }
  return true;
}

function abortable<T>(promise: Promise<T>, signal: AbortSignal): Promise<T> {
  return new Promise((resolve, reject) => {
    const abort = () => reject(new Error("warmup cancelled"));
    signal.addEventListener("abort", abort, { once: true });
    promise.then(value => { signal.removeEventListener("abort", abort); resolve(value); },
      error => { signal.removeEventListener("abort", abort); reject(error); });
    if (signal.aborted) abort();
  });
}

export default function (pi: ExtensionAPI) {
  pi.registerFlag("desk-kv-warm-provider", { description: "Explicit warmup provider", type: "string" });
  pi.registerFlag("desk-kv-warm-model", { description: "Exact warmup model ID", type: "string" });
  const provider = String(pi.getFlag("desk-kv-warm-provider") || process.env.PI_KV_WARM_PROVIDER || "").trim();
  const modelId = String(pi.getFlag("desk-kv-warm-model") || process.env.PI_KV_WARM_MODEL || "").trim();
  const selected = (ctx: ExtensionContext) =>
    ON.has((process.env.PI_KV_WARMUP || "").trim().toLowerCase()) &&
    Boolean(provider && modelId && ctx.model?.provider === provider && ctx.model.id === modelId &&
      ctx.model.api === "openai-completions");
  const idle = (ctx: ExtensionContext) => ctx.isIdle() && !ctx.hasPendingMessages();
  let active: AbortController | null = null;

  async function warm(ctx: ExtensionContext, controller: AbortController, prefix: string) {
    const model = ctx.model!;
    try {
      const auth = await abortable(ctx.modelRegistry.getApiKeyAndHeaders(model), controller.signal);
      if (controller.signal.aborted || !selected(ctx) || !idle(ctx)) return;
      if (!auth.ok) { log("skip_auth_unavailable"); return; }
      // Follow only this model's resolved endpoint/auth; no unrelated global URL override.
      const url = new URL(auth.baseUrl || model.baseUrl);
      if (!["http:", "https:"].includes(url.protocol) || url.username || url.password || url.search || url.hash) {
        log("skip_unsupported_endpoint"); return;
      }
      url.pathname = url.pathname.replace(/\/+$/, "") + "/chat/completions";
      const windowMs = bounded(process.env.PI_KV_WARM_WINDOW_SEC, 1800, 60, 86400) * 1000;
      const root = join(homedir(), ".pi", "agent", "cache", "desk-kv-warmup-attempts-v2");
      if (!claimWindow(root, JSON.stringify([provider, modelId, url.href]), Date.now(), windowMs)) {
        log("skip_shared_cooldown"); return;
      }
      const headers = new Headers(auth.headers);
      if (auth.apiKey && !headers.has("authorization")) headers.set("authorization", "Bearer " + auth.apiKey);
      headers.set("content-type", "application/json");
      const response = await abortable(fetch(url, {
        method: "POST", headers, redirect: "error", signal: controller.signal,
        body: JSON.stringify({ model: modelId, messages: [
          { role: "system", content: prefix }, { role: "user", content: "hi" },
        ], max_tokens: 1, stream: false }),
      }), controller.signal);
      void response.body?.cancel().catch(() => undefined);
      log("http_" + response.status);
    } catch { log(controller.signal.aborted ? "cancelled_or_timeout" : "failed"); }
  }

  pi.on("session_start", (_event, ctx) => {
    try {
      if (active || !selected(ctx) || !idle(ctx)) return;
      // Never load another session's cached payload or truncate/substitute a prefix.
      const prefix = ctx.getSystemPrompt();
      if (typeof prefix !== "string" || !prefix.trim() || Buffer.byteLength(prefix, "utf8") > MAX_PREFIX_BYTES) {
        log("skip_prefix_empty_or_large"); return;
      }
      const controller = new AbortController();
      active = controller;
      const timeout = setTimeout(() => controller.abort(),
        bounded(process.env.PI_KV_WARM_TIMEOUT_MS, 5000, 100, 10000));
      void warm(ctx, controller, prefix).finally(() => {
        clearTimeout(timeout);
        if (active === controller) active = null;
      });
    } catch { log("skip_context_unavailable"); }
  });

  const cancel = () => { active?.abort(); };
  // Actual inference always takes priority. No warmup in before_provider_request.
  pi.on("before_agent_start", cancel);
  pi.on("before_provider_request", cancel);
  pi.on("model_select", cancel);
  pi.on("session_shutdown", cancel);
}
