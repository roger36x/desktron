// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 4/13 片
// 一步工具的展开内容。bash 用终端形制(# 描述 / $ 命令 / 输出),其余给输入与输出两段。
// 后台任务或子代理交回的那份:退出码、耗时、输出文件,再是报告正文。挂在派出它的
// 那一步的展开区末尾——这一步从派出到收场的全部记录都在同一个盒子里。
// 暂判的那一份先不画。通知与这一步自己的结果谁先到并不固定,前台 Bash 实见通知先到:
// 此刻后端只能按「还没有自己的结果」暂记一笔(returned.pending),等结果落地再复核。
// 界面若照着暂判画,一条同步命令在整个执行期间都顶着紫色的「已返回」,复核撤回后才变回去。
function hsRet(part) {
  const r = part && part.returned;
  return r && !r.pending ? r : null;
}

function hsReturnDetail(part) {
  const r = hsRet(part);
  if (!r) return "";
  const bits = [DeskI18n.t(r.status === "error" ? "ret.failed" : r.status === "cancelled" ? "ret.cancelled" : "ret.returned")];
  if (r.exit_code != null) bits.push(DeskI18n.t("ret.exit", { code: r.exit_code }));
  if (r.duration_ms > 0) bits.push(hsDur(r.duration_ms));
  const file = r.output_file
    ? `<div class="hs-tt-rf">${hsEsc(DeskI18n.t("ret.outputFile", { file: String(r.output_file) }))}</div>` : "";
  const text = String(r.text || "");
  const body = text ? `<div class="hs-tt-o">${hsClip(text, 5000)}</div>` : "";
  return `<div class="hs-tt-ret"><div class="hs-tt-rh">${hsEsc(bits.join(" · "))}</div>${file}${body}</div>`;
}

function hsStepDetail(part) {
  const ret = hsReturnDetail(part);
  const base = hsStepDetailBase(part);
  const failure = hsToolFailureView(part.state || {}).detail;
  const extra = (failure ? `<div class="hs-tt-o">${hsClip(failure, 5000)}</div>` : "") + ret;
  if (!extra) return base;
  // 展开区已有内容就把返回段接在盒子里;没有(入参还没流完)就单独给返回一个盒子
  return base ? base.replace(/<\/div>\s*$/, extra + "</div>") : `<div class="hs-term">${extra}</div>`;
}

function hsStepDetailBase(part) {
  const st = part.state || {};
  const input = st.input || {};
  const out = st.output ? String(st.output) : "";
  if (String(part.tool || "").toLowerCase() === "bash") {
    const summary = hsToolDescription(input);
    const desc = summary ? `<div class="hs-tt-d"># ${hsEsc(summary.slice(0, 200))}</div>` : "";
    // 命令没记下来就整行不出:一个光秃秃的 $ 什么也没说,补一句解释也只是换个东西占版面。
    const raw = hsCommandView(input.command, summary);
    // 命令原样保留换行:脚本是按行写的,挤成一段就读不出结构了(heredoc 尤其明显)。
    // 提示符单独占一列,折行的部分跟着正文缩进对齐,不会回到行首和 $ 混在一起。
    const lines = raw.split("\n");
    const keep = lines.slice(0, 40);
    const cmd = raw
      ? '<div class="hs-tt-c"><span class="hs-dm">$</span><span class="hs-dt">'
        + hsShHi(keep.join("\n"))
        + (lines.length > keep.length ? "\n" + DeskI18n.t("tool.moreLines", { count: lines.length - keep.length }) : "")
        + "</span></div>"
      : "";
    if (!desc && !cmd && !out) return "";   // 跑动中入参还没流完:此刻展开只有一个空盒子
    return `<div class="hs-term">${desc}${cmd}${out ? `<div class="hs-tt-o">${hsClip(out, 5000)}</div>` : ""}</div>`;
  }
  const diff = hsDiffDetail(part.tool, input);
  if (diff) return diff;
  const read = hsReadDetail(part.tool, input, out);
  if (read) return read;
  // 其余工具:和执行命令那一档同一个盒子,不再分「输入」「输出」两段各自带标题。
  // 标题本身不带信息——上下已经是入参与结果,再各压一行小字只是把盒子撑高。
  // 入参里凡有 description / command 的,按命令那套排(# 说明 / $ 命令),
  // 剩下的字段走键值两列。不认识的工具也就落进一个眼熟的形制里。
  const gsummary = hsToolDescription(input);
  const gd = gsummary
    ? `<div class="hs-tt-d"># ${hsEsc(gsummary.slice(0, 200))}</div>` : "";
  const gcRaw = hsCommandView(input.command, gsummary);
  const gc = gcRaw
    ? '<div class="hs-tt-c"><span class="hs-dm">$</span><span class="hs-dt">'
      + hsShHi(gcRaw.split("\n").slice(0, 40).join("\n")) + "</span></div>"
    : "";
  const rest = {};
  for (const [k, val] of Object.entries(input)) {
    if (k === "description" || k === "command") continue;
    rest[k] = val;
  }
  const gkv = Object.keys(rest).length ? hsKvRows(rest) : "";
  const go = out ? `<div class="hs-tt-o">${hsClip(out, 5000)}</div>` : "";
  if (!gd && !gc && !gkv && !go) return "";
  return `<div class="hs-term">${gd}${gc}${gkv}${go}</div>`;
}

// 缩略图的两件事:取不到就把 img 摘掉(留一个碎图图标比不画更糟),取到了接上点开看原尺寸。
// 不写成内联 onerror:那段 HTML 是拼字符串插进去的,处理器写在里面既难读,也把行为埋进了标记。
function hsReadPic(part) {
  const img = part.el && part.el.querySelector(".hs-rd-img");
  if (!img || img._wired) return;
  img._wired = true;
  img.onerror = () => { img.closest(".hs-rd-pic")?.classList.add("noimg"); img.remove(); };
  img.onclick = () => {
    const p = (part.state || {}).input || {};
    hsOpenFile(String(p.file_path || p.notebook_path || ""), "", img);
  };
}

