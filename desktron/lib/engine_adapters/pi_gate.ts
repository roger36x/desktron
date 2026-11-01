/** Desk 在 Pi RPC 模式下的唯一显式扩展。
 *
 * Pi 本身没有沙箱或权限系统；tool_call hook 是执行前的 fail-closed 闸。RPC 侧把 confirm
 * 转成 extension_ui_request，Desk 收到人的决定后再回 extension_ui_response。
 */
import {
	getDocsPath,
	getExamplesPath,
	getReadmePath,
	type ExtensionAPI,
} from "@earendil-works/pi-coding-agent";

// This credential belonged to the retired Volcengine adapter. Remove stale local config
// before any model-visible tool can spawn a child process.
delete process.env.VOLCENGINE_WEB_SEARCH_API_KEY;

const SAFE_READ = new Set(["read", "grep", "find", "ls", "ask_question", "web_search", "web_fetch"]);
const SAFE_EDIT = new Set(["edit", "write"]);
const PREFIX = "DESKTRON_PERMISSION:";
// 与 memory_engine.gate.PATTERN_JS 同源；测试用 check_js_sync() 防止两侧漂移。
const MEM_BLOCK = /(^|\/)\.memory\/(_sealed|[\w.-]+\/(sot|shared)|local\/(private|shared))(\/|$)|(^|\/)memory\/(private|shared)(\/|$)/;
const MEM_MSG =
	"拒绝：_sealed 与 <工作流>/sot 是记忆准入闸管控目录，不能直接写或编辑。" +
	"请使用 memory skill 的 mem_add.py / mem_update.py；流水使用 journal skill。";
const KN_CACHE = new Set(["cache", "deepwiki", "alert-rules", "coverage", "__pycache__", "temp", "staging"]);
const KN_READ_EXEMPT = new Set([...KN_CACHE, "local", ".curator", ".kbkit"]);
const KN_EXEMPT = new Set(["INDEX.md", "KB-INDEX.md", "index.md", "README.md", "access-gaps.md"]);
const KN_RE = /(?:^|\/)knowledge\/(.+)$/;
const SH_WRITE_RE = /(?:>>?|\btee\b|\bsed\b[^|;&\n]*?-[a-z]*i[a-z]*|\bcp\b|\bmv\b|\bdd\b[^|;&\n]*?\bof=)[\s=]*(['"]?)([^\s"'|;&>()]+)\1/g;
const SH_READ_RE = /\b(?:cat|head|tail|less|more|bat|nl|xxd|od)\b\s+(?:-[^\s]+\s+)*(["']?)([^\s"'|;&]+)\1/g;

function knowledgeWriteBlocked(path: string): boolean {
	if (path.includes("/imports/")) return false;
	const match = KN_RE.exec(path);
	if (!match) return false;
	const parts = match[1].split("/");
	const base = parts.at(-1) || "";
	if (!base.endsWith(".md") || KN_EXEMPT.has(base)) return false;
	return !parts.slice(0, -1).some((part) => KN_CACHE.has(part) || part.startsWith("."));
}

function knowledgeReadBlocked(path: string): boolean {
	if (!path.endsWith(".md")) return false;
	const match = KN_RE.exec(path);
	if (!match) return false;
	const parts = match[1].split("/");
	if (KN_EXEMPT.has(parts.at(-1) || "")) return false;
	return !parts.slice(0, -1).some((part) => KN_READ_EXEMPT.has(part) || part.startsWith("."));
}

function protectedReason(tool: string, input: Record<string, unknown>): string | undefined {
	const path = String(input.path || input.filePath || input.file || input.filename || "");
	if (tool === "read" && knowledgeReadBlocked(path)) {
		return "拒绝裸读 git 内 knowledge 笔记：请使用 curator show（保留访问计数、置信度与冲突提示）。";
	}
	if (SAFE_EDIT.has(tool)) {
		if (MEM_BLOCK.test(path)) return MEM_MSG;
		if (knowledgeWriteBlocked(path)) {
			return "拒绝写入已退役的 knowledge 目录：新增事实请使用 memory skill 的 mem_add.py --tier fact --kind fact。";
		}
	}
	if (tool !== "bash") return undefined;
	const command = String(input.command || "");
	for (const match of command.matchAll(SH_WRITE_RE)) {
		const target = match[2] || "";
		if (MEM_BLOCK.test(target)) return MEM_MSG;
		if (knowledgeWriteBlocked(target)) {
			return "拒绝用 shell 写入已退役的 knowledge 目录：新增事实请使用 memory skill 的准入命令。";
		}
	}
	for (const match of command.matchAll(SH_READ_RE)) {
		if (knowledgeReadBlocked(match[2] || "")) {
			return "拒绝用 shell 裸读 git 内 knowledge 笔记：请使用 curator show。";
		}
	}
	return undefined;
}

export default function (pi: ExtensionAPI) {
	pi.on("before_agent_start", (event, ctx) => {
		event.systemPromptOptions.sections.docs = [
			"仅在用户询问 Pi 本身、SDK、扩展、主题、技能或 TUI 时读取：",
			`- ${getReadmePath()}`,
			`- ${getDocsPath()}`,
			`- ${getExamplesPath()}`,
			"实现前读完相关 Markdown 及其引用。",
		].join("\n");
		return undefined;
	});
	pi.on("tool_call", async (event, ctx) => {
		// recap 支线沿用主回合的工具定义以命中提示缓存，但不允许执行任何工具，
		// 包括只读工具。宿主在收到第一条助手完成帧后就结束进程。
		if (process.env.DESKTRON_PI_RECAP === "1") {
			return { block: true, reason: "recap 隔离支线禁止调用工具" };
		}
		const input = (event.input || {}) as Record<string, unknown>;
		const protectedBlock = protectedReason(event.toolName, input);
		if (protectedBlock) return { block: true, reason: protectedBlock };
		const mode = process.env.DESKTRON_PI_PERMISSION_MODE || "default";
		if (mode === "bypassPermissions" || SAFE_READ.has(event.toolName)) return undefined;
		if (mode === "acceptEdits" && SAFE_EDIT.has(event.toolName)) return undefined;
		if (!ctx.hasUI) {
			return { block: true, reason: "Desk 权限界面不可用，已安全拒绝" };
		}
		const confirmed = await ctx.ui.confirm(
			PREFIX + event.toolName,
			JSON.stringify({ tool: event.toolName, input }),
			{ timeout: 300_000, signal: ctx.signal },
		);
		if (!confirmed) {
			return { block: true, reason: "用户在 Desk 拒绝了该操作" };
		}
		return undefined;
	});
}
