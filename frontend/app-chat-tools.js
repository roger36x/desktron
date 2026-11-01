// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 3/13 片
// ---- 渲染 ------------------------------------------------------------------

const HS_ICONS = {
  qprev: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"15\" height=\"15\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.qprev\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m20.2 6.8-8.8 8.3a1.25 1.25 0 0 0 0 1.8l8.8 8.3\" /></g></svg>",
  qnext: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"15\" height=\"15\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.qnext\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m11.8 6.8 8.8 8.3a1.25 1.25 0 0 1 0 1.8l-8.8 8.3\" /></g></svg>",
  qclose: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"15\" height=\"15\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.qclose\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m7.2 7.2 17.6 17.6M24.8 7.2 7.2 24.8\" /></g></svg>",
  qpen: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"13\" height=\"13\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.qpen\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m6.5 20.3 13.2-13a3.55 3.55 0 0 1 5 5L11.5 25.2l-6.3 1.6Z\" /><path d=\"m17.6 9.4 5 5M6.5 20.3l5 4.9\" /></g></svg>",
  qchat: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"13.5\" height=\"13.5\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.qchat\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M10.5 5.8h11a6 6 0 0 1 6 6v7a6 6 0 0 1-6 6h-9.9l-6.7 3 1.3-6.3a6 6 0 0 1-1.7-4.2v-5.5a6 6 0 0 1 6-6Z\" /><path d=\"M10.8 13h10.4M10.8 18h6.6\" /></g></svg>",
  check: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"14\" height=\"14\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.check\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m5.4 16.6 6.7 7.1L26.6 8.3\" /></g></svg>",
  clip: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"16\" height=\"16\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.clip\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><g transform=\"translate(1 -1) rotate(45 15 17)\"><path d=\"M13 11v11a2 2 0 0 0 4 0V10a4 4 0 0 0-8 0v12a6 6 0 0 0 12 0V10\" /></g></g></svg>",
  // 回退:双左角标。原来画的是时钟叠一支逆时针箭头,三组笔画挤在 14px 里糊成一团;
  // 双角标只有两笔,小尺寸下仍然清晰,而且与「再问一次」的圆形箭头形状上不撞。
  rewind: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"14.5\" height=\"14.5\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.rewind\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m14.2 7.5-8.3 7.6a1.25 1.25 0 0 0 0 1.8l8.3 7.6M25.2 7.5l-8.3 7.6a1.25 1.25 0 0 0 0 1.8l8.3 7.6\" /></g></svg>",
  chev: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"14\" height=\"14\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.chev\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m11.8 6.8 8.8 8.3a1.25 1.25 0 0 1 0 1.8l-8.8 8.3\" /></g></svg>",
  lock: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"20\" height=\"20\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.lock\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"5.8\" y=\"13\" width=\"20.4\" height=\"14.3\" rx=\"4.1\" /><path d=\"M10.1 13V9.9a5.9 5.9 0 0 1 11.8 0V13M16 19.1v3.1\" /></g></svg>",
  unlock: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"20\" height=\"20\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.unlock\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"5.8\" y=\"13\" width=\"20.4\" height=\"14.3\" rx=\"4.1\" /><path d=\"M10.1 13V9.9a5.9 5.9 0 0 1 11.2-2.6M16 19.1v3.1\" /></g></svg>",
  copy: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"14.1\" height=\"14.1\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.copy\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"10.2\" y=\"10.2\" width=\"17\" height=\"17\" rx=\"3.7\" /><path d=\"M6.6 21.4A3.8 3.8 0 0 1 4.8 18V8.6a3.8 3.8 0 0 1 3.8-3.8H18a3.8 3.8 0 0 1 3.4 1.8\" /></g></svg>",
  // 已批准的独立重绘向下箭头；保持原显示尺寸，32格几何见 remaining-inline-icon-manifest.json。
  down: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"17\" height=\"17\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.down\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M16 5.5v20.7M7.6 18.3l8.4 8.4 8.4-8.4\" /></g></svg>",
  chev2: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"HS_ICONS.chev2\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m6.8 11.8 8.3 8.8a1.25 1.25 0 0 0 1.8 0l8.3-8.8\" /></g></svg>",
  fork: '<span class="hs-fork-icon" aria-hidden="true"></span>',
};
const HS_TOOL_LABELS = { read: "Read", write: "Write", edit: "Edit", bash: "Bash", grep: "Grep",
  glob: "Glob", list: "List", webfetch: "Fetch", task: "Subagent", agent: "Subagent",
  subagent: "Subagent", todowrite: "Todo", patch: "Patch" };
// 过程行的说法照 Codex:动作 + 对象(「编辑 engine_adapters.py」),而不是工具名 + 命令原文。
// 原文太长,一行截断的等宽灰字占掉半屏还读不出发生了什么;要看细节点开这一行即可。
// 值是语言包里的动作名：tool.<动作>.running / tool.<动作>.done。
const HS_TOOL_VERBS = { read: "read", write: "write", edit: "edit", multiedit: "edit",
  bash: "run", grep: "search", glob: "find", webfetch: "fetch", websearch: "search",
  mcp: "call", todowrite: "todo", notebookedit: "edit",
  askuserquestion: "ask" };
// 这一回合到此为止做了些什么:按工具归类计数,拼成一句话。
// 顶行原来说的是「当前这一步」,而下一行说的也是当前这一步,同一件事写了两遍;
// 归并之后顶行回答「这一轮在做什么」,下一行回答「此刻在哪一步」,两行各说一件事。
// 量词按工具分,不统一用「次」——「搜索 3 次」和「搜索 3 处」差着一层意思。

// 动词分两态:跑动中「执行中」,收尾后「已执行」。一列里既有正在跑的也有跑完的,
// 光看「执行」分不出哪条还在动。
function hsToolVerb(t, running) {
  const v = HS_TOOL_VERBS[String(t || "").toLowerCase()];
  if (!v) return hsToolLabel(t);
  return DeskI18n.t(`tool.${v}.${running ? "running" : "done"}`);
}

// 对象:文件取文件名,命令取首个可执行词,其余留空——一行里最该认出来的是「动了哪个东西」
// 一步做了什么,取一个能读的宾语。返回 { t, code }:code 决定它用等宽还是正文字体——
// 文件名、命令、模式是标识符,散文式的摘要不是,两者用同一种字体会互相冒充。
//
// 摘要不需要另外调模型:引擎自己就写。claude 的 Bash / Agent / TaskCreate 三个工具的入参里
// 带 description,是模型发起调用时随手写下的一句话(实测本会话 2775 次 bash 调用有 2775 条),
// 比命令首词说明得多——「python3」什么也没讲,「查找记录里的压缩标记」才是那步在干的事。
// 没有 description 的工具(读写编辑)靠文件名本身就够,不必补摘要。
// 等宽显示宽度:CJK、全角、emoji 占两列,ASCII 占一列。命令与路径都在等宽字体里读,
// 按列宽截断才不会被一条 CJK 长命令撑开一列步骤。claude 的 Bash 摘要用 Bun.stringWidth
// 同一口径,浏览器里没有,用 Intl.Segmenter 按 grapheme 自算。
function hsGlyphWide(cp) {
  return (cp >= 0x1100 && cp <= 0x115F) || (cp >= 0x2E80 && cp <= 0xA4CF) ||
         (cp >= 0xAC00 && cp <= 0xD7A3) || (cp >= 0xF900 && cp <= 0xFAFF) ||
         (cp >= 0xFE30 && cp <= 0xFE4F) || (cp >= 0xFF00 && cp <= 0xFF60) ||
         (cp >= 0xFFE0 && cp <= 0xFFE6) || (cp >= 0x1F000 && cp <= 0x1FAFF);
}
function hsStrWidth(s) {
  let w = 0;
  for (const g of (new Intl.Segmenter(undefined, { granularity: "grapheme" })).segment(String(s))) {
    const seg = g.segment;
    w += [...seg].length > 1 ? 2 : (hsGlyphWide(seg.codePointAt(0)) ? 2 : 1);
  }
  return w;
}
// 按显示宽度截断,超长末尾补 …。省略号本身占 1 列,截断预算留 1 列给它。
function hsCut(s, n) {
  s = String(s || "").trim();
  if (hsStrWidth(s) <= n) return s;
  let out = "", w = 0;
  for (const g of (new Intl.Segmenter(undefined, { granularity: "grapheme" })).segment(s)) {
    const seg = g.segment;
    const gw = [...seg].length > 1 ? 2 : (hsGlyphWide(seg.codePointAt(0)) ? 2 : 1);
    if (w + gw > n - 1) break;
    out += seg; w += gw;
  }
  return out + "…";
}