// 读取:引擎给的输出本来就是「行号 制表符 正文」,原样塞进 pre 靠制表符对齐,
// 一遇到长行折行就整片错位,而上面还压着一段 JSON 入参。拆成行号列与正文列,
// 和改动的呈现同一套形制;文件路径单独一行,入参里其余的字段行号已经说了。
const HS_READ_MAX = 60;
const HS_IMG_EXT = /\.(png|jpe?g|gif|webp|svg|bmp|ico)$/i;

function hsReadDetail(tool, input, out) {
  const t = String(tool || "").toLowerCase();
  if (t !== "read" && t !== "notebookread") return "";
  const path = String(input.file_path || input.notebook_path || "");
  const head = path
    ? `<div class="hs-rd-h">${hsEsc(path.split("/").slice(-3).join("/"))}</div>` : "";
  // 读图片:工具结果里没有「行号 制表符 正文」那种文本,按文本铺只会得到一个只剩路径的空框。
  // 这一步的内容本来就是视觉的,路径名答不了「看到了什么」,所以把图摆出来。
  // /workbench/view?raw=1 已经按原始字节直出、带正确的 content-type,不必另开接口。
  // 取不到(路径在允许根之外,比如 /tmp)时把 img 摘掉,退回只有路径的形态,不留碎图图标。
  if (path && HS_IMG_EXT.test(path)) {
    const src = "/workbench/view?raw=1&path=" + encodeURIComponent(path);
    return `<div class="hs-diff hs-read hs-rd-pic">${head}`
      + `<img class="hs-rd-img" src="${hsEsc(src)}" alt="" loading="lazy"></div>`;
  }
  if (!out) return head ? `<div class="hs-diff hs-read">${head}</div>` : "";
  const lines = String(out).replace(/\n+$/, "").split("\n");
  const shown = lines.slice(0, HS_READ_MAX);
  // 行号与正文先拆开,再把正文整篇送去高亮:带着行号高亮,行首那串数字会被当成代码的一部分。
  const cells = shown.map((ln) => {
    const m = ln.match(/^\s*(\d+)\t([\s\S]*)$/);
    return { n: m ? m[1] : "", text: m ? m[2] : ln };
  });
  // 一行行号都没有的,就不是文件内容,而是引擎的一句交代(读图片给的是
  // 「Image read successfully」)。仍旧铺进带行号的代码栅格,那句话会被空的行号列
  // 推出一段缩进,一句话占着一个代码块的排面。改成一行淡字。
  if (cells.every((c) => !c.n)) {
    const note = shown.map((s) => hsEsc(s)).join(" ").trim();
    return note ? `<div class="hs-diff hs-read">${head}<div class="hs-rd-note">${note}</div></div>`
                : (head ? `<div class="hs-diff hs-read">${head}</div>` : "");
  }
  const hi = hsHiRows(cells.map((c) => c.text), path);
  const body = cells.map((c, i) => '<div class="hs-dl ctx"><span class="hs-dn">' + c.n
    + '</span><span class="hs-dt">' + ((hi && hi[i]) || hsEsc(c.text)) + "</span></div>").join("");
  const more = lines.length > shown.length
    ? `<div class="hs-dmore">${hsEsc(DeskI18n.t("tool.moreLines", { count: lines.length - shown.length }))}</div>` : "";
  return `<div class="hs-diff hs-read">${head}${body}${more}</div>`;
}

// 其余工具的入参:一行一个字段,键在左、值在右。原来是缩进过的 JSON——括号、引号、逗号
// 占了一半篇幅,而这里要看的只是「哪个文件、什么模式」这几个值。
function hsKvRows(obj) {
  return Object.entries(obj).map(([k, val]) => {
    let s = typeof val === "string" ? val : JSON.stringify(val);
    s = s == null ? "" : String(s);
    if (s.length > 400) s = s.slice(0, 400) + "…";
    return `<div class="hs-dl"><span class="hs-kk">${hsEsc(k)}</span>`
      + `<span class="hs-dt">${hsEsc(s)}</span></div>`;
  }).join("");
}

// 思考与阶段性说明、工具按发生顺序平铺在过程区。只有外层过程区负责折叠，
// 思考不再单独折一层；隐藏正文不解析、不保留 DOM，展开后沿用增量排版。
function hsProcHost(t) {
  return hsProcBox(t).querySelector(".hs-proc-body");
}

// 把非正文事件放回这一轮的过程时间线。at 是 t.parts 中「下一块」的下标：卡片会落在
// 该位置之后实际画出的第一块过程节点前；后面的块尚未出现时先接到末尾，实时事件随后
// append 即自然保持顺序。审批账本与引擎权限请求共用这条路，避免各自在正文末尾堆一排。
function hsProcPlace(t, el, at) {
  const body = hsProcHost(t);
  const parts = [...t.parts.values()];
  const start = Math.max(0, Math.min(Number.isFinite(at) ? at : parts.length, parts.length));
  const prev = parts.slice(0, start).reverse().find((p) => p !== el._procPart
    && p.el && p.el.parentNode === body);
  const next = parts.slice(start).find((p) => p !== el._procPart
    && p.el && p.el.parentNode === body);
  const ref = next ? next.el : null;
  const nodes = [...body.children];
  const here = nodes.indexOf(el);
  const afterPrev = !prev || here > nodes.indexOf(prev.el);
  const beforeNext = !next || (here >= 0 && here < nodes.indexOf(next.el));
  // 同一个锚点可以连续产生多张审批卡。只要仍位于前后 part 之间就保持原位；强求
  // nextSibling === ref 会让这几张卡在每轮账本同步时互相交换位置并触发滚动跳变。
  if (el.parentNode !== body || !afterPrev || !beforeNext) body.insertBefore(el, ref);
  hsProcBox(t).classList.add("has-steps");
  return body;
}

function hsProcPlacePart(part, el) {
  const parts = [...part.turn.parts.values()];
  const i = parts.indexOf(part);
  el._procPart = part;
  return hsProcPlace(part.turn, el, i < 0 ? parts.length : i + 1);
}

// 待人处理的卡片若仍藏在折叠过程里就等于没有入口。只在 pending 到达时主动展开；
// 处理完成后保留用户刚才所在的展开态，不再替用户折回去。
function hsProcReveal(t) {
  const box = hsProcBox(t);
  box.classList.add("has-steps", "open");
  hsProcOpenSave(t, true);
}

