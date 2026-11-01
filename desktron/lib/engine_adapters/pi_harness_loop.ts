/**
 * loop —— 动作优先循环 + verify 硬闸（仅对目标模型启用）。
 *
 * 机制:
 *  1. 提醒注入:连续多轮无工具调用时,在 context 里注入"动手查证"提醒。
 *  2. 验证提醒:edit/write 后追加"改完验证"提醒。
 *  3. verify 硬闸:agent 准备停止时,若改过但没验证,塞回一条要求先验证(防死循环上限)。
 *
 * 模型门控:只在 harnessModels 匹配的模型上生效(默认 deepseek/*)。
 * 配置优先级:环境变量 DESKTRON_PI_HARNESS_MODELS > 内置兜底 deepseek/*。
 */

import type { ExtensionAPI, ExtensionContext } from "@earendil-works/pi-coding-agent";
const REMINDER_AFTER_TURNS = 3; // 连续 N 轮无工具调用后提醒
const MAX_VERIFY_NUDGES = 1; // verify 硬闸最多塞回次数(防死循环)
// 识别"验证动作"的 bash 命令:跑测试/构建/语法检查
const VERIFY_RE = /python3?\s+(test|-m\s+unittest|-m\s+py_compile)|pytest|unittest|py_compile|check_frontend|npm\s+test|bun\s+test|yarn\s+test|go\s+test|cargo\s+test|make\s+test/i;

// ---- 模型门控 ----

function globToRe(pattern: string): RegExp {
  const escaped = pattern.replace(/[.+^${}()|[\]\\]/g, "\\$&").replace(/\*/g, ".*");
  return new RegExp("^" + escaped + "$");
}

function harnessModels(): string[] {
  const env = process.env.DESKTRON_PI_HARNESS_MODELS;
  if (env && env.trim()) {
    return env.split(",").map((s) => s.trim()).filter(Boolean);
  }
  return ["deepseek/*"];
}

function harnessEnabled(ctx: ExtensionContext): boolean {
  const m = (ctx as any).model;
  if (!m?.provider || !m?.id) return false;
  const id = `${m.provider}/${m.id}`;
  return harnessModels().some((p) => globToRe(p).test(id));
}

export default function (pi: ExtensionAPI) {
  // 每回合计数:连续多轮无工具调用 → 提醒
  let quietTurns = 0;

  // verify 硬闸状态:改过但没验证时,在 agent 停止前塞回提醒
  let currentTurn = 0;
  let lastEditTurn = -1;
  let lastVerifyTurn = -1;
  let verifyNudged = 0;

  const reset = () => {
    quietTurns = 0;
    currentTurn = 0;
    lastEditTurn = -1;
    lastVerifyTurn = -1;
    verifyNudged = 0;
  };

  pi.on("session_start", () => reset());

  // 模型切换时重置,避免残留旧模型的动作记录
  pi.on("model_select", () => reset());

  pi.on("turn_start", (event) => {
    currentTurn = event.turnIndex;
    quietTurns += 1;
  });

  pi.on("tool_call", () => {
    quietTurns = 0;
  });

  pi.on("context", (event, ctx) => {
    if (!harnessEnabled(ctx)) return;
    if (quietTurns >= REMINDER_AFTER_TURNS) {
      event.messages.push({
        role: "user",
        content: [
          {
            type: "text",
            text: "[系统提醒] 你已连续多轮没有调用工具。优先动手查证(运行命令、读文件),用工具拿到事实再继续,不要停留在思考。",
          },
        ],
        timestamp: Date.now(),
      });
      quietTurns = 0;
    }
    return { messages: event.messages };
  });

  // edit/write 后追加验证提醒,并记录改动/验证动作供硬闸使用
  pi.on("tool_result", (event, ctx) => {
    if (!harnessEnabled(ctx)) return;
    if (event.toolName === "edit" || event.toolName === "write") {
      lastEditTurn = currentTurn;
      const blocks = Array.isArray(event.content)
        ? event.content
        : [{ type: "text" as const, text: String(event.content ?? "") }];
      return {
        content: [
          ...blocks,
          { type: "text" as const, text: "\n[提醒] 改动已完成。运行验证(测试/命令,或 verify 技能)确认通过后再继续。" },
        ],
      };
    }
    if (event.toolName === "bash") {
      const cmd = typeof (event.input as any)?.command === "string" ? (event.input as any).command : "";
      if (VERIFY_RE.test(cmd)) lastVerifyTurn = currentTurn;
    }
  });

  // verify 硬闸:agent 准备停止时,若改过但没验证,塞回一条要求先验证。
  // 必须挂在 agent_end,让 Pi 的 post-run 队列把 follow-up 跑完后再发最终 agent_settled。
  // 若挂在 settled 上,扩展会在 Desk 已判定回合结束后偷偷启动新一轮；下一条用户消息
  // 撞上这条隐藏轮次时就会被 RPC 以 "Agent is already processing" 拒绝。
  pi.on("agent_end", (_e, ctx) => {
    if (!harnessEnabled(ctx)) return;
    if (lastEditTurn < 0) return; // 没改动,不要求验证
    if (lastVerifyTurn >= lastEditTurn) return; // 改动后已验证
    if (verifyNudged >= MAX_VERIFY_NUDGES) return;
    verifyNudged += 1;
    // 用 custom 消息 + display:false:进 LLM context 让模型继续,但不渲染成用户气泡
    pi.sendMessage(
      {
        customType: "harness-verify-nudge",
        content:
          "你还没有运行验证就准备结束。请先运行验证(测试/构建命令,或 verify 技能)确认改动通过,再报告结果。",
        display: false,
      },
      { deliverAs: "followUp", triggerTurn: true },
    );
  });

}