// 检索式与路径长得像,但正则的元字符是可靠的分界:一句给人读的说明里不会出现它们。
// 命中就整段不着色——宁可少涂,也不要把 `a|b` 里的半截当成文件。
// 下面的摘要也用同一道判据:检索式当宾语倒出来没人读得懂,那种整条退回命令原文。
const HS_PATTERNISH = /[|\\*+?^$[\]()]/;
// 网页搜索里这几种不是文件路径,但确实是完整的定位串。整段用标识符色,不能再交给
// HS_PATH_TOKEN 从第二个斜杠或域名处切开——那会画成灰色 `https:/` + 紫色 `/host/...`。
const HS_WEB_LOCATOR = /^(?:https?:\/\/|site:(?:https?:\/\/)?|(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,}(?:[\/:?#]|$))/i;
// site: 本身是机器定位符，后面的检索词却是给人读的自然语言。两者若整段使用
// 等宽字体，中文会回退到另一套 CJK 字体，同字号下字面框明显更大。
const HS_WEB_QUERY_HEAD = /^site:(?:https?:\/\/)?\S+/i;
// 动作行里的机器标识符按文字系统拆开：拉丁字母、数字与路径符号走较小的等宽字体，
// 中文与全角标点回到动作行正文字体。代码展开区仍需要 2:1 列对齐，不走这条规则。
const HS_CJK_RUN = /[\u2014\u2018-\u201D\u2026\u2E80-\u303F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF\u{20000}-\u{2FA1F}]+/gu;
// MCP 的 `server.tool` 是一个完整限定名。它不是文件名，整段都应使用标识符样式；
// 若交给路径正则，较长的 tool 名会因为扩展名长度上限只涂到一半。
const HS_QUALIFIED_ID = /^[A-Za-z][A-Za-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+$/;

// 把命令拆成阶段。; && || | 都是分隔,引号里的那些不算——`grep -E "a|b"` 是一个阶段。
// 2>&1 这类重定向里的 & 会被当成分隔切开,但切点在阶段末尾,首词与实参都不受影响。
function hsCmdStages(cmd) {
  const s = String(cmd || "");
  const out = [];
  let cur = "", quote = "";
  for (let i = 0; i < s.length; i++) {
    const c = s[i];
    if (quote) { cur += c; if (c === quote && s[i - 1] !== "\\") quote = ""; continue; }
    if (c === '"' || c === "'") { quote = c; cur += c; continue; }
    if (c === ";" || c === "|" || c === "&") {
      if (s[i + 1] === c) i++;          // && 与 || 是一个分隔,不是两个
      out.push(cur); cur = ""; continue;
    }
    cur += c;
  }
  out.push(cur);
  return out.map((x) => x.trim()).filter(Boolean);
}

// 这一段里够格当宾语的实参。选项、重定向、引号里的内容都不算——引号里装的是数据
// (检索式、内联脚本),不是这一步动的东西。
function hsCmdArgs(seg) {
  return String(seg).replace(/(["'])[\s\S]*?\1/g, " ").split(/\s+/).slice(1)
    .filter((a) => a && !a.startsWith("-") && !/^\d*[<>]/.test(a));
}
// 宾语:取最后一个实参的末段名。绝大多数命令把对象放在末尾(cat f、ls dir、sed -n … f)。
// 取不到就返回空串,由调用处退回命令原文:「读取 文件」这种没有宾语的说法什么也没说。
function hsCmdArg(seg) {
  const args = hsCmdArgs(seg);
  const base = (args[args.length - 1] || "").replace(/\/+$/, "").split("/").pop() || "";
  return HS_PATTERNISH.test(base) ? "" : base;
}

// 命令意图:模型没写 # 注释、入参也没有 description 时,把常见命令翻成「这一步想干什么」。
// 词表外的退回命令原文。
//
// 只看第一个真正的动作那一段。后面的 | head、; echo、2>&1 是配料,不是这一步的目的——
// 原来在整条命令上找第一个命中的词,于是 `ls -la 长路径 | head -30` 被说成「读取 head
// 后面那个文件」,`python3 -m pytest x.py | tail` 被说成「读取」。拿本机 pi 会话里实录的
// 71 条单行命令实测:命中 62 条,其中 29 条张冠李戴、22 条把 grep 的正则原样倒出来,
// 真正说对的只有 11 条(2026-08-17)。
//
// 宾语一律从同一段里取,不跨段。翻不出宾语的动词(「读取 文件」)不如原样的命令,
// 那种也退回原文。
// git 的全局选项剥掉,只留 `git <子命令> …`。codex 惯用 `git -C /abs/path status`
// (它的 cwd 未必是仓根),而判子命令的正则是 `^git\s+status\b`——中间夹着 -C 与一长串
// 路径就一条都不命中,一屏 git 卡片全退回命令原文。与后端 CodexAdapter._git_norm 同一份逻辑。
const HS_GIT_GLOBAL_VALUE = ["-C", "-c", "--git-dir", "--work-tree", "--namespace",
                             "--exec-path", "--super-prefix"];
function hsGitNorm(head) {
  const toks = String(head || "").split(/\s+/).filter(Boolean);
  if (!toks.length || toks[0].toLowerCase() !== "git") return head;
  let i = 1;
  while (i < toks.length) {
    const t = toks[i];
    if (HS_GIT_GLOBAL_VALUE.includes(t)) { i += 2; continue; }   // 连它的值一起跳
    if (t.startsWith("-")) { i += 1; continue; }                 // --no-pager / --git-dir=… / -P
    break;                                                       // 到子命令了
  }
  return ["git"].concat(toks.slice(i)).join(" ");
}

function hsCmdIntent(cmd) {
  const raw = String(cmd || "");
  // 脚本(多行、heredoc)不在这里翻:标题那一行本来就放不下,展开区里有原文
  if (!raw.trim() || raw.includes("<<") || raw.includes("\n")) return "";
  const head = hsGitNorm(hsCmdStages(raw).find((s) => !/^cd(\s|$)/.test(s)) || "");
  if (!head) return "";
  const low = head.toLowerCase();
  const obj = hsCmdArg(head);
  const say = (verb) => (obj ? verb + " " + obj : "");

  if (/^(?:rg|grep)\b/.test(low) || /^git\s+grep\b/.test(low)) {
    // 检索式本身就是宾语。引号里的优先,没引号取首个非选项的词。
    const q = head.match(/(["'])([\s\S]+?)\1/);
    const pat = q ? q[2]
                  : (head.split(/\s+/).slice(1).find((a) => a && !a.startsWith("-")) || "");
    return (!pat || HS_PATTERNISH.test(pat)) ? "" : DeskI18n.t("cmd.find") + " " + hsCut(pat, 28);
  }
  if (/^git\s/.test(low)) {
    // 子命令(diff / log …)是动词的一部分,不是宾语:不跳过它就成了「检查代码差异 diff」
    const gobj = hsCmdArg(head.replace(/^\s*git\s+\S+/i, "git"));
    const gsay = (verb) => (gobj ? verb + " " + gobj : verb);
    if (/^git\s+status\b/.test(low)) return DeskI18n.t("cmd.gitStatus");
    if (/^git\s+diff\b/.test(low)) return gsay(DeskI18n.t("cmd.gitDiff"));
    if (/^git\s+show\b/.test(low)) return DeskI18n.t("cmd.gitShow");
    if (/^git\s+log\b/.test(low)) return gsay(DeskI18n.t("cmd.gitLog"));
    if (/^git\s+branch\b/.test(low)) return DeskI18n.t("cmd.gitBranch");
    if (/^git\s+rev-parse\b/.test(low)) return DeskI18n.t("cmd.gitRevParse");
    if (/^git\s+ls-files\b/.test(low)) return DeskI18n.t("cmd.gitLsFiles");
    return "";
  }
  if (/\b(?:pytest|unittest)\b/.test(low)) return say(DeskI18n.t("cmd.test")) || DeskI18n.t("cmd.test");
  if (low.includes("py_compile") || low.includes("node --check") || low.includes("ast.parse")) {
    return say(DeskI18n.t("cmd.syntax")) || DeskI18n.t("cmd.syntax");
  }
  if (/^(?:shasum|sha256sum|md5)\b/.test(low)) return say(DeskI18n.t("cmd.checksum"));
  // -i 是就地改写,与只读的那几个不是一件事,说成「读取」是反的
  if (/^sed\b/.test(low)) return /\s-i\b/.test(head) ? say(DeskI18n.t("cmd.rewrite")) : say(DeskI18n.t("cmd.read"));
  // wc 不在这里:它数行数不是读内容,而且常常一次给好几个文件,说「读取 最后那个」是错的
  if (/^(?:cat|head|tail|less|more)\b/.test(low)) return say(DeskI18n.t("cmd.read"));
  if (/^(?:ls|tree)\b/.test(low)) return say(DeskI18n.t("cmd.list"));
  if (/^find\b/.test(low)) {
    const n = head.match(/-name\s+["']?([^\s"']+)/);
    return (n && !HS_PATTERNISH.test(n[1])) ? DeskI18n.t("cmd.findFile") + " " + n[1] : "";
  }
  if (/^curl\b/.test(low)) return DeskI18n.t("cmd.api");
  if (/^(?:python\d*|node|bash|sh|zsh)\b/.test(low)) {
    // 脚本名取第一个,不取末尾:后面跟的是脚本自己的参数(`python3 x.py --note 甲`
    // 的末位是「甲」)。取不到脚本名说明是 -c / -m 那类,退回命令原文。
    const s = hsCmdArgs(head).find((a) => /\.(?:py|js|mjs|sh)$/.test(a));
    return s ? DeskI18n.t("cmd.run") + " " + s.split("/").pop() : "";
  }
  return "";
}

// app-server 偶尔把多行 shell 显示成 `'# 意图\n命令"`:开头多一枚单引号、末尾多一枚
// 双引号,正文里的 jq 引号也跟着 shell-escape。这里只剥明确的「引号紧跟 #」外壳;
// 普通以字符串开头的命令不碰。
function hsCommandSource(command) {
  let src = String(command || "").trim();
  if (src.startsWith("{")) {
    try {
      const wrapped = JSON.parse(src);
      if (wrapped && typeof wrapped === "object" && !Array.isArray(wrapped)
          && Object.keys(wrapped).length === 1 && typeof wrapped.input === "string") {
        src = wrapped.input.trim();
      }
    } catch {}
  }
  if (/^["']#/.test(src)) {
    src = src.slice(1);
    if (/["']$/.test(src)) src = src.slice(0, -1);
  }
  return src;
}

// Code Mode 的 bash 入参不是 shell 本身,而是一小段调用 tools.exec_command 的 JS。
// 摘要与展开区都应看里面的 cmd；否则意图缺失时会把 `const r = await …` 当成命令。
// 不用 eval 解析这段模型输出，只读取普通单/双引号与模板字符串，避免执行任何内容。
function hsJsString(literal) {
  const raw = String(literal || "");
  if (raw.length < 2) return "";
  if (raw[0] === '"') {
    try { return JSON.parse(raw); } catch { return ""; }
  }
  if (!"'`".includes(raw[0]) || raw[raw.length - 1] !== raw[0]) return "";
  const esc = { n: "\n", r: "\r", t: "\t", b: "\b", f: "\f", v: "\v", 0: "\0" };
  let out = "";
  for (let i = 1; i < raw.length - 1; i++) {
    let c = raw[i];
    if (c !== "\\") { out += c; continue; }
    c = raw[++i];
    if (c == null) break;
    if (c === "\n") continue;                         // JS 行续接
    if (c === "\r") { if (raw[i + 1] === "\n") i++; continue; }
    if (c === "x" && /^[0-9a-f]{2}$/i.test(raw.slice(i + 1, i + 3))) {
      out += String.fromCharCode(parseInt(raw.slice(i + 1, i + 3), 16)); i += 2; continue;
    }
    if (c === "u" && /^[0-9a-f]{4}$/i.test(raw.slice(i + 1, i + 5))) {
      out += String.fromCharCode(parseInt(raw.slice(i + 1, i + 5), 16)); i += 4; continue;
    }
    out += Object.prototype.hasOwnProperty.call(esc, c) ? esc[c] : c;
  }
  return out;
}

function hsNestedCommands(command) {
  const src = String(command || "");
  if (!/\btools\.exec_command\s*\(/.test(src)) return [];
  const re = /(?:["']cmd["']|\bcmd\b)\s*:\s*("(?:[^"\\]|\\[\s\S])*"|'(?:[^'\\]|\\[\s\S])*'|`(?:[^`\\]|\\[\s\S])*`)/g;
  const out = [];
  for (const m of src.matchAll(re)) {
    const value = hsJsString(m[1]);
    if (value) out.push(hsCommandSource(value));
  }
  return out;
}

// 模型显式写下的工具意图。Codex 的 Code Mode 外层是 JS,用 `// @intent:`;
// 直接 shell 与 pi 仍用首行 `# 意图`。只认 JS 前导注释区,不扫代码正文——示例字符串里
// 即使碰巧出现 marker,也不能反过来给整次调用改标题。
const HS_INTENT_PLACEHOLDERS = new Set([
  "意图", "本步意图", "本步目的", "目的", "摘要", "intent", "purpose",
]);

function hsIntentValue(value) {
  const text = String(value || "").trim();
  const plain = text.replace(/^[<《【\[（(]+|[>》】\]）)]+$/g, "").trim().toLowerCase();
  return HS_INTENT_PLACEHOLDERS.has(plain) ? "" : text;
}

function hsIntentMarker(command, codeOnly) {
  const lines = hsCommandSource(command).split(/\r?\n/);
  if (!lines.length) return "";
  const first = lines[0].trim();
  if (!codeOnly && first.startsWith("#") && !first.startsWith("#!")) {
    return first.replace(/^#+/, "").trim();
  }
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    const m = line.match(/^\/\/\s*@intent\s*[:：]\s*(.+?)\s*$/i);
    if (m) return m[1].trim();
    if (line.startsWith("// @exec:")) continue;
    break;
  }
  return "";
}

function hsIntentComment(command, codeOnly) {
  return hsIntentValue(hsIntentMarker(command, codeOnly));
}

// 摘要的来源优先级与步骤标题一致。显式 @intent 可以覆盖后端此前猜出的「读取 x」;
// claude 原生 description 则优先于普通 shell 注释,免得 shellcheck 指令冒充任务意图。
function hsToolDescription(inp) {
  const explicit = hsIntentComment(inp.command, true);
  if (explicit) return explicit;
  const desc = hsIntentValue(inp.description);
  if (desc) return desc;
  const direct = hsIntentComment(inp.command, false);
  if (direct) return direct;
  const nested = hsNestedCommands(inp.command);
  const intents = nested.map((cmd) => hsIntentComment(cmd, false)).filter(Boolean);
  if (!intents.length) return "";
  return intents[0] + (nested.length > 1 ? DeskI18n.t("tool.andMore", { count: nested.length }) : "");
}

// 展开区已经另画一行 `# 摘要`,命令正文就不再重复那一行。只在首行确实被拿来当摘要时删;
// claude 的 description 若与 shell 技术注释不同,注释仍属于真实命令,完整保留。
function hsDirectCommandView(command, description) {
  const src = hsCommandSource(command);
  const lines = src.split(/\r?\n/);
  if (!lines.length) return "";
  let at = 0;
  while (at < lines.length && !lines[at].trim()) at++;
  const codeMarker = hsIntentMarker(src, true);
  if (codeMarker) {
    if (lines[at] && lines[at].trim().startsWith("// @exec:")) lines.splice(at, 1);
    while (at < lines.length && !lines[at].trim()) at++;
    if (lines[at] && /^\/\/\s*@intent\s*[:：]/i.test(lines[at].trim())) lines.splice(at, 1);
    return lines.join("\n").trim();
  }
  const shellMarker = hsIntentMarker(src, false);
  const shellIntent = hsIntentValue(shellMarker);
  const desc = hsIntentValue(description);
  if (shellMarker && ((!shellIntent) || !desc || desc === shellIntent)
      && /^#(?!\!)/.test(lines[at].trim())) {
    lines.splice(at, 1);
  }
  return lines.join("\n").trim();
}

function hsCommandView(command, description) {
  const nested = hsNestedCommands(command);
  if (!nested.length) return hsDirectCommandView(command, description);
  return nested.map((cmd) => {
    // 嵌套 cmd 的首行按 Code Mode 约定就是该命令的意图；占位词同样会在这里被摘掉。
    const own = hsIntentComment(cmd, false);
    return hsDirectCommandView(cmd, own || description);
  }).filter(Boolean).join("\n\n");
}

function hsNestedCommandIntents(commands) {
  const out = [];
  for (const command of commands) {
    const view = hsDirectCommandView(command, hsIntentComment(command, false));
    for (const line of view.split(/\r?\n/).map((x) => x.trim()).filter(Boolean)) {
      const intent = hsCmdIntent(line);
      if (intent) out.push(intent);
    }
  }
  return out;
}

function hsSubagentSummary(inp) {
  let tasks = [];
  if (typeof inp.task === "string") tasks.push(inp.task);
  if (Array.isArray(inp.tasks)) {
    tasks.push(...inp.tasks.map((item) => item && item.task).filter((task) => typeof task === "string"));
  }
  if (!tasks.length && typeof inp.description === "string") tasks.push(inp.description);
  if (!tasks.length && typeof inp.prompt === "string") tasks.push(inp.prompt);
  tasks = tasks.map((task) => String(task).replace(/\s+/g, " ").trim()
    // 绝对路径是任务的定位信息，不该吃掉整条摘要；保留末段即可辨认对象。
    .replace(/(^|\s)((?:~|\/)\S*[A-Za-z0-9._-])/g, (all, lead, path) => {
      const tail = path.replace(/\/+$/, "").split("/").pop();
      return lead + (tail || path);
    })).filter(Boolean);
  if (!tasks.length) return "";
  if (tasks.length === 1) return hsCut(tasks[0], 46);
  const width = Math.max(10, Math.floor((46 - tasks.length + 1) / tasks.length));
  return hsCut(tasks.map((task) => hsCut(task, width)).join("；"), 46);
}

function hsToolObj(part) {
  const inp = (part.state || {}).input || {};
  const kind = String(part.tool || "").toLowerCase().replace(/[_-]/g, "");
  if (["task", "agent", "subagent"].includes(kind)) {
    return { t: hsSubagentSummary(inp), code: false };
  }
  if (kind === "websearch") {
    const query = inp.pattern || inp.query
      || (Array.isArray(inp.queries) ? inp.queries.filter(Boolean).join(", ") : "");
    if (query) {
      const text = String(query).trim();
      // 动词已经是「已搜索」,这里只放宾语；否则后端的 description 会画成
      // 「已搜索 搜索 https:…」。URL / site: 是一个完整定位串,其余查询仍按检索式灰显。
      const mixed = HS_WEB_QUERY_HEAD.test(text) && /\s/.test(text);
      return { t: hsCut(text, 46), code: !mixed, webq: mixed, pat: !HS_WEB_LOCATOR.test(text) };
    }
  }
  const desc = hsToolDescription(inp);
  if (desc) return { t: hsCut(desc, 46), code: HS_QUALIFIED_ID.test(desc) };
  const f = inp.file_path || inp.filePath || inp.path || inp.notebook_path;
  if (f) return { t: String(f).split("/").pop(), code: true };
  // 检索式单独标一档。它是机器串(要等宽),但不是可定位的对象——淡紫在这套 UI 里说的是
  // 「这东西点得开、找得到」,而 `**/*writing*voice*/SKILL.md` 谁也打不开,它是查询条件。
  // 着成路径色只会让人误读成「读了这个文件」,何况 glob 本来就长得像路径。
  if (inp.pattern) return { t: hsCut(String(inp.pattern), 40), code: true, pat: true };
  if (inp.url) return { t: String(inp.url).replace(/^https?:\/\//, "").split("/")[0], code: true };
  if (inp.command) {
    const raw = hsCommandSource(inp.command);
    const nested = hsNestedCommands(raw);
    if (nested.length) {
      const intents = hsNestedCommandIntents(nested);
      if (intents.length) {
        const title = intents[0] + (intents.length > 1 ? DeskI18n.t("tool.andMore", { count: intents.length }) : "");
        return { t: hsCut(title, 46), code: false };
      }
      // 认不出动作时也只退回内部命令，不能泄漏 Code Mode 的 JS 包装层。
      const inner = hsCommandView(raw, "").replace(/\s+/g, " ");
      return { t: hsCut(inner, 46), code: true, cmd: true };
    }
    // 没有显式意图:查命令意图词表,把常见命令翻译成「这一步想干什么」,词表外退回原文。
    const intent = hsCmdIntent(raw);
    if (intent) return { t: hsCut(intent, 46), code: false };
    // 前面的 cd 是铺垫,剥掉;剥完什么都不剩(命令就是个 cd)时退回原样,
    // 空着一行「已执行」比多余的 cd 更难读。
    const c = (raw.replace(/^cd\s+\S+\s*&&\s*/, "") || raw).replace(/\s+/g, " ");
    // 带上参数,不只取首词。首词是动作,宾语在参数里:「ls」谁也看不出列的是哪个目录,
    // 「ls -la /tmp/x」才是那一步真正做的事(2026-08-17 用户实见,pi 的 bash 不写
    // description,一列步骤于是全是光秃秃的 ls)。claude 那侧有 description 的照旧优先用它,
    // 走不到这里。超长按显示宽度截断、末尾补 …,和 claude 的 Bash 摘要同口径。
    return { t: hsCut(c, 46), code: true, cmd: true };
  }
  // 认不出的入参形状:先看引擎自己给的标题。某些 skill / webfetch 工具
  // 入参只有 {name} / {url},但 state.title 里写着「Loaded skill: lark-doc」——
  // 引擎自报的那句话比从输出里抠一句准。各引擎共用这一条,不为某一家单写分支。
  if (inp.name && typeof inp.name === "string") return { t: hsCut(inp.name, 40), code: true };
  const title = String((part.state || {}).title || "").trim();
  if (title) return { t: hsCut(title, 46), code: false };
  // 入参一个字段都没有、引擎也没给标题:引擎那侧的回填在权限打断时会错位,已修,但历史记录里
  // 存着一批,而且不是所有情况都救得回来。与其留一行没有宾语的「已执行」,不如从结果里退一句话
  // 顶上——说不清做了什么,至少说清它的下场。
  return { t: hsToolGist(part), code: false };
}



// 摘要是自然语言，但其中的文件名和路径仍属于可定位对象，沿用默认的淡紫标识符样式。
// 整句套等宽会把“读取”“查找”也误画成代码，因此只给命中的片段加色。
// 三条分支各自要求一个明确的路径特征:显式前缀(/ ~/ ./ ../)、带斜杠且末段有扩展名、
// 或者本身就是一个带扩展名的文件名。原来中间那条是「凡带斜杠都算」,于是检索式
// `session/adopt|adopt_session|` 里的 session/adopt 也被涂成了文件(2026-08-10 实见)。
// 起手加一道词边界:否则散文里的 `session/adopt` 会从中间的斜杠切进去,把 `/adopt`
// 当成绝对路径涂色。
const HS_PATH_TOKEN = /(?<![A-Za-z0-9_@.+-])(?:(?:~|\.{1,2})?\/(?:[A-Za-z0-9_@.+\-\u3400-\u4DBF\u4E00-\u9FFF]+\/)*[A-Za-z0-9_@.+\-\u3400-\u4DBF\u4E00-\u9FFF]+|(?:[A-Za-z0-9_@.+\-\u3400-\u4DBF\u4E00-\u9FFF]+\/)+[A-Za-z0-9_@+\-\u3400-\u4DBF\u4E00-\u9FFF]+\.[A-Za-z][A-Za-z0-9]{0,8}|(?:[A-Za-z0-9_@+\-\u3400-\u4DBF\u4E00-\u9FFF]+\.)+[A-Za-z][A-Za-z0-9_-]{0,9})/g;

function hsMachineTextPaint(el, text) {
  let at = 0;
  for (const m of String(text || "").matchAll(HS_CJK_RUN)) {
    if (m.index > at) el.appendChild(document.createTextNode(text.slice(at, m.index)));
    const span = document.createElement("span");
    span.className = "hs-step-cjk";
    span.textContent = m[0];
    el.appendChild(span);
    at = m.index + m[0].length;
  }
  if (at < text.length) el.appendChild(document.createTextNode(text.slice(at)));
}

function hsToolTargetPaint(el, obj, running) {
  const text = String(obj.t || "") + (running && obj.t ? "…" : "");
  el.textContent = "";
  el.classList.toggle("txt", !obj.code);
  el.classList.toggle("pat", !!obj.pat);   // 检索式:等宽照旧,配色退回正文灰
  el.classList.toggle("cmd", !!obj.cmd);   // 脚本/命令:等宽照旧,但不再用可定位对象的强调色
  el.classList.toggle("webq", !!obj.webq); // site: 等宽；后面的自然语言回到界面字体
  if (obj.webq) {
    const match = text.match(HS_WEB_QUERY_HEAD);
    const head = match ? match[0] : "";
    if (head) {
      const span = document.createElement("span");
      span.className = "hs-step-locator";
      hsMachineTextPaint(span, head);
      el.appendChild(span);
      el.appendChild(document.createTextNode(text.slice(head.length)));
      return;
    }
  }
  if (obj.code || HS_PATTERNISH.test(text)) { hsMachineTextPaint(el, text); return; }
  let at = 0;
  for (const m of text.matchAll(HS_PATH_TOKEN)) {
    if (m.index > at) el.appendChild(document.createTextNode(text.slice(at, m.index)));
    const span = document.createElement("span");
    span.className = "hs-step-path";
    hsMachineTextPaint(span, m[0]);
    el.appendChild(span);
    at = m.index + m[0].length;
  }
  if (at < text.length) el.appendChild(document.createTextNode(text.slice(at)));
}

// 从工具结果里抠一句概括。认得出的几种下场直接给定语,认不出就取输出首行。
const HS_GIST = [
  [/requires? (approval|permission)|permission denied|user (denied|rejected)/i, "tool.notApproved"],
  [/interrupted|aborted by user|cancell?ed/i, "tool.interrupted"],
  [/timed? ?out/i, "tool.timedOut"],
];
function hsToolOutcome(part, running) {
  const st = part.state || {};
  if (st.status === "error") return DeskI18n.t(st.failure?.kind === "validation" ? "tool.invalid" : "tool.failed");
  return hsToolVerb(part.tool, running);
}

function hsToolGist(part) {
  const st = part.state || {};
  const out = String(st.output || "").trim();
  if (st.status === "running") return "";        // 还在跑,等入参流完就有了
  for (const [re, label] of HS_GIST) if (re.test(out)) return DeskI18n.t(label);
  const line = out.split("\n").map((x) => x.trim()).find(Boolean) || "";
  if (line) return line.slice(0, 46);
  return DeskI18n.t("tool.noInput");
}

function hsToolLabel(t) {
  const k = String(t || "tool").toLowerCase();
  return HS_TOOL_LABELS[k] || (k.charAt(0).toUpperCase() + k.slice(1));
}

function hsEsc(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function hsClip(s, n) {
  const t = String(s);
  return hsEsc(t.slice(0, n)) + (t.length > n ? DeskI18n.t("tool.truncated") : "");
}

// 过程展开态按会话保存。页面刷新与回合末对账都会重建节点，展开偏好不能绑在 DOM 上。
function hsProcOpenKey(t) {
  const text = String(t.user || "").replace(/\s+/g, " ").trim();
  return text ? "text:" + text.slice(0, 180) : "id:" + String(t.uid || t.msgId || t.uts || "");
}

function hsProcOpenSet(v) {
  if (v.procOpen) return v.procOpen;
  try { v.procOpen = new Set(JSON.parse(localStorage.getItem("hs:proc:" + v.sid) || "[]")); }
  catch { v.procOpen = new Set(); }
  return v.procOpen;
}

function hsProcOpenSave(t, open) {
  const v = t.view;
  if (!v) return;
  const set = hsProcOpenSet(v), key = hsProcOpenKey(t);
  if (open) set.add(key); else set.delete(key);
  try { localStorage.setItem("hs:proc:" + v.sid, JSON.stringify([...set].slice(-100))); } catch {}
}

// 工具调用不各自成卡,合并进本回合的一条「已处理」折叠行——过程收起来,正文留在外面。
// 过程区空了但这一回合确实有过程:按 t.parts 重画一遍。返回是否画出了东西。
// 思考、工具与审批都属于过程；正文不属于过程区。
function hsProcRepaint(t, body) {
  let n = 0;
  for (const q of t.parts.values()) {
    if (q.type !== "tool" && q.type !== "reasoning" && q.type !== "approval" && q.type !== "compact") continue;
    n++;
    if (q.el && q.el.parentNode === body) continue;
    q.el = null;            // 旧节点已经不在这儿了,让 hsPaint 重建并挂进来
    q.sig = null;           // 签名去重会拦住重建,一并清掉
    try { hsPaint(t.view, q); } catch { n--; }
  }
  if (t.failureError) {
    hsProcFailure(t, t.failureError);
    n++;
  }
  return n > 0 && body.children.length > 0;
}

// 回合失败属于这一轮的处理结果，与思考和工具步骤一起收在过程区里。后端已经把不同
// 引擎的失败统一成 source / code / message；这里不按引擎或错误码分支，只展示脱敏后的正文。
// 把错误留在 turn 上，过程盒在重建后仍可由 hsProcRepaint 恢复同一条详情。
function hsProcFailure(t, error) {
  const message = String((error || {}).message || "").trim();
  if (!t || !message) return false;
  t.failureError = { ...(error || {}), message };
  const body = hsProcBox(t).querySelector(".hs-proc-body");
  let el = body.querySelector(".hs-turn-failure");
  if (!el) {
    el = document.createElement("div");
    el.className = "hs-turn-failure";
    el.innerHTML = '<span class="hs-turn-failure-k"></span><span class="hs-turn-failure-v"></span>';
    DeskI18n.bind(el.querySelector(".hs-turn-failure-k"), "tool.stopReason");
    body.appendChild(el);
  }
  hsSetText(el.querySelector(".hs-turn-failure-v"), message);
  hsProcBox(t).classList.add("has-steps");
  return true;
}

function hsProcBox(t) {
  // 判据是「它还在这一回合的容器里吗」,不是「有没有父节点」,也不是「在不在文档里」。
  //   parentNode:整片重建之后旧盒子仍挂在那片已脱离文档的旧节点上,不为空——照它判,
  //     新画的步骤全塞进一个看不见的盒子:过程区点不开、正文也跟着不见(日志里是
  //     「parts=30 步骤=3」)。
  //   isConnected:往更早翻页那一路(hsPrepend)是先在一个游离的 scratch 里铺好再整体搬进去,
  //     那期间整棵都不在文档里,照它判就每画一个 part 建一个新盒子——一屏十几条分割线,
  //     刷新之后才恢复(2026-08-17 用户实见)。
  // holder.contains 两种情形都对:问的正是「这个盒子是不是我此刻要画进去的那个容器里的」。
  if (t.procEl && t.holder && t.holder.contains(t.procEl)) return t.procEl;
  // 旧盒子掉出文档时(重建走 replaceChildren,整片旧节点会被换掉),它里面是这一回合
  // 已经画好的步骤,而每个步骤的 part.el 还指着那儿——hsPaintTool 只在 part.el 为空时
  // 才新建并挂载,所以直接另起一个空盒子等于把它们永久丢掉:盒子在、过程区是空的、
  // 点了没反应,而状态行仍能报出最后一个工具名(t.parts 还在)。刷新或 sync home 之后
  // 「等待中」展不开就是这么来的。所以接过来,不重画。
  const old = t.procEl;
  const el = document.createElement("div");
  el.className = "hs-proc";
  // 一回合只剩这一条:状态、步数、展开记号都在上面,点它展开整个处理过程(思考 + 工具)。
  // 原来是「思考」「已处理 N 步」两条折叠行加一条浮动状态,三处讲同一件事。
  el.innerHTML = '<div class="hs-proc-bar"><span class="hs-proc-slot"></span>'
    + "</div>"
    + '<div class="hs-proc-body"></div><div class="hs-rule"></div>';
  // 这一行只在有过程可看时才存在(过程为空的由 hsProcTrim 撤掉),所以点它永远不该没反应。
  // 原来的写法是「过程区没有子节点就直接返回」,于是渲染一旦掉了队——节点留在某个已经
  // 脱离文档的旧盒子里,而 hsPaintTool 只在 part.el 为空时才重建——这一行就变成一个死的
  // 摆设:数据在 t.parts 里,状态行还能报出最后一个工具名,点下去却什么也不发生
  // (2026-08-10 实见,切引擎重载之后必现)。改成就地补画再展开。
  el.querySelector(".hs-proc-bar").onclick = () => {
    const body = el.querySelector(".hs-proc-body");
    if (!body.children.length && !hsProcRepaint(t, body)) return;
    hsViewportChange(t.view, () => {
      el.classList.toggle("open");
      hsProcOpenSave(t, el.classList.contains("open"));
      // 只在外层展开/收起这一刻遍历。流式帧仍仅更新自己的 part，工具详情保持懒加载。
      for (const q of t.parts.values()) {
        if (q.type !== "reasoning") continue;
        if (el.classList.contains("open")) q._fullAttempted = false;
        hsPaint(t.view, q);
      }
    }, false, "process-toggle");
  };
  el.classList.add("has-body");
  if (old) {
    // 步骤与顶行原样搬过来:append 是移动不是复制,part.el 仍指向同一批节点,
    // 后续的 part.updated 照常落回它们身上。展开态也一并沿用。
    const take = (sel) => {
      const from = old.querySelector(sel), to = el.querySelector(sel);
      if (from && to) to.append(...from.childNodes);
    };
    take(".hs-proc-body");
    take(".hs-proc-slot");
    if (el.querySelector(".hs-proc-body").children.length) el.classList.add("has-steps");
    if (old.classList.contains("open")) el.classList.add("open");
  }
  // 插到最前而不是追加:这一条是回合的顶行,而它是懒建的——没有工具调用的回合要等状态行
  // 来了才创建,那时正文已经在 holder 里,追加就落到正文下面去了(重建后尤其明显)。
  t.holder.insertBefore(el, t.holder.firstChild);
  t.procEl = el;
  if (hsProcOpenSet(t.view).has(hsProcOpenKey(t))) el.classList.add("open");
  return el;
}

// 命令的高亮:交给 hljs 的 bash 词法,与改动、读取两处同一套配色。
// hljs 不在、或它对这段命令抛错时,退回下面那个只认字符串与注释的扫描——
// 命令里常有 heredoc、进程替换这类结构,高亮不了也不该让整段消失。
function hsShHi(raw) {
  const src = String(raw || "");
  const hi = hsHiCode(src, "bash");
  if (hi !== null) return hi;
  // 一次扫过,不分别处理——注释符落在字符串里不该当注释,反之亦然,分开做就会互相误判。
  const re = /("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|#[^\n]*)/g;
  let out = "", i = 0, m;
  while ((m = re.exec(src))) {
    out += hsEsc(src.slice(i, m.index));
    out += `<span class="${m[0][0] === "#" ? "sh-c" : "sh-s"}">${hsEsc(m[0])}</span>`;
    i = m.index + m[0].length;
  }
  return out + hsEsc(src.slice(i));
}

// 写文件类的工具按增删行呈现,不摆原始入参。整篇内容塞进一个 JSON 字段里、换行还是字面量
// 的 \n,那不是给人读的形态——终端里这类操作一律显示成带 +/- 的行,这里对齐它。
// 行号不从入参猜；工具结果若带真实位置就直接使用，否则再走兼容定位。
const HS_DIFF_MAX = 120;   // 最多铺这么多行,再多在末尾注明省了几行
const HS_DIFF_CTX = 3;     // 每处改动上下各留几行不变的做参照

function hsDiffSplit(text) {
  return String(text == null ? "" : text).replace(/\n$/, "").split("\n");
}

// 逐行求最长公共子序列。编辑工具给的是替换前后的两段文本,直接一整块删、一整块增的话,
// 只改了一个词的那种也要人自己去两段里对眼——把没变的行认出来当参照,改动才看得出在哪。
// 规模按行算,入参通常十几行;超过阈值退化成整块删增,不值得为极端情形付平方级的代价。
function hsLineDiff(a, b) {
  if (a.length > 400 || b.length > 400) {
    return a.map((t) => ({ kind: "del", text: t })).concat(b.map((t) => ({ kind: "add", text: t })));
  }
  const n = a.length, m = b.length;
  const dp = Array.from({ length: n + 1 }, () => new Uint16Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      dp[i][j] = a[i] === b[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
    }
  }
  const out = [];
  let i = 0, j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { out.push({ kind: "ctx", text: a[i] }); i++; j++; }
    else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ kind: "del", text: a[i] }); i++; }
    else { out.push({ kind: "add", text: b[j] }); j++; }
  }
  while (i < n) out.push({ kind: "del", text: a[i++] });
  while (j < m) out.push({ kind: "add", text: b[j++] });
  return out;
}

// 只留改动附近的几行参照,中间成片没变的折成一道空隙。全铺出来的话,一次小改动会拖着
// 几十行无关内容,反而把改在哪淹掉。
function hsDiffTrim(rows) {
  const keep = new Array(rows.length).fill(false);
  rows.forEach((r, i) => {
    if (r.kind === "ctx") return;
    for (let k = Math.max(0, i - HS_DIFF_CTX); k <= Math.min(rows.length - 1, i + HS_DIFF_CTX); k++) keep[k] = true;
  });
  const out = [];
  let cut = false;
  rows.forEach((r, i) => {
    if (keep[i]) { out.push(r); cut = false; }
    else if (!cut) { out.push({ kind: "gap", text: "", skipped: 1 }); cut = true; }
    else out[out.length - 1].skipped++;
  });
  return out;
}

// full=true:不做折叠,把全量行交出去(审阅侧栏自己折,并把折掉的行留着以便就地展开)
function hsDiffRows(tool, input, full) {
  const trim = full ? ((r) => r) : hsDiffTrim;
  const t = String(tool || "").toLowerCase();
  let rows = [];
  let path = input.file_path || input.path || input.filePath || "";
  if (t === "write") {
    // 写整篇也按增行画,与 edit / multiedit 同一套形制。此前它单独一档(只给左缘一道绿轨、
    // 不铺行底色),理由是整篇标绿等于整块涂色;但同一段会话里两种改文件的动作长得不一样,
    // 读的人要先分辨这是哪一种才知道颜色怎么解释,代价比那点涂色更大。
    rows = hsDiffSplit(input.content).map((x) => ({ kind: "add", text: x }));
  } else if (t === "edit") {
    const changes = Array.isArray(input.changes) && input.changes.length
      ? input.changes : (input.diff ? [{ file_path: input.file_path || "", diff: input.diff }] : []);
    if (changes.length) {
      path = changes[0].file_path || path;
      for (const c of changes) {
        if (changes.length > 1) rows.push({ kind: "file", text: c.file_path || "" });
        for (const line of hsDiffSplit(c.diff || "")) {
          if (line.startsWith("--- ") || line.startsWith("+++ ")) continue;
          if (line.startsWith("@@") || line.startsWith("\\ No newline")) {
            rows.push({ kind: "meta", text: line });
          } else if (line.startsWith("+")) rows.push({ kind: "add", text: line.slice(1) });
          else if (line.startsWith("-")) rows.push({ kind: "del", text: line.slice(1) });
          else rows.push({ kind: "ctx", text: line.startsWith(" ") ? line.slice(1) : line });
        }
      }
    } else if (Array.isArray(input.edits) && input.edits.length) {
      // Pi 的 edit 一次可以带多组 old/new；适配层会把 oldText/newText 归一成这两个字段。
      for (const e of input.edits) {
        if (rows.length) rows.push({ kind: "gap", text: "" });
        rows = rows.concat(trim(hsLineDiff(hsDiffSplit(e.old_string ?? e.oldText),
                                          hsDiffSplit(e.new_string ?? e.newText))));
      }
    } else {
      rows = trim(hsLineDiff(hsDiffSplit(input.old_string), hsDiffSplit(input.new_string)));
    }
  } else if (t === "multiedit") {
    for (const e of input.edits || []) {
      if (rows.length) rows.push({ kind: "gap", text: "" });
      rows = rows.concat(trim(hsLineDiff(hsDiffSplit(e.old_string), hsDiffSplit(e.new_string))));
    }
  } else {
    return null;
  }
  return rows.length ? { rows, path } : null;
}

// 增删行数。摆在步骤那一行的文件名后面(「已编辑 app-chat.js +3 -1」),而不是框内另起一行:
// 折叠着的时候就该看得到改了多大,展开只为看改了什么。
function hsDiffStat(tool, input) {
  const r = hsDiffRows(tool, input);
  if (!r) return "";
  const add = r.rows.filter((x) => x.kind === "add").length;
  const del = r.rows.filter((x) => x.kind === "del").length;
  return { add, del };
}

// 整块高亮之后按行切开。逐行分别高亮是做不成的:python 的 docstring、C 的块注释这类
// 结构跨行,单看一行认不出自己在注释里面,颜色就断在半路。切的办法是行尾把还开着的
// span 收起来,下一行开头照原样重开。
function hsHiSplit(html) {
  const lines = [], open = [];
  let cur = "", i = 0;
  while (i < html.length) {
    if (html[i] === "<") {
      const j = html.indexOf(">", i);
      if (j < 0) { cur += html.slice(i); break; }
      const tag = html.slice(i, j + 1);
      if (tag[1] === "/") open.pop(); else open.push(tag);
      cur += tag;
      i = j + 1;
      continue;
    }
    let end = html.indexOf("<", i);
    if (end < 0) end = html.length;
    const nl = html.indexOf("\n", i);
    if (nl >= 0 && nl < end) end = nl;
    cur += html.slice(i, end);
    i = end;
    if (html[i] === "\n") {
      lines.push(cur + "</span>".repeat(open.length));
      cur = open.join("");
      i++;
    }
  }
  lines.push(cur);
  return lines;
}

const HS_HI_MAX = 60 * 1024;   // 超过这么长不高亮:同步跑一遍会卡住渲染,不值得

// 一整段过一遍高亮,拿回 HTML;语言不认、hljs 不在、抛错、太长,一律返回 null 由调用方退回纯文本。
function hsHiCode(src, lang) {
  if (!src || !lang || typeof hljs === "undefined" || src.length > HS_HI_MAX) return null;
  try {
    if (!hljs.getLanguage(lang)) return null;
    return hljs.highlight(src, { language: lang, ignoreIllegals: true }).value;
  } catch { return null; }
}

// 按文件名取语言,整篇高亮后拿回逐行的 HTML。切回来的行数与原行数对不上就返回 null
// ——宁可不上色,也不能让行与行错位。
function hsHiRows(texts, path) {
  const ext = String(path || "").split(".").pop().toLowerCase();
  const lang = typeof _CODE_LANG === "object" ? _CODE_LANG[ext] : "";
  if (!texts.length || !lang) return null;
  const hi = hsHiCode(texts.join("\n"), lang);
  if (hi === null) return null;
  const out = hsHiSplit(hi);
  return out.length === texts.length ? out : null;
}

function hsDiffDetail(tool, input) {
  const r = hsDiffRows(tool, input);
  if (!r) return "";
  const rows = r.rows;
  const mark = { add: "+", del: "-", ctx: " " };
  const shown = rows.slice(0, HS_DIFF_MAX);
  // 高亮按新旧两侧分别算:参照行两侧都在,增行只在新侧,删行只在旧侧。各自成篇再高亮,
  // 跨行的结构才不会被对侧插进来的行截断。
  const hiSide = (kinds) => {
    const idx = [], txt = [];
    shown.forEach((x, i) => { if (kinds.includes(x.kind)) { idx.push(i); txt.push(x.text); } });
    const hi = hsHiRows(txt, r.path || input.file_path);
    const m = new Map();
    if (hi) idx.forEach((i, k) => m.set(i, hi[k]));
    return m;
  };
  const hiNew = hiSide(["ctx", "add"]);
  const hiOld = hiSide(["ctx", "del"]);
  const cell = (x, i) => (x.kind === "del" ? hiOld.get(i) : hiNew.get(i)) || hsEsc(x.text);
  // 折叠记号也走两列:它代表被略去的那几行,落在正文列里才读得出「这里少了一段」,
  // 顶在行首会跟增删记号排成一列,像是又一种改动。
  const body = shown.map((r, i) => r.kind === "file"
    ? `<div class="hs-dfile">${hsEsc(r.text)}</div>`
    : r.kind === "gap"
    ? `<div class="hs-dl gap" data-skipped="${r.skipped || 0}"><span class="hs-dn"></span><span class="hs-dm"> </span>`
      + `<span class="hs-dt">${hsEsc(r.text || "⋯")}</span></div>`
    : `<div class="hs-dl ${r.kind}"><span class="hs-dn"></span>`
      + `<span class="hs-dm">${mark[r.kind] || " "}</span>`
      + `<span class="hs-dt">${cell(r, i)}</span></div>`).join("");
  const more = rows.length > shown.length
    ? `<div class="hs-dmore">${hsEsc(DeskI18n.t("tool.moreLines", { count: rows.length - shown.length }))}</div>` : "";
  return `<div class="hs-diff">${body}${more}</div>`;
}

// 行号。新写的整篇从 1 数起；Pi edit 的成功结果带真实首个改动行，可同步编号。
// 旧记录或其他编辑形状没有位置时，才回文件里按替换后文本定位。找不到(文件后来又变了、
// 或者不在已登记的仓里)就留空，不编一个。删除行沿用当前号码但不递增。
async function hsDiffNumber(part) {
  const el = part.el && part.el.querySelector(".hs-diff");
  if (!el || el.dataset.numbered) return;
  el.dataset.numbered = "1";
  const rows = [...el.querySelectorAll(".hs-dl")];
  if (!rows.length) return;
  // 统一补丁自带号码:@@ -371,4 +414,13 @@ 里加号那一侧就是新文件的起始行。
  // 有它就不必回文件里找——那条路依赖的是 claude 的 Edit 入参形状(file_path + new_string),
  // 而给补丁的引擎(codex 的 patch,以及任何走 diff 的)拿不出 new_string,于是号码整片留空。
  if (rows.some((r) => r.classList.contains("meta"))) {
    let n = 0;
    for (const r of rows) {
      if (r.classList.contains("gap")) {
        n += Number(r.dataset.skipped) || 0;
        continue;
      }
      const slot = r.querySelector(".hs-dn");
      if (r.classList.contains("meta")) {
        const m = (r.textContent || "").match(/\+(\d+)/);
        if (m) n = parseInt(m[1], 10);
        if (slot) slot.textContent = "";
        continue;
      }
      if (!slot) continue;
      // 删除行沿用当前号码但不递增:它在新文件里不存在
      if (r.classList.contains("del")) { slot.textContent = n; continue; }
      slot.textContent = n;
      n++;
    }
    return;
  }
  const tool = String(part.tool || "").toLowerCase();
  const state = part.state || {};
  const inp = state.input || {};
  const edits = Array.isArray(inp.edits) ? inp.edits : [];
  let start = 1;
  // Pi edit 的工具结果自带第一条真实改动行。卡片可能还展示它前面的上下文行，
  // 因此减去第一条 add/del 之前在新文件里实际存在的行；全程同步，不再等待文件请求。
  // 多段离散编辑只有第一处的位置，无法据此推断后续段落，仍走下面的兼容定位。
  const changed = Number(state.line_start);
  if (tool === "edit" && edits.length <= 1 && Number.isInteger(changed) && changed > 0) {
    let before = 0;
    for (const r of rows) {
      if (r.classList.contains("add") || r.classList.contains("del")) break;
      if (r.classList.contains("gap")) {
        const skipped = Number(r.dataset.skipped);
        if (Number.isInteger(skipped) && skipped > 0) before += skipped;
      } else if (!r.classList.contains("meta")) before++;
    }
    start = Math.max(1, changed - before);
  } else if (tool !== "write") {
    const path = inp.file_path || inp.path || inp.filePath || "";
    const firstEdit = inp.edits && inp.edits[0];
    const probe = String(inp.new_string || inp.newText
                         || (firstEdit && (firstEdit.new_string || firstEdit.newText)) || "");
    if (!path || !probe) return;
    const loc = await hsResolveFile(path, part.el).catch(() => null);
    if (!loc) return;
    let txt = "";
    try {
      txt = await api(`/workbench/fs?root=${encodeURIComponent(loc.root)}&path=${encodeURIComponent(loc.rel)}&file=1`,
                      { timeout: 15000 });
    } catch { return; }
    const all = String(txt);
    let at = all.indexOf(probe);
    if (at < 0) {
      // 整段对不上多半是这之后文件又改过。退一步按首个有内容的行找,它通常还在原处。
      const anchor = probe.split("\n").find((x) => x.trim().length > 3);
      at = anchor ? all.indexOf(anchor) : -1;
    }
    if (at < 0) return;
    start = all.slice(0, at).split("\n").length;
  }
  let n = start;
  for (const r of rows) {
    if (r.classList.contains("gap")) {
      n += Number(r.dataset.skipped) || 0;
      continue;
    }
    const slot = r.querySelector(".hs-dn");
    if (r.classList.contains("del")) { slot.textContent = n; continue; }
    slot.textContent = n;
    n++;
  }
}