// 引子行:模型动手之前写的那一句「Now the frontend removals:」。它引出的是紧接着的
// 工具调用,而工具调用收在「处理中」里,引子留在正文就成了一句悬在半空的话——冒号之后
// 什么都没有。把它挪进过程区,排在它引出的那一步前面。
// 判据收得紧:短、以冒号收尾、后面紧跟工具调用,三者同时成立才动它。少挪好过错挪,
// 挪错等于把一句正文从正文里拿走。
const HS_LEAD_MAX = 120;

function hsIsLead(p) {
  if (!p || p.type !== "text") return false;
  const t = (p.text || "").trim();
  return !!t && t.length <= HS_LEAD_MAX && /[:：]$/.test(t) && !/\n/.test(t);
}

function hsLeadIn(tool) {
  const list = [...tool.turn.parts.values()];
  const prev = list[list.indexOf(tool) - 1];
  if (!hsIsLead(prev) || !prev.el || prev.lead) return;
  prev.lead = true;
  prev.el.classList.add("hs-lead");
  hsProcHost(tool.turn).appendChild(prev.el);   // 排在这一步之前:紧接着就轮到工具行
}

// 折叠的步骤只画摘要。Codex 一轮里常有上百个 commandExecution；若建页时把每一步的
// 命令、输出、diff 与语法高亮都提前塞进隐藏 DOM，切会话的主线程时间几乎全耗在看不见
// 的内容上。详情在第一次展开时才构建；已经展开的步骤收到状态更新时仍立即刷新。
// 列表里工具输出是截断的(后端 LIST_OUTPUT_CAP;codex 的会话里这些输出占返回体的 99%,
// 而它们默认折叠着)。展开这一刻才把全文取回来,取到就地补进 part,后续展开不再请求。
// 思考正文在列表里也是截断的(textMore 标全长,见 cli_store.LIST_REASON_CAP)。展开那一刻取回。
function hsThinkFull(v, part) {
  if (!part || !part.textMore || part._fullPending || part._fullAttempted) return;
  // 一次展开最多自动请求一次。失败由明确的重试入口处理，不能随每帧文字反复打接口。
  part._fullPending = true;
  part._fullAttempted = true;
  const more = part.el?.querySelector(".hs-think-more");
  if (more) { more.hidden = false; more.disabled = true; more.textContent = DeskI18n.t("proc.loadingFull"); }
  const preview = part.text;
  api(`/session/${encodeURIComponent(v.sid)}/part/${encodeURIComponent(part.id)}`, { timeout: 20000 })
    .then((got) => {
      // 等待期间的实时文字或类型迁移优先；旧全文响应不能覆盖新进展。
      if (!part.el || part.type !== "reasoning") return;
      if (!got || typeof got.text !== "string" || part.text !== preview || part.buf) {
        // 下一帧才刷新的 delta 也算新进展，否则全文若已包含它，刷帧时会重复追加。
        throw new Error("stale preview");
      }
      // 历史过程先以截断预览落地，全文可能在首屏已经贴底后才返回。正文容器关闭了
      // 原生滚动锚定，若在事务外直接重绘，旧过程每长高一块，手机视口就会向上漂一段。
      // 数据切换、按钮收起与正文重绘必须作为同一次布局变化提交：贴底态精确回到底部，
      // 阅读态则由统一语义锚点守住当前位置，不依赖 WebKit 的 ResizeObserver 时序。
      hsViewportChange(v, () => {
        part.text = got.text;
        delete part.textMore;
        if (more) more.hidden = true;
        hsResetNarrativeCommit(part);
        hsPaint(v, part);   // 已收起则只更新数据，不重建隐藏正文
      }, false, "reasoning-full");
    })
    .catch(() => {
      if (more) { more.disabled = false; more.textContent = DeskI18n.t("proc.partial"); }
    })
    .finally(() => { part._fullPending = false; });
}

function hsStepFull(v, part) {
  const st = (part && part.state) || {};
  // 入参也可能是截断的:2026-08-25 拆一段真实会话发现,工具那一类里入参比输出还大
  // (41% 对 22%)——长命令、heredoc、整段内联脚本都在入参里。两者任一被截都要取全块。
  if ((!st.outputMore && !st.inputMore && !part.returnedMore) || part._fullPending) return;
  part._fullPending = true;
  api(`/session/${encodeURIComponent(v.sid)}/part/${encodeURIComponent(part.id)}`, { timeout: 20000 })
    .then((got) => {
      const gs = (got || {}).state || {};
      const full = gs.output;
      // 判据用「有没有取回这一块」,不能只看 output 是不是字符串:入参被截而输出为空的
      // 工具(写文件、派子代理)那一路会在这里返回,全文补不回来。
      if (!got || !part.el) return;
      part.state = { ...st };
      if (typeof full === "string") { part.state.output = full; delete part.state.outputMore; }
      if (gs.input && typeof gs.input === "object") {
        part.state.input = gs.input; delete part.state.inputMore;
      }
      if (got.returned && typeof got.returned === "object") {
        part.returned = got.returned; delete part.returnedMore;
      }
      const box = part.el.querySelector(".hs-step-d");
      if (box) {                       // 还开着才重画:人可能已经收起来了
        box.innerHTML = hsStepDetail(part) || box.innerHTML;
        hsDiffNumber(part); hsReadPic(part);
      }
    })
    .catch(() => {})
    .then(() => { part._fullPending = false; });
}

function hsStepDetailMount(part, v) {
  if (!part.el) return false;
  if (part.el.querySelector(".hs-step-d")) return true;
  if (v) hsStepFull(v, part);
  const detail = hsStepDetail(part);
  if (!detail) return false;
  const el = document.createElement("div");
  el.className = "hs-step-d";
  el.innerHTML = detail;
  part.el.appendChild(el);
  hsDiffNumber(part);   // 这两项都只服务展开区，不再为折叠步骤做文件读取与节点遍历
  hsReadPic(part);
  return true;
}

// host 只给子代理只读页使用:工具行仍走主会话同一套摘要与详情,但不需要回合的过程盒、
// 引子和审阅侧栏。此前子代理另画一行纯文本,完整的 input/output 明明已经从后端回来,
// 却在这里被降成了「已执行 某件事」,也就无从展开。
// 工具行的重建签名:状态、结果长度、入参长度任一变了才整行重建。hsPaintLive 用它判断
// 这次会不会真的重建,只有会重建才开布局事务(事务要量气泡,不必每次工具更新都付)。
// 「已返回」这一行怎么写。子代理与后台命令都用同一个词:这一步的动词讲的是「它回来了」。
function hsReturnVerb(r) {
  if (r.status === "error") return DeskI18n.t("ret.returnFailed");
  if (r.status === "cancelled") return DeskI18n.t("ret.cancelled");
  return DeskI18n.t("ret.returned");
}
function hsReturnStat(r) {
  const bits = [];
  if (r.exit_code != null) bits.push(DeskI18n.t("ret.exit", { code: r.exit_code }));
  if (r.duration_ms > 0) bits.push(hsDur(r.duration_ms));
  return bits.join(" · ");
}

// 只精简工具包装器的通用失败提示，具体报错仍保留，避免丢失排错信息。
function hsToolFailureExit(st) {
  if (st.status !== "error") return "";
  if (Number.isInteger(st.failure?.exit_code)) return DeskI18n.t("ret.exitParen", { code: st.failure.exit_code });
  const m = String(st.failure?.message || "").trim().match(/^工具执行失败\s*[（(]退出码\s+(-?\d+)[）)]$/);
  return m ? DeskI18n.t("ret.exitParen", { code: m[1] }) : "";
}

// 旧记录也可能把输出前 500 字复制成 failure.message。重复内容留在输出区；
// 独立的短报错仍可见，长报错收进详情，不能因为折叠就丢失排错信息。
function hsToolFailureView(st) {
  const message = st.status === "error" ? String(st.failure?.message || "").trim() : "";
  if (!message || /^工具执行失败(?:\s*[（(]退出码\s+-?\d+[）)])?$/.test(message)) return { summary: "", detail: "" };
  const normalize = (s) => s.replace(/\\[nrt]/g, " ").replace(/\s+/g, " ").trim();
  const repeated = normalize(String(st.output || "")).includes(normalize(message));
  if (repeated) return { summary: "", detail: "" };
  return message.length <= 160 && !/[\r\n]/.test(message)
    ? { summary: message, detail: "" } : { summary: "", detail: message };
}

function hsToolSig(part) {
  const st = part.state || {};
  const r = hsRet(part);
  return `${st.status}|${(st.output || "").length}|${JSON.stringify(st.input || {}).length}|${st.progressRevision || 0}`
    + `|failure:${st.failure?.message || ""}:${st.failure?.exit_code ?? ""}`
    + (r ? `|ret:${r.status || ""}:${(r.text || "").length}:${r.exit_code == null ? "" : r.exit_code}` : "");
}

function hsPaintTool(v, part, host) {
  const target = host || hsProcHost(part.turn);
  if (!part.el) {
    part.el = document.createElement("div");
    part.el.className = "hs-step";
    if (!host) hsLeadIn(part);
    target.appendChild(part.el);
  } else if (!target.contains(part.el)) {
    target.appendChild(part.el);   // 同正文那一支:不在当前容器里就接回来
  }
  const st = part.state || {};
  // 签名去重:同一 part 的 updated 会反复来,内容没变就不重建 DOM(重建会打断展开态与文本选中)
  const sig = hsToolSig(part);
  if (part.sig !== sig) {
    part.sig = sig;
    const wasOpen = part.el.classList.contains("open");
    // 取证:只在真的重建这一步的 DOM 时记(签名没变不重建,也就不改高度);不记参数
    globalThis.hsVpdNote?.(v, "tool", { st: String(st.status || ""), open: wasOpen ? 1 : 0, sub: host ? 1 : 0 });
    part.el.innerHTML =
      // 不给每行挂箭头:整行可点已由悬停变色表达,右侧一列箭头是重复的装饰,
      // 而且这份清单本身就在「已处理 N 步」的折叠里,再多一层展开记号读起来是两级套两级。
      '<div class="hs-step-h"><span class="hs-step-n"></span><span class="hs-step-a"></span></div>';
    // 跑动中末尾缀三个点:这一步还没有结果,而它和已经跑完的那些排在同一列里,
    // 光靠动词的「中」字分辨要一个字一个字读。点缀在行末,扫过去就分得开。
    const run = st.status === "running";
    const nEl = part.el.querySelector(".hs-step-n");
    const obj = hsToolObj(part);
    const ae = part.el.querySelector(".hs-step-a");
    // 「已写入」分两种:新建了一个文件,还是覆盖了一个已有的。引擎的结果原话里写着
    // created / updated,据它区分;分不出时保持「已写入」。
    const made = String(part.tool || "").toLowerCase() === "write" && st.status === "done"
                 && /created successfully/i.test(String(st.output || ""));
    const ret = hsRet(part);
    hsSetText(nEl, ret ? hsReturnVerb(ret)
                       : (made ? DeskI18n.t("tool.created") : hsToolOutcome(part, run)) + (run && !obj.t ? "…" : ""));
    hsToolTargetPaint(ae, obj, run);
    const failureExit = hsToolFailureExit(st);
    let failureEl = part.el.querySelector(".hs-step-failure");
    const failureText = hsToolFailureView(st).summary;
    if (failureText && !failureEl) {
      failureEl = document.createElement("div"); failureEl.className = "hs-step-failure";
      part.el.appendChild(failureEl);
    }
    if (failureEl) { failureEl.textContent = failureText; failureEl.hidden = !failureText; }

    // 后台任务交回:行末写退出码与耗时,与增删统计同一档的附注。派出时那句「后台运行中」
    // 的回执留在展开区里,这一行改讲结果。
    let re_ = part.el.querySelector(".hs-step-ret");
    if ((ret || failureExit) && !re_) {
      re_ = document.createElement("span");
      re_.className = "hs-step-ret";
      part.el.querySelector(".hs-step-h").appendChild(re_);
    }
    if (re_) hsSetText(re_, ret ? hsReturnStat(ret) : failureExit);
    part.el.classList.toggle("ret", !!ret);
    const stat = run ? null : hsDiffStat(part.tool, st.input || {});
    let se = part.el.querySelector(".hs-step-c");
    if (stat && !se) {
      se = document.createElement("span");
      se.className = "hs-step-c";
      se.innerHTML = '<span class="hs-cadd"></span><span class="hs-cdel"></span>';
      part.el.querySelector(".hs-step-h").appendChild(se);
    }
    if (se) {
      se.querySelector(".hs-cadd").textContent = stat && stat.add ? "+" + stat.add : "";
      se.querySelector(".hs-cdel").textContent = stat && stat.del ? "-" + stat.del : "";
    }
    if (!host && hsRvIsEdit(part)) {   // 编辑类的步骤给一个入口:点开侧栏看这一轮改了什么
      // 这里只要知道「点开后先落在哪个文件」,直接从入参取路径。不调 hsRvPartFiles:
      // 那要跑一遍逐行 diff,而每次状态刷新都会重画这一行,长会话上白付一次算力。
      const inp0 = (st.input || {});
      const ch0 = Array.isArray(inp0.changes) ? inp0.changes.find((c) => c && c.file_path) : null;
      // 旧 Pi 历史仍可能保留原生 path；适配层升级前后的消息都应有同一个审阅入口。
      const first = { path: (ch0 && ch0.file_path) || inp0.file_path || inp0.path || inp0.filePath || "" };
      if (first.path) {
        let rb = part.el.querySelector(".hs-step-r");
        if (!rb) {
          rb = document.createElement("button");
          rb.type = "button";
          rb.className = "hs-step-r";
          rb.textContent = DeskI18n.t("proc.review");
          part.el.querySelector(".hs-step-h").appendChild(rb);
        }
        rb._turn = part.turn;
        rb._path = first.path || "";
        rb.onclick = (ev) => {   // 不能冒泡到行头:那一层是展开/收起
          ev.stopPropagation();
          hsRvOpen(v, part.turn, rb._path);
        };
      }
    }
    part.el.querySelector(".hs-step-h").onclick = () => {
      if (part.el.classList.contains("open")) {
        part.el.classList.remove("open");
        part.el.querySelector(".hs-step-d")?.remove();
        part.sig = hsToolSig(part);
        return;
      }
      if (hsStepDetailMount(part, v)) {
        part.el.classList.add("open");
        part.sig = hsToolSig(part);
      }
    };
    if (wasOpen && hsStepDetailMount(part, v)) part.el.classList.add("open");
  }
  part.el.classList.toggle("run", st.status === "running");
  part.el.classList.toggle("err", st.status === "error");
  if (host) return;
  const box = hsProcBox(part.turn);
  // 当前这次调用本身就是一步，已经足够证明盒子有内容。旧实现每加一步都 querySelectorAll
  // 整个过程区，N 个步骤会做 1+2+…+N 次节点扫描；Codex 的密集命令轮最受影响。
  box.classList.add("has-steps");
}

// 流式期间的轻排版:只处理段落与行内码,不碰标题、列表、强调。
// 故意不处理未闭合的反引号——半截语法在下一帧就会补全,提前渲染反而闪。
function hsLiteHtml(text) {
  const esc = (x) => x.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  return String(text).split(/\n{2,}/).map((para) => {
    const body = esc(para).replace(/`([^`\n]+)`/g, "<code>$1</code>").replace(/\n/g, "<br>");
    // 轻排版的尾部也标记可见汉字，避免下一次完整Markdown渲染时段距跳变。
    const han = /\p{Script=Han}/u.test(para) ? ' data-markdown-han-text="true"' : "";
    return "<p" + han + ">" + body + "</p>";
  }).join("");
}

// 流式期间的排版:已经写定的部分走完整 markdown,正在打的最后一段保持素排。
// 原来整段都只做轻量排版,等收尾才渲染一次——输出一长,那段「标题是井号、列表是星号」
// 的时间就很长。整段实时渲染又不行:半截的 ** 或没闭合的围栏会让画面一直抖。
// 切点取最后一个换行,并保证围栏成对——block 在换行处即完整,标题、列表、段落边打边定型,不必等空行。
const HS_MD_MS = 260;   // 重渲染的最小间隔:markdown-it 解析几 KB 只要毫秒级,但没必要每帧都跑

function hsSafeCut(t) {
  const fence = (t.match(/^```/gm) || []).length;
  let end = t.length;
  if (fence % 2) {                       // 围栏没闭合:退到它开始之前
    const at = t.lastIndexOf("\n```");
    if (at < 0) return 0;
    end = at;
  }
  const br = t.lastIndexOf("\n", end);
  return br < 0 ? 0 : br;
}

function hsStreamHtml(part, source) {
  const txt = (source === undefined ? stripFraming(part.text) : String(source)).trim();
  const now = Date.now();
  if (part.mdAt && now - part.mdAt < HS_MD_MS && part.mdCut !== undefined) {
    // 没到重解析的点:定型的那截沿用上一次的结果,只把新打出来的尾巴接上
    return { head: part.mdHtml || "", tail: hsLiteHtml(txt.slice(part.mdCut).trim()) };
  }
  const cut = hsSafeCut(txt);
  part.mdAt = now;
  part.mdCut = cut;
  part.mdHtml = cut > 0 ? md.render(txt.slice(0, cut)).trim() : "";
  return { head: part.mdHtml, tail: hsLiteHtml(txt.slice(cut).trim()) };
}

// 只留稳定前缀两端的有界指纹，不复制整段正文。stripFraming 可能在闭合标签到达后改写
// 旧尾巴；尾部采样能识别这种“原始 delta 追加、规范化文本却不是追加”的情况。
function hsNarrativeStamp(txt, len = txt.length) {
  const n = Math.max(0, Math.min(Number(len) || 0, txt.length));
  return `${n}:${txt.slice(0, Math.min(96, n))}:${txt.slice(Math.max(0, n - 96), n)}`;
}

// 工具边界把当时稳定的 markdown 变成一个不可变片段。同一 reasoning/text id 恢复输出时，
// 只排新增后缀；不再把几十万字的稳定前缀重新交给 markdown-it。若 Provider 回写了旧正文，
// hsMergePart 会撤销缓存并安全退回一次全量解析。片段边界与工具边界一致，也避免半截活动尾巴定稿。
function hsNarrativePieces(part, settled, final = false) {
  const txt = stripFraming(part.text).trim();
  const priorLen = part.mdCommittedLen || 0;
  const priorHtml = part.mdCommittedHtml || "";
  // 工具边界可以切在列表、引用定义等跨块语法中；实时阶段按片段冻结以控制工作量，
  // 引擎真正结束时再做且只做一次全文规范化，保证最终 DOM 与历史回读完全一致。
  if (final) {
    if (part.mdFinalLen === txt.length && priorHtml) {
      return { head: priorHtml, tail: "", base: priorHtml, commit: true };
    }
    const head = txt ? md.render(txt).trim() : "";
    part.mdCommittedLen = txt.length;
    part.mdCommittedStamp = hsNarrativeStamp(txt);
    part.mdCommittedHtml = head;
    part.mdFinalLen = txt.length;
    part.mdHtml = null; part.mdCut = undefined; part.mdAt = 0;
    return { head, tail: "", base: "", commit: true };
  }
  // 文本替换由 hsMergePart 显式撤销 committed 状态；普通 delta 只会追加，因此这里只记长度，
  // 不复制一份可能数十万字的稳定前缀常驻手机内存。
  const canAppend = priorLen > 0 && txt.length >= priorLen
    && part.mdCommittedStamp === hsNarrativeStamp(txt, priorLen);
  if (priorLen > 0 && !canAppend) {
    // 原始消息可能仍是 append，但 stripFraming 等规范化改变了旧前缀。让本次从真实全文
    // 对账，不能把错误切点后的后缀接到旧 DOM 上。
    part.mdCommittedLen = 0; part.mdCommittedStamp = null; part.mdCommittedHtml = null;
    part.streamCommittedNodes = 0; part.streamHeadHtml = part.streamTailHtml = null;
    part.mdHtml = null; part.mdCut = undefined; part.mdAt = 0;
  }
  if (settled) {
    const suffix = canAppend ? txt.slice(priorLen).trim() : txt;
    const rendered = suffix ? md.render(suffix).trim() : "";
    const head = canAppend ? priorHtml + rendered : rendered;
    part.mdCommittedLen = txt.length;
    part.mdCommittedStamp = hsNarrativeStamp(txt);
    part.mdCommittedHtml = head;
    part.mdFinalLen = 0;
    part.mdHtml = null; part.mdCut = undefined; part.mdAt = 0;
    return { head, tail: "", base: canAppend ? priorHtml : "", commit: true };
  }
  if (!canAppend) return hsStreamHtml(part, txt);
  const live = hsStreamHtml(part, txt.slice(priorLen));
  return { head: priorHtml + live.head, tail: live.tail, base: priorHtml, commit: false };
}

function hsStreamNodeSame(a, b) {
  if (!a || !b || a.nodeType !== b.nodeType) return false;
  if (a.nodeType === 1) return a.outerHTML === b.outerHTML;
  return a.nodeValue === b.nodeValue;
}

function hsStreamNodes(html) {
  const tpl = document.createElement("template");
  tpl.innerHTML = html || "";
  return [...tpl.content.childNodes];
}

// 每个流式块只保留一个无盒子的注释作为边界：前面是已经定型的 markdown，后面是仍在
// 打字的尾部。过去每帧给整块 innerHTML 赋值，会把已经显示的标题、列表和代码全部销毁
// 重建；浏览器既要重新布局整段，也会丢掉滚动锚点候选。现在只替换真正变化的后缀。
function hsStreamMark(part, root) {
  if (part.streamRoot === root && part.streamMark && part.streamMark.parentNode === root) {
    return part.streamMark;
  }
  part.streamRoot = root;
  part.streamMark = document.createComment("hs-live-tail");
  root.appendChild(part.streamMark);
  part.streamHeadHtml = null;
  part.streamTailHtml = null;
  part.streamCommittedNodes = 0;
  return part.streamMark;
}

function hsStreamPatchHead(part, root, mark, html, base) {
  if (part.streamHeadHtml === html) return;
  // 分段定稿只会在旧 head 后追加完整 HTML。直接解析并插入新增片段，避免每次把全部历史
  // HTML 再过一遍 template、节点比较与布局；Provider 改写前缀时仍走下面的通用对账。
  if (part.streamHeadHtml && html.startsWith(part.streamHeadHtml)) {
    hsStreamNodes(html.slice(part.streamHeadHtml.length)).forEach((n) => root.insertBefore(n, mark));
    part.streamHeadHtml = html;
    return;
  }
  const all = [];
  for (let n = root.firstChild; n && n !== mark; n = n.nextSibling) all.push(n);
  // 活动后缀的 markdown 可能改写自己的最后一个块，因此新 HTML 不一定是旧 HTML 的字符串
  // 追加；但 committed 前缀是不可变的。只对账它后面的节点，不扫描、更不重建稳定前缀。
  const segmented = !!(base && part.streamHeadHtml?.startsWith(base) && html.startsWith(base));
  const stable = segmented ? Math.min(part.streamCommittedNodes || 0, all.length) : 0;
  const old = segmented ? all.slice(stable) : all;
  const fresh = hsStreamNodes(segmented ? html.slice(base.length) : html);
  let keep = 0;
  while (keep < old.length && keep < fresh.length && hsStreamNodeSame(old[keep], fresh[keep])) keep++;
  old.slice(keep).forEach((n) => n.remove());
  fresh.slice(keep).forEach((n) => root.insertBefore(n, mark));
  part.streamHeadHtml = html;
}

function hsStreamPatchTail(part, root, mark, html) {
  if (part.streamTailHtml === html) return;
  let n = mark.nextSibling;
  while (n) { const next = n.nextSibling; n.remove(); n = next; }
  hsStreamNodes(html).forEach((x) => root.appendChild(x));
  part.streamTailHtml = html;
}

function hsStreamPaint(part, root, pieces) {
  const mark = hsStreamMark(part, root);
  hsStreamPatchHead(part, root, mark, (pieces && pieces.head) || "", (pieces && pieces.base) || "");
  hsStreamPatchTail(part, root, mark, (pieces && pieces.tail) || "");
  if (pieces && pieces.commit) {
    let count = 0;
    for (let n = root.firstChild; n && n !== mark; n = n.nextSibling) count++;
    part.streamCommittedNodes = count;
  }
}

// 来源消息只读展示，不复用用户气泡的编辑、重发或输入历史入口。
function hsPaintExecution(v, part) {
  const source = part.source || {};
  const label = DeskI18n.t({ agent: "exec.agent", script: "exec.script", command: "exec.command" }[source.kind] || "exec.async");
  part.turn.executionTaskId = part.task_id || "";
  if (part.at) part.turn.uat = part.at;
  const status = DeskI18n.t({ done: "exec.done", error: "ret.failed", cancelled: "ret.cancelled" }[part.status] || "ret.returned");
  if (!part.el) {
    part.el = document.createElement("section");
    part.el.className = "hs-execution";
    part.el.innerHTML = '<div class="hs-execution-head"><span class="hs-execution-source"></span>'
      + '<span class="hs-execution-status"></span></div><div class="hs-execution-body"></div>'
      + '<div class="hs-execution-file"></div><button type="button" class="hs-execution-toggle" aria-expanded="false"></button>';
    const toggle = part.el.querySelector("button");
    DeskI18n.bind(toggle, "common.showMore");
    toggle.onclick = () => {
      const open = part.el.classList.toggle("expanded");
      toggle.setAttribute("aria-expanded", String(open));
      DeskI18n.bind(toggle, open ? "common.less" : "common.showMore");
      if (open && part.textMore && !part._fullPending) {
        part._fullPending = true;
        DeskI18n.bind(toggle, "common.reading");
        api(`/session/${encodeURIComponent(v.sid)}/part/${encodeURIComponent(part.id)}`, { timeout: 20000 })
          .then((got) => {
            if (!got || got.type !== "execution-result" || typeof got.text !== "string") throw new Error(DeskI18n.t("exec.notFound"));
            part.text = got.text; delete part.textMore;
            hsPaintExecution(v, part);
            DeskI18n.bind(toggle, part.el.classList.contains("expanded") ? "common.less" : "common.showMore");
          })
          .catch(() => {
            part.el.classList.remove("expanded");
            toggle.setAttribute("aria-expanded", "false");
            DeskI18n.bind(toggle, "exec.readFailed");
          })
          .finally(() => { part._fullPending = false; });
      }
    };
    // 视觉上是新回合的来信，而非助手产出区末尾的另一份工具输出。
    part.turn.el.insertBefore(part.el, part.turn.holder);
  } else if (part.el.parentNode !== part.turn.el) {
    part.turn.el.insertBefore(part.el, part.turn.holder);
  }
  const agentReturn = source.kind === "agent";
  const scriptReturn = source.kind === "script";
  part.el.querySelector(".hs-execution-source").textContent = label + " · " + (source.name || DeskI18n.t("exec.result")) + (scriptReturn ? ":" : "");
  const statusEl = part.el.querySelector(".hs-execution-status");
  statusEl.hidden = scriptReturn;
  statusEl.textContent = scriptReturn ? "" : agentReturn ? status : status
    + (part.exit_code != null ? " · " + DeskI18n.t("ret.exit", { code: part.exit_code }) : "")
    + (part.duration_ms > 0 ? " · " + hsDur(part.duration_ms) : "");
  const body = part.el.querySelector(".hs-execution-body");
  const text = String(part.text || "");
  // 子代理报告沿用正文排版；禁用原始 HTML 和自动加载图片，输出不获得页面执行能力。
  if (part._executionPainted !== text || part._executionKind !== source.kind) {
    part._executionPainted = text; part._executionKind = source.kind;
    const markdown = source.kind === "agent" && typeof window.markdownit === "function";
    body.classList.toggle("md", markdown);
    if (markdown) {
      const renderer = hsPaintExecution.markdown || (hsPaintExecution.markdown =
        mdWithHanParagraphs(window.markdownit({ html: false, linkify: true }).disable("image")));
      body.innerHTML = renderer.render(text || DeskI18n.t("exec.noBody"));
      if (typeof hsLinkify === "function") hsLinkify(body);
    } else body.textContent = text || DeskI18n.t("exec.noBody");
  }
  part.el.querySelector(".hs-execution-file").textContent = part.output_file
    ? DeskI18n.t("ret.outputFileColon", { file: part.output_file }) : "";
  const toggle = part.el.querySelector("button");
  toggle.hidden = !part.textMore && text.length <= 400 && text.split("\n").length <= 6;
  part.el.classList.toggle("short", toggle.hidden);
}

function hsTextAttach(t, el) {
  const foot = t.footEl;
  t.holder.insertBefore(el, foot && foot.parentNode === t.holder ? foot : null);
}

function hsPaint(v, part) {
  if (part.type === "compact") { hsPaintCompact(v, part); return; }
  if (part.type === "approval") { hsPaintApproval(v, part); return; }
  if (part.type === "execution-result") { hsPaintExecution(v, part); return; }
  if (part.type === "tool") { hsPaintTool(v, part); return; }
  // 取证:只记类型、是否收尾、是否新建、长度与上次画到的长度(判整段重排还是尾巴追加)
  globalThis.hsVpdNote?.(v, "paint", { pt: String(part.type || ""), done: part.done ? 1 : 0, nw: part.el ? 0 : 1,
                                len: (part.text || "").length, pl: part.paintedLen || 0 });

  if (part.type === "reasoning") {
    if (!part.el) {
      part.el = document.createElement("div");
      part.el.className = "hs-think";
      part.el.innerHTML = '<div class="hs-think-bar"><span class="hs-think-l"></span>'
        + '<span class="hs-chev">' + HS_ICONS.chev + "</span></div>"
        + '<div class="hs-think-body"></div>'
        + '<button type="button" class="hs-think-more" hidden></button>';
      part.el.querySelector(".hs-think-more").onclick = () => {
        part._fullAttempted = false;
        hsThinkFull(v, part);
      };
      hsProcHost(part.turn).appendChild(part.el);
    } else if (!hsProcHost(part.turn).contains(part.el)) {
      hsProcHost(part.turn).appendChild(part.el);   // 同正文那一支:不在当前容器里就接回来
    }
    const body = part.el.querySelector(".hs-think-body");
    const settled = part.done || part.settled;
    const dur = (part.time && part.time.start && part.time.end) ? hsDur(part.time.end - part.time.start) : "";
    const l = part.el.querySelector(".hs-think-l");
    hsSetText(l, settled ? (dur ? DeskI18n.t("think.doneFor", { dur }) : DeskI18n.t("think.done")) : DeskI18n.t("think.running"));
    l.classList.toggle("done", !!settled);
    if (!part.turn.procEl?.classList.contains("open")) {
      // 以外层过程区为准：CSS 隐藏了内层思考按钮，检查内层 open 会让正文永远不绘制。
      // 收起时释放正文节点和解析缓存；保留权威文字，下一次展开再按需恢复。
      if (part.paintT) { clearTimeout(part.paintT); part.paintT = 0; }
      part.paintAt = 0;
      if (body.firstChild) body.replaceChildren();
      if (part.streamRoot === body) {
        part.streamRoot = part.streamMark = null;
        part.streamHeadHtml = part.streamTailHtml = null;
        part.streamCommittedNodes = 0;
      }
      part.mdHtml = part.mdCommittedHtml = part.mdCommittedStamp = null;
      part.mdCommittedLen = 0; part.mdFinalLen = 0; part.mdCut = undefined; part.mdAt = 0;
      part.paintedLen = (part.text || "").length;
      return;
    }
    // 先裁掉首尾空白:思考正文常以换行收尾,渲染出来是一个空行盒,实测在块底多出 24px,
    // 看着就像思考与下一步之间空了一大截(真正的外部间距只有 6px)。
    // 渲染结果也要裁:markdown-it 的输出以 \n 收尾,那个换行在容器里是一个匿名文本节点,
    // 占掉整整一个行盒(实测 24px),看着就是思考与下一步之间空了一大截。
    //
    // 展开期间边打边排,与正文那一支同一条路(hsStreamHtml:定型的那截走完整 markdown、
    // 正在打的最后一段保持素排)。折叠时上面的分支不构造不可见正文。
    body.classList.add("md");
    hsStreamPaint(part, body, hsNarrativePieces(part, settled, !!part.done));
    part.paintedLen = (part.text || "").length;   // 全量重绘对齐尾巴直写的基线(hsTailNudge)
    hsThinkFull(v, part);   // 历史预览在外层展开时取全，不再依赖隐藏的内层按钮
    return;
  }

  if (!part.el) {
    part.el = document.createElement("div");
    part.el.className = "hs-text";
    // 收尾帧有时先于正文快照到达，操作行已经建好。正文仍 appendChild 会被放到
    // 时间/复制按钮之后，形成“操作行在回复前”的倒序；操作行始终是回合末尾锚点。
    hsTextAttach(part.turn, part.el);
  } else if (part.lead) {
    // 已归过程区的引子:整片重建会按序重画每一块,这里不能再按正文接回去,否则重建一次
    // 就退回正文一次。此刻它后面的工具还没画,接到末尾即是原有顺序。
    if (!hsProcHost(part.turn).contains(part.el)) hsProcHost(part.turn).appendChild(part.el);
  } else if (!part.turn.holder.contains(part.el)) {
    // 节点还在,只是不在当前这个容器里(整片重建把旧的那一片换走了)。接回来,不新建:
    // 已经画好的内容与后续增量都挂在它身上,新建等于把这一段正文丢掉。
    // 判据同 hsProcBox:用 contains 而不是 isConnected——离屏铺页时整棵都不在文档里,
    // 那时不该判成「掉出去了」。重新挂载也要守住操作行在末尾的顺序。
    hsTextAttach(part.turn, part.el);
  }
  // 流式中不做完整 markdown 解析(每秒几十次会卡,半截语法还会闪),但把段落与行内码先按
  // 收尾后的形态排:空行切 <p>、反引号包的切 <code>。这两样占了正文的绝大部分,
  // 提前对齐之后,收尾那一下只剩标题、列表、代码块会重排,跳变小得多。
  if (part.done || part.settled) {
    part.el.classList.add("md");
    hsStreamPaint(part, part.el, hsNarrativePieces(part, true, !!part.done));
    hsLinkify(part.el);
    hsCodePaths(part.el);
  } else {
    part.el.classList.add("md");
    hsStreamPaint(part, part.el, hsNarrativePieces(part, false));
  }
  part.paintedLen = (part.text || "").length;   // 同上:全量重绘之后,尾巴直写从这里接着长
}

function hsDur(ms) {
  if (ms == null || ms < 0) return "";
  return ms < 1000 ? Math.round(ms) + "ms" : (ms / 1000).toFixed(1).replace(/\.0$/, "") + "s";
}

// 工具参数摘要:优先文件路径 / 命令 / 模式,退到整个输入的紧凑串
function hsEllipsis(s, n) {
  s = String(s).replace(/\s+/g, " ").trim();
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// 待办清单:模型每次调 TodoWrite 都会把整张表作为参数传进来,它现在被折进「已处理 N 步」
// 里当成一次普通工具调用。人真正想知道的是「此刻在做哪一项、下一项是什么」,所以把
// 进行中那项与其后第一条待办拎到状态行,和 TUI 的 ✱ 当前任务 / └ Next 是同一份信息。
function hsTodoSync(v, part) {
  if (String(part.tool || "").toLowerCase() !== "todowrite") return;
  const list = ((part.state || {}).input || {}).todos;
  if (!Array.isArray(list)) return;
  const name = (x) => x && (x.activeForm || x.content || "");
  const i = list.findIndex((x) => x && x.status === "in_progress");
  v.todoNow = i >= 0 ? name(list[i]) : "";
  const rest = list.slice(i >= 0 ? i + 1 : 0).find((x) => x && x.status === "pending");
  v.todoNext = rest ? name(rest) : "";
  hsStatusPaint(v);
}

function hsToolArg(part) {
  const inp = (part.state || {}).input || {};
  const k = ["file_path", "filePath", "path", "command", "pattern", "query", "url", "notebook_path"].find((x) => inp[x]);
  if (k) return String(inp[k]);
  const s = JSON.stringify(inp);
  return s === "{}" ? "" : s.slice(0, 120);
}
