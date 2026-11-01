// desktron 自建前端：我们的页面，会话 API 由后端各引擎适配器实现（同源调用）。
// 结构（ui-design §11）：工作区轨（rail）→ 上下文面板（panel）→ 主区（聊天/资产查看）。
// 流式渲染：message.part.delta 字符级增量直接追加到对应 part 的 DOM，完成后 markdown 升级。
const $ = (s) => document.querySelector(s);
// 界面语言见 app-i18n.js 与 app-i18n-<语言>.js；服务端把它们合进同一个 /app-core.js 请求，排在本文件之前。
// 围栏代码块的外壳:顶上一条页眉标着语种,右侧一枚复制钮,正文在下。
// 原来只有一个光秃秃的 pre——语种不写出来,读的人得自己从内容认;要取走内容只能手动划选。
// 页眉与正文同处一块底色,不画分隔线:那条线会把一块内容读成两块。
const CD_COPY_ICON = '<svg viewBox="0 0 32 32" width="14" height="14" data-desk-scattered="09" aria-hidden="true"><g fill="none" stroke="currentColor" stroke-width="2.1" stroke-linecap="round" stroke-linejoin="round"><rect x="10.2" y="10.2" width="17" height="17" rx="3.7"/><path d="M6.6 21.4A3.8 3.8 0 0 1 4.8 18V8.6a3.8 3.8 0 0 1 3.8-3.8H18a3.8 3.8 0 0 1 3.4 1.8"/></g></svg>';
function cdCodeBlock(bodyHtml, lang) {
  return '<div class="cd-code"><div class="cd-code-h">'
    + '<span class="cd-code-lang">' + escapeHtml(lang || "text") + "</span>"
    + '<button class="cd-code-copy" type="button" title="复制">' + CD_COPY_ICON + "</button></div>"
    + '<pre class="hljs"><code>' + bodyHtml + "</code></pre></div>";
}
// Codex 按可见行内文字的 Han script 标记段落，链接地址与图片 alt 不参与。
// 属性只描述内容；条件间距仅由 html.os-windows 的 CSS 启用。
function mdWithHanParagraphs(renderer) {
  const original = renderer.renderer.rules.paragraph_open;
  renderer.renderer.rules.paragraph_open = (tokens, idx, options, env, self) => {
    const inline = tokens[idx + 1];
    const text = inline && inline.type === "inline"
      ? (inline.children || []).filter(t => t.type === "text" || t.type === "code_inline").map(t => t.content).join("")
      : "";
    if (/\p{Script=Han}/u.test(text)) tokens[idx].attrSet("data-markdown-han-text", "true");
    return original ? original(tokens, idx, options, env, self) : self.renderToken(tokens, idx, options);
  };
  return renderer;
}
const md = window.markdownit({ html: false, linkify: true, breaks: false });
mdWithHanParagraphs(md);
// 中日韩友好的强调判定。CommonMark 规定:收尾标记前面若是标点,后面就必须是空白或标点,
// 否则不算收尾。中文里「」《》这类全角标点紧贴汉字是常态,于是 **「已完成」**来自 整串
// 按原文输出,不加粗——英文几乎碰不到,中文写作里每天都撞。markdown-it 是照规范实现的,
// 不是它的 bug,所以在扫描标记的那一步补一层:一侧是全角标点、另一侧是汉字时,放行开合。
// 只放行原本判为不可开合的那一侧,不动其余情形(12 例对照:该修的 5 例修好,
// 英文、纯汉字加粗、嵌套强调、散星号一例未变)。
(function (m) {
  const CJK = /[\u2E80-\u303F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF]/;
  const CJK_PUNCT = /[\u2018\u2019\u201C\u201D\u2014\u2026\u3001-\u303F\uFF01-\uFF0F\uFF1A-\uFF20\uFF3B-\uFF40\uFF5B-\uFF65]/;
  const ASCII_PUNCT = /[!-\/:-@\[-`{-~]/;
  const S = m.inline && m.inline.State;
  if (!S || !S.prototype.scanDelims) return;   // 换了渲染器版本就静默不打,不影响正常渲染
  const orig = S.prototype.scanDelims;
  S.prototype.scanDelims = function (start, canSplitWord) {
    const res = orig.call(this, start, canSplitWord);
    const marker = this.src.charCodeAt(start);
    let pos = start;
    while (pos < this.posMax && this.src.charCodeAt(pos) === marker) pos++;
    const last = start > 0 ? this.src[start - 1] : "";
    const next = pos < this.posMax ? this.src[pos] : "";
    if (!last || !next) return res;
    // 闭合侧放宽两档:
    // 一、前面是全角标点时一律可闭合,不再要求后面也是汉字——「**……快。**GUI」这种
    //     后面接拉丁字母的写法在中文里同样常见,原来的条件把它挡在外面。
    // 二、前面是半角标点、后面紧跟汉字时也放行(「**结论:**后面」)。要求后面是汉字,
    //     所以英文里的 `**a**:` 之类不受影响。
    if (!res.can_close) {
      if (CJK_PUNCT.test(last)) res.can_close = true;
      else if (ASCII_PUNCT.test(last) && CJK.test(next)) res.can_close = true;
    }
    if (!res.can_open) {
      if (CJK_PUNCT.test(next)) res.can_open = true;
      else if (ASCII_PUNCT.test(next) && CJK.test(last)) res.can_open = true;
    }
    return res;
  };
})(md);
// 围栏块直接接管 fence 规则,不走 highlight 选项:markdown-it 只有在 highlight 的返回值以
// `<pre` 开头时才原样采用,否则会再套一层 pre/code。外壳是 div,进不了那道口子,
// 于是外面多一个盒子、里面又一个盒子。
md.renderer.rules.fence = (tokens, idx) => {
  const t = tokens[idx];
  const lang = String(t.info || "").trim().split(/\s+/)[0].toLowerCase();
  let body;
  try {
    body = lang && hljs.getLanguage(lang)
      ? hljs.highlight(t.content, { language: lang }).value
      : md.utils.escapeHtml(t.content);
  } catch { body = md.utils.escapeHtml(t.content); }
  return cdCodeBlock(body, lang) + "\n";
};
// 正文里的图片。markdown-it 本来就解析 `![]()`,但没有任何约束:截图多是 2560 宽的
// retina 图,按原尺寸铺出来会撑破聊天栏(2026-09-10 实测),也点不开看原图。
//
// src 只放行 `/workbench/view` 这一个本机接口,其余(含 http/https/data/file)一律降级成
// 链接文字。放行外链等于让正文能悄悄拉取远端资源——谁渲染了这段话、什么时候看的,对面
// 服务器都知道;正文是模型写的,这个口子不该开。要引用外部图就给链接,由人自己点。
const CD_IMG_SRC_OK = /^\/workbench\/view(\?|$)/;
md.renderer.rules.image = (tokens, idx, opts, env, self) => {
  const t = tokens[idx];
  const src = String(t.attrGet("src") || "");
  const alt = self.renderInlineAsText(t.children || [], opts, env);
  if (!CD_IMG_SRC_OK.test(src)) {
    // 不静默吞掉:看不见图又没有交代,读的人只会以为渲染坏了。给一条可点的链接。
    const safe = md.utils.escapeHtml(src);
    return '<a class="cd-img-x" href="' + safe + '" title="外部图片不内嵌,点开查看">'
      + md.utils.escapeHtml(alt || src) + "</a>";
  }
  t.attrSet("class", "cd-img");
  t.attrSet("loading", "lazy");
  return self.renderToken(tokens, idx, opts);
};
// 点开看原图、取不到时收掉。与过程卡片里的读图(hsReadPic)同一套行为,只是那边拿得到
// 工具入参里的路径,这边得从 src 的 path 参数反解。
document.addEventListener("click", (ev) => {
  const im = ev.target && ev.target.closest ? ev.target.closest("img.cd-img") : null;
  if (!im) return;
  ev.preventDefault();
  let path = "";
  try { path = new URL(im.getAttribute("src") || "", location.origin).searchParams.get("path") || ""; }
  catch { path = ""; }
  if (path && typeof hsOpenFile === "function") hsOpenFile(path, "");
}, true);
// error 不冒泡,只能在捕获阶段接。取不到图时留一个碎图图标比不画更糟,换成一行说明。
document.addEventListener("error", (ev) => {
  const im = ev.target;
  if (!im || im.tagName !== "IMG" || !im.classList || !im.classList.contains("cd-img")) return;
  const note = document.createElement("span");
  note.className = "cd-img-x";
  note.textContent = im.getAttribute("alt") || "图片取不到";
  im.replaceWith(note);
}, true);

// 复制钮:取 pre 里的纯文本,不取高亮后的 HTML。点完把图标换成对勾,一秒半后自己换回来。
document.addEventListener("click", (ev) => {
  const b = ev.target && ev.target.closest ? ev.target.closest(".cd-code-copy") : null;
  if (!b) return;
  ev.preventDefault();
  ev.stopPropagation();
  const box = b.closest(".cd-code");
  const pre = box && box.querySelector("pre");
  if (!pre) return;
  const text = pre.textContent || "";
  try {
    if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(text);
    else {
      const ta = document.createElement("textarea");
      ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
      document.body.appendChild(ta); ta.select();
      document.execCommand("copy"); ta.remove();
    }
  } catch { return; }
  if (b._t) clearTimeout(b._t);
  b.classList.add("ok");
  b.innerHTML = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
    + 'stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="4 12.5 9.5 18 20 6.5"/></svg>';
  b._t = setTimeout(() => { b.classList.remove("ok"); b.innerHTML = CD_COPY_ICON; }, 1500);
}, true);
// 只自动链接带协议的 URL:否则 linkify 会把正文里的文件名(how.md、output.md——.md 是摩尔多瓦 TLD)
// 当域名误链成 http://how.md,点开弹空白悬浮框。写全的 https:// 链接不受影响。
md.linkify.set({ fuzzyLink: false });
let activeId = null;
let agents = [];

// ---- 通用内存缓存(前端会话内):TTL + 旧值先用(SWR)+ 并发去重 ----
// get(key, fetcher, {ttl, onRefresh}):fresh 期内直接回缓存;过期但有旧值时立即回旧值、
// 后台刷新(新值经 onRefresh 回调,调用方自行重绘);无缓存则等 fetcher。fetcher 抛错不入缓存。
// 任何"整页重建导致的重复取数"都可以接:面板取数、记忆、Context 列表等。
const memCache = {
  _m: new Map(),
  async get(key, fetcher, opts = {}) {
    const ttl = opts.ttl || 60000;
    const e = this._m.get(key);
    if (e && e.v !== undefined && Date.now() - e.t < ttl) return e.v;
    if (e && e.v !== undefined) { this._refresh(key, fetcher, opts); return e.v; }
    return this._refresh(key, fetcher, opts);
  },
  _refresh(key, fetcher, opts) {
    let e = this._m.get(key);
    if (!e) { e = {}; this._m.set(key, e); }
    if (e.inflight) return e.inflight;
    e.inflight = (async () => {
      try {
        const v = await fetcher();
        e.v = v; e.t = Date.now();
        if (opts.onRefresh) { try { opts.onRefresh(v); } catch {} }
        return v;
      } finally { e.inflight = null; }
    })();
    return e.inflight;
  },
  peek(key) { const e = this._m.get(key); return e ? e.v : undefined; },
  drop(key) { this._m.delete(key); },
  dropPrefix(pre) { for (const k of [...this._m.keys()]) if (k.startsWith(pre)) this._m.delete(k); },
};

// 全局链接路由(捕获期):主文档里的锚点一律不许带走主框架。页内锚点照常,其余按目标分流。
// contenteditable 里的 <a> 被编辑克隆后会丢 onclick(JS 属性不随克隆走),没有这层兜底就会整窗导航走丢。
//
// 相对路径这一支是 2026-08-10 补的:备忘与正文里写的仓内路径经 markdown 渲染成 <a href="docs/x.md">,
// 点一下就相对当前页导航到一个后端没有的 URL,拿回一张 http.server 的裸 404 —— 整个应用被顶掉,
// 没有返回入口,只能退出重开。放不放得开那个文件是次要的,不能让它导航走才是要紧的。
document.addEventListener("click", (ev) => {
  const a = ev.target && ev.target.closest ? ev.target.closest("a[href]") : null;
  if (!a) return;
  const href = (a.getAttribute("href") || "").trim();
  if (!href || href.startsWith("#")) return;   // 页内锚点:放行
  // 先判归属,再决定拦不拦。这一层挡的是「会把主框架导航走」的链接;而带自定义协议的 href
  // (javascript:void(0) 之类)多半只是个占位,真正的动作挂在元素自己的 onclick 上——
  // 会话里的文件引用就是这么建的(见 app-chat.js 的 hsFileLink)。
  // 原来这一支排在 stopPropagation 之后:捕获期就把事件打断了,目标的 onclick 永远轮不到,
  // 一屏文件引用全部点不动(2026-08-16 实见)。所以这类只 preventDefault 挡掉导航,
  // 不动传播,让页面自己那份处理照常跑。
  const fileLine = typeof hsIsFileLineHref === "function" && hsIsFileLineHref(href);
  if (!fileLine && !/^(https?|mailto|tel):/i.test(href) && /^[a-z][\w+.-]*:/i.test(href)) {
    ev.preventDefault();
    return;
  }
  ev.preventDefault();
  ev.stopPropagation();
  if (/^https?:\/\//.test(href)) {
    // 回答里的飞书消息引用属于当前会话的证据，不是一个要离开 Desk 浏览的网站。
    // 只在会话页、且链接能定位到具体消息时接管；普通群链接和文档链接仍走原路。
    if (typeof hsLarkRefOpenFromLink === "function" && hsLarkRefOpenFromLink(href, a)) return;
    if (typeof openPage === "function") openPage(href, (a.textContent || "").trim().slice(0, 40) || href);
    return;
  }
  if (/^(mailto|tel):/i.test(href)) { if (typeof openExternal === "function") openExternal(href); return; }
  // 只剥 ./ 这种相对前缀。原来写的是 /^\.?\//,那个点是可选的,于是绝对路径的根斜杠
  // 也被一起吃掉:/Users/…/orientation.md 变成 Users/…,hsResolveFile 认不出是绝对路径,
  // 退去仓内找一个叫 Users 的顶层目录,自然找不到(2026-08-19 用户实见)。
  // 根相对的文档链接(/docs/x.md)仍然照旧能开:hsResolveFile 试过绝对路径不存在之后
  // 会自己退回仓内再找一遍。
  // 把锚点本身一并交下去：相对路径的基准属于它所在的会话，不能拿当前工作流、
  // 当前侧栏选择或第一个同名代码仓来猜。绝对路径仍不依赖这个上下文。
  if (typeof hsOpenFileHref === "function") hsOpenFileHref(href, a);
  else if (typeof hsOpenFile === "function") hsOpenFile(href.replace(/^\.\//, "").split("#")[0]);
}, true);

// 当前连的是哪一份后端:本机(local)还是服务器上常驻的那份(remote)。
// 由 /workbench/backends 报,init 时取一次。取不到按 local——退化成原有行为,不猜。
// 不在前端按地址判:经 SSH 隧道访问服务器时页面地址也是 127.0.0.1。
let _deskRole = "local";
function deskRole() { return _deskRole; }
// 同 initEnginePick:取数与应用拆开,开机那份随 /workbench/boot 一起回来时直接应用。
async function loadDeskRole() {
  try { applyDeskRole(await api("/workbench/backends")); } catch {}
}

function applyDeskRole(r) {
  if (r && r.role === "remote") _deskRole = "remote";
}

// ---- 工作台导航状态 ----
// 已评审自绘内联图标：32格；制作与摘要见 vendor/original-inline-icon-manifest.json。
// _svg 仅供保留的旧工作流 zap 键使用，其他图形保持各自已确认的光学比例。
const _svg = (inner) =>
  `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;
const ICONS = {
  server: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.server\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"4\" y=\"3.5\" width=\"24\" height=\"10\" rx=\"3.6\" /><rect x=\"4\" y=\"18.5\" width=\"24\" height=\"10\" rx=\"3.6\" /><circle cx=\"9\" cy=\"8.5\" r=\"1.15\" fill=\"currentColor\" stroke=\"none\" /><circle cx=\"9\" cy=\"23.5\" r=\"1.15\" fill=\"currentColor\" stroke=\"none\" /><path d=\"M17.5 8.5h4.5M17.5 23.5h4.5\" /></g></svg>",
  share: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.share\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M10.5 14.2 21 8.6M10.5 17.8 21 23.4\" /><circle cx=\"7\" cy=\"16\" r=\"3.6\" /><circle cx=\"24\" cy=\"6.8\" r=\"3.6\" /><circle cx=\"24\" cy=\"25.2\" r=\"3.6\" /></g></svg>",
  laptop: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.laptop\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"5.5\" y=\"4\" width=\"21\" height=\"18\" rx=\"3.8\" /><path d=\"M5.5 26.5h21\" /></g></svg>",
  cloud: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.cloud\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M9 26c-4.3 0-7-2.7-7-6.6 0-3.6 2.5-6.3 5.9-6.8.9-5.1 4.2-8 8.8-8 5.1 0 8.2 3.5 8.7 8.6 3.1.8 4.6 3 4.6 6.2 0 4-2.7 6.6-7.1 6.6Z\" /></g></svg>",
  link: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.link\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M12 9.2H9.2a6.8 6.8 0 0 0 0 13.6H12M20 9.2h2.8a6.8 6.8 0 0 1 0 13.6H20M10 16h12\" /></g></svg>",
  // Legacy workflow icon key: retained for dynamic configuration compatibility.
  zap: _svg('<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>'),
  bell: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"16\" height=\"16\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.bell\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path fill=\"currentColor\" d=\"M8 13.8c0-5 3-8 8-8s8 3 8 8v5l2.1 3.2c.6 1 .1 2-1.1 2H7c-1.2 0-1.7-1-1.1-2L8 18.8Z\" /><path d=\"M16 3.6v2.2M12.3 27c1.9 2.1 5.5 2.1 7.4 0\" /></g></svg>",
  shield: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.shield\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M16 3.4c-3.2 1.9-6.7 3-10.4 3.6v8.2c0 5.9 4.1 10.3 10.4 13.4 6.3-3.1 10.4-7.5 10.4-13.4V7c-3.7-.6-7.2-1.7-10.4-3.6Z\" /></g></svg>",
  clipboard: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.clipboard\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M10.5 6.5H9c-3 0-4.5 1.7-4.5 4.7v13c0 3 1.5 4.5 4.5 4.5h14c3 0 4.5-1.5 4.5-4.5v-13c0-3-1.5-4.7-4.5-4.7h-1.5\" /><rect x=\"10.5\" y=\"3\" width=\"11\" height=\"6\" rx=\"2.7\" /><path d=\"m10.7 18.1 3.5 3.6 7.2-7.5\" /></g></svg>",
  activity: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.activity\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M2.8 16H8l4-10 7.1 20 4.7-10h5.4\" /></g></svg>",
  overview: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.overview\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3.5\" y=\"5\" width=\"25\" height=\"22\" rx=\"5\" /><path d=\"M13 5.8v20.4M13.5 16h14.2\" /></g></svg>",
  list: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.list\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"5.5\" cy=\"6\" r=\"1.35\" fill=\"currentColor\" stroke=\"none\" /><circle cx=\"5.5\" cy=\"16\" r=\"1.35\" fill=\"currentColor\" stroke=\"none\" /><circle cx=\"5.5\" cy=\"26\" r=\"1.35\" fill=\"currentColor\" stroke=\"none\" /><path d=\"M12 6h15M12 16h15M12 26h15\" /></g></svg>",
  book: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.book\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M16 7C12.8 4.9 8.6 4.7 4 5.7v21c4.9-1.1 8.8-.6 12 1.7 3.2-2.3 7.1-2.8 12-1.7v-21c-4.6-1-8.8-.8-12 1.3Zm0 0v21.4\" /></g></svg>",
  monitor: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.monitor\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3.5\" y=\"3.5\" width=\"25\" height=\"19\" rx=\"4.5\" /><path d=\"M16 22.5v6M10 28.5h12\" /></g></svg>",
  trash: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"16\" height=\"16\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.trash\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><g transform=\"translate(-1.17296 -1.07234) scale(1.07331)\"><path d=\"M6.5 10v13.5c0 3 1.8 5 4.8 5h9.4c3 0 4.8-2 4.8-5V10M4 7.8h24M10.5 7.8V5.4c0-1.4.8-2.1 2.3-2.1h6.4c1.5 0 2.3.7 2.3 2.1v2.4M12.3 13.8v9M19.7 13.8v9\" /></g></g></svg>",
  code: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.code\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m10.5 7.5-7 8.5 7 8.5M21.5 7.5l7 8.5-7 8.5\" /></g></svg>",
  memory: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.memory\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M27 7.5c0 2.5-4.9 4.5-11 4.5S5 10 5 7.5 9.9 3 16 3s11 2 11 4.5ZM5 7.5v17c0 2.5 4.9 4.5 11 4.5s11-2 11-4.5v-17M5 16c0 2.5 4.9 4.5 11 4.5s11-2 11-4.5\" /></g></svg>",
  resume: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.resume\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M7.2 8.8a11.4 11.4 0 1 1-2 12M5 3.5v7.3h7.3\" /></g></svg>",
  enter: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"16\" height=\"16\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.enter\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><g transform=\"translate(-1.65323 -1.08592) scale(1.06787)\"><path d=\"m11 3.5 10.2 10.6c1.1 1.1 1.1 2.7 0 3.8L11 28.5\" stroke-width=\"2.6\" /></g></g></svg>",
  folder: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.folder\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M3 10V8c0-2.8 1.6-4.5 4.5-4.5h5c1.1 0 1.8.4 2.5 1.2l1.8 2H25c2.7 0 4 1.6 4 4.3v13.5c0 2.8-1.6 4.2-4.3 4.2H7.3C4.6 28.7 3 27.3 3 24.5V10Z\" /></g></svg>",
  branch: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"16\" height=\"16\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.branch\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><g transform=\"translate(-0.41744 -0.41744) scale(1.02609)\"><circle cx=\"7.5\" cy=\"6\" r=\"3.3\" /><circle cx=\"24.5\" cy=\"6\" r=\"3.3\" /><circle cx=\"16\" cy=\"26\" r=\"3.3\" /><path d=\"M7.5 9.3V12c0 4.9 8.5 5.3 8.5 10.7M24.5 9.3V12c0 4.9-8.5 5.3-8.5 10.7\" /></g></g></svg>",
  target: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.target\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M19 4.4A12 12 0 1 0 27.6 13M17.3 10.8a5.6 5.6 0 1 0 3.9 3.9M16 16l9.1-9.1\" /><path d=\"m29.2 3-2.1 6.3-4.4-4.4Z\" fill=\"currentColor\" stroke=\"none\" /></g></svg>",
  note: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.note\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"5\" y=\"3.5\" width=\"22\" height=\"25\" rx=\"4.8\" /><path d=\"M11 10.5h10M11 17h10M11 23.5h6\" /></g></svg>",
  pencil: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"16\" height=\"16\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.pencil\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.2\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><g transform=\"translate(-3.08656 -2.14561) scale(1.13189)\"><path d=\"m7.2 20.7 12.7-14.5a3.5 3.5 0 0 1 5.3 4.7l-12.7 14.5-6.3 2.1Z\" /><path d=\"m17.4 9.1 5.3 4.7M18.5 28h9\" /></g></g></svg>",
  back: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"16\" height=\"16\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.back\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><g transform=\"translate(32 0) scale(-1 1)\"><g transform=\"translate(-1.65323 -1.08592) scale(1.06787)\"><path d=\"m11 3.5 10.2 10.6c1.1 1.1 1.1 2.7 0 3.8L11 28.5\" stroke-width=\"2.6\" /></g></g></g></svg>",
  plus: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.plus\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M16 5v22M5 16h22\" /></g></svg>",
  more: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.more\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><circle cx=\"6\" cy=\"16\" r=\"2\" fill=\"currentColor\" stroke=\"none\" /><circle cx=\"16\" cy=\"16\" r=\"2\" fill=\"currentColor\" stroke=\"none\" /><circle cx=\"26\" cy=\"16\" r=\"2\" fill=\"currentColor\" stroke=\"none\" /></g></svg>",
  check: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.check\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m5 16.6 7.3 7.4L27 7\" /></g></svg>",
  close: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.close\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m7 7 18 18M25 7 7 25\" /></g></svg>",
  hide: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.hide\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M4 12.3c3.3-4.1 7.2-6.2 12-6.2 5.3 0 9.6 3.5 13 9.9-1.4 2.7-3.1 4.9-5.1 6.6M18.5 25.7c-.8.1-1.6.2-2.5.2-5.3 0-9.6-3.5-13-9.9M6 3.7l20 24.6M12.8 12.2a5 5 0 0 0 6.4 7.6\" /></g></svg>",
  file: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.file\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M18.3 3.5H9.2C6 3.5 4.5 5.3 4.5 8.5v15c0 3.2 1.5 5 4.7 5h13.6c3.2 0 4.7-1.8 4.7-5V12.4c0-1.3-.5-2.4-1.4-3.3l-4.5-4.2c-.9-.9-2-1.4-3.3-1.4Z\" /><path d=\"M18.5 4v5.6c0 1.5.8 2.3 2.3 2.3h5.8\" /></g></svg>",
  image: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.image\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3.5\" y=\"3.5\" width=\"25\" height=\"25\" rx=\"5\" /><circle cx=\"10\" cy=\"10\" r=\"2\" fill=\"currentColor\" stroke=\"none\" /><path d=\"m4.5 24 7-8 5.8 6.1 5.2-5.5 5 5.5\" /></g></svg>",
  braces: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.braces\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M11 3H9c-2.2 0-3.3 1.6-3.3 4v4c0 2.6-1.1 4.1-3.2 5 2.1.9 3.2 2.4 3.2 5v4c0 2.4 1.1 4 3.3 4h2M21 3h2c2.2 0 3.3 1.6 3.3 4v4c0 2.6 1.1 4.1 3.2 5-2.1.9-3.2 2.4-3.2 5v4c0 2.4-1.1 4-3.3 4h-2\" /></g></svg>",
  shellfile: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.shellfile\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.25\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"m5 7 8.5 9L5 25M18 25h10\" /></g></svg>",
  moon: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.moon\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M14.4 3.7a12.4 12.4 0 1 0 13.9 15.1c-4.6 1-8.5-.2-11.2-3.3-2.7-3.1-3.8-7.4-2.7-11.8Z\" /></g></svg>",
  sidebar: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.sidebar\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3.5\" y=\"5\" width=\"25\" height=\"22\" rx=\"5\" /><path d=\"M12 5.8v20.4\" /></g></svg>",
  terminal: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.terminal\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3.5\" y=\"5\" width=\"25\" height=\"22\" rx=\"5\" /><path d=\"m9 12 4.5 4-4.5 4M18.7 21h4.8\" /></g></svg>",
  browser: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.browser\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><rect x=\"3.5\" y=\"5\" width=\"25\" height=\"22\" rx=\"5\" /><path d=\"M4.3 12h23.4\" /><circle cx=\"8\" cy=\"8.5\" r=\"0.85\" fill=\"currentColor\" stroke=\"none\" /><circle cx=\"12\" cy=\"8.5\" r=\"0.85\" fill=\"currentColor\" stroke=\"none\" /></g></svg>",
  lock: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" width=\"16\" height=\"16\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.lock\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><g transform=\"translate(-0.52512 -0.29919) scale(1.03282)\"><rect x=\"5.5\" y=\"12\" width=\"21\" height=\"17\" rx=\"5.2\" /><path d=\"M10 12V8.6a6 6 0 0 1 12 0V12\" /><circle cx=\"16\" cy=\"19.2\" r=\"1.35\" fill=\"currentColor\" stroke=\"none\" /><path d=\"M16 20.4v3.4\" /></g></g></svg>",
  chart: "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 32 32\" width=\"16\" height=\"16\" fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\" class=\"desk-reviewed-icon\" data-desk-icon=\"ICONS.chart\" aria-hidden=\"true\"><g fill=\"none\" stroke=\"currentColor\" stroke-width=\"2.1\" stroke-linecap=\"round\" stroke-linejoin=\"round\"><path d=\"M4.5 3.5v21c0 2.3 1.3 3.5 3.5 3.5h20M11 21v-7M18 21V9M25 21V4\" /></g></svg>",
};
const DESKTOP = "_desktop";   // 伪工作区：未归类会话的家，默认落点
// 渲染主题只有两个:默认的深色(cyan)与翻面的浅色。设置页给当前端提供浅色 / 深色
// 两种固定偏好，不随系统外观变化。
// 曾经还有紫、绿、橙、玫红、灰五套换色,但配色选择器早已不在界面上——常量与 CSS 都留着、
// 谁也选不中(2026-08-16 核对后清理)。要恢复的话得连选择器一起做,不是补回这几行。
const THEME_PRESET_IDS = new Set(["cyan", "light"]);
const THEME_PREFERENCE_IDS = new Set(["light", "dark"]);
let domains = [];                 // 工作区目录（workbench.json，发行版资产）
let hiddenDomains = [];           // 本机隐藏的工作区 id（.workbench/hidden.json，可恢复，不动官方定义）
let hiddenFoldOpen = false;       // 「已隐藏」折叠的展开态（跨重渲染保留，恢复时不收起）
let expandedProjects = new Set(); // coding 已展开的项目 id（跨重渲染保留展开态，避免自动折叠）
let nav = { type: null, id: null };
let wbState = { sessions: {} };   // 运行态：会话→工作区映射（.workbench/state.json）
let allSessions = [];
let activeAssetPath = null;
let unassignedOpen = false;
let taskAuditOpen = false;
let desktopApps = [];   // desktop 通用应用（workbench.desktopApps）

// 流式状态：messageID/partID → DOM 与缓冲
const msgEls = new Map();   // messageID → 消息容器
const partBuf = new Map();  // partID → { type, field, text, bodyEl, msgID }

// 这一页是不是本机在看。经隧道从别处连进来时,每个请求都是一次跨区往返
// (2026-08-25 实测:复用连接时中位 242ms,新建连接时一半会超 600ms、最慢 1540ms),
// 凡是「只对开发机有意义」或「密到人察觉不出差别」的轮询,都该按这一条收敛。
// 判据看的是客户端在哪,而不是服务端是不是开发态——后者问的是另一回事:同一份服务端
// 既服务本机的 app 壳,也服务手机经隧道连进来的那一页。
function isLocalView() {
  const h = location.hostname;
  return h === "127.0.0.1" || h === "localhost" || h === "::1" || h === "[::1]";
}

// 同一条 GET 正在飞时不再发第二趟,两个调用方共用同一个 Promise。
// 启动那一窗里多个模块各自取同一份数据是常态(任务清单三处要、工作流任务两处要、
// 隧道状态两处要),本机看不出来,经隧道每一趟都是一次跨区往返(中位 242ms)——
// 2026-09-02 实测:15 个启动请求里 4 个是这种重复,占掉将近一秒。
// 只合并 GET:带副作用的方法各发各的。请求落地(成功或失败)即出表,不做结果缓存,
// 免得把「刚写完再读」读成旧值。
const _inflight = new Map();

// 空闲再做。启动窗里挤着的次要请求挪到这里:有 requestIdleCallback 就等真正空闲,
// 没有(Safari/WKWebView 至今不支持)退到一个足够晚的定时器——手机端那一页正是它。
function _idle(fn, delay = 1500) {
  if (typeof requestIdleCallback === "function") requestIdleCallback(fn, { timeout: 4000 });
  else setTimeout(fn, delay);
}

function api(path, opts = {}) {
  const method = (opts.method || "GET").toUpperCase();
  const single = method === "GET" && !opts.body && !opts.signal;
  if (single) {
    const hit = _inflight.get(path);
    if (hit) return hit;
  }
  const p = _apiFetch(path, opts);
  if (single) {
    _inflight.set(path, p);
    p.catch(() => {}).finally(() => {
      if (_inflight.get(path) === p) _inflight.delete(path);
    });
  }
  return p;
}

function _apiFetch(path, opts = {}) {
  const ctrl = new AbortController();
  const t = setTimeout(() => ctrl.abort(), opts.timeout || 8000);
  // 动态 API 在同一个 URL 下持续变化。Safari/WKWebView 会缓存没有明确策略的 JSON，
  // 使手机端在 Desk 重启后仍读到旧模型目录；请求侧也显式绕过缓存，与响应头双保险。
  return fetch(path, { cache: "no-store", ...opts, signal: ctrl.signal })
    .then((r) => {
      if (!r.ok) return r.text().then((b) => {   // 带出响应体里的真实错误(后端统一 {error} 字段),不只给状态码
        let m = "", data = null; try { data = JSON.parse(b); m = data.error || ""; } catch {}
        const error = new Error(`${path} → ${r.status}${m ? "：" + m : ""}`);
        // 调用方用结构化状态识别能力缺失等兼容场景；不要再解析展示文案。
        error.status = r.status;
        error.path = path;
        error.detail = m;
        error.code = data && data.code;
        error.data = data;
        throw error;
      });
      if (r.status === 204) return null;
      const ct = r.headers.get("content-type") || "";
      return ct.includes("json") ? r.json() : r.text();
    })
    .finally(() => clearTimeout(t));
}

// ---- deskLog:把前端的诊断写进日志悬浮窗 ----------------------------------
// 浏览器里 console.log 的东西只有开着 Web 检查器才看得到,而前端状态恰恰是最难取证的
// 那一部分——「切引擎重载之后过程区展不开」这类问题,现场只在页面里,后端日志一无所知。
// 后端的 /workbench/logs 加了 POST 分支(lib/logbuf.py 的 push_client),前后端进同一个
// 环形缓冲、同一个面板,时间线对得上:哪一帧到了后端、界面那一刻画成什么样,并排就能读。
//
// 三条约束决定了下面的写法:
// 一、不能拖慢主流程。调用方一律不 await,发送失败静默吞掉——诊断打不出去是小事,
//     为它抛一个未捕获的 Promise rejection 反而污染真正要看的现场。
// 二、要合批。流式渲染每帧都可能打一条,一条一个请求会把 ui_server(单线程 HTTP)打满,
//     于是攒 200ms 发一次;push_client 本来就按行拆,多行拼成一个 body 后在面板里仍是多行。
// 三、同时留一份在 console。开着检查器排查时不该反而少了信息。
const DESKLOG_MS = 200;
const DESKLOG_MAX = 400;   // 后端不可达时缓冲不无限涨:超出就丢最早的,现场以最近的为准
let _deskLogBuf = [], _deskLogTimer = 0;

function _deskLogFlush() {
  _deskLogTimer = 0;
  if (!_deskLogBuf.length) return;
  const line = _deskLogBuf.join("\n");
  _deskLogBuf = [];
  // 不走 api():那层会把非 2xx 变成异常,而这里任何失败都该无声。keepalive 让页面正在
  // 卸载(切引擎重载页面)时最后一批仍发得出去——那恰好是最需要留痕的时刻。
  try {
    fetch("/workbench/logs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ line }),
      keepalive: true,
    }).catch(() => {});
  } catch {}
}

function deskLog(...args) {
  const line = args.map((a) => {
    if (typeof a === "string") return a;
    try { return JSON.stringify(a); } catch { return String(a); }   // 循环引用之类:退回 String
  }).join(" ");
  try { console.log(line); } catch {}
  _deskLogBuf.push(line);
  if (_deskLogBuf.length > DESKLOG_MAX) _deskLogBuf = _deskLogBuf.slice(-DESKLOG_MAX);
  if (!_deskLogTimer) _deskLogTimer = setTimeout(_deskLogFlush, DESKLOG_MS);
}
window.deskLog = deskLog;

// 过滤掉引擎插件注入的系统提示 part（非用户真实输入，如 oh-my-openagent 的 search-mode）
const INJECT_MARKERS = ["[search-mode]", "MAXIMIZE SEARCH EFFORT", "<system-reminder>", "[ultraworker]"];
// 本地命令的输出(<local-command-stdout> 等)也算注入:它是命令打印的东西,
// 不是谁说的话。实时那条路同样会收到,前端这道兜底与导入侧的处理对齐。
// 后台任务与子代理跑完时 harness 注入的完成通知(<task-notification>:任务 id、
// 输出文件、状态、摘要,子代理那种还带整份返回正文)也算注入。导入侧已按 origin.kind
// 认掉,这里留一道按前缀的兜底,给存量存储里已经落下的那些。
const LOCAL_CMD_OUT = ["<local-command-stdout>", "<local-command-stderr>", "<local-command-caveat>",
                       "<task-notification>"];
// 会话框架的碎片。harness 记账用的标签偶尔会被引擎当成正文吐出来——2026-08-16 实见一条
// 正常回复的末尾跟着 `system<total_tokens>15000000 tokens left</total_tokens>`。
// 它不是谁说的话,也不是要人处理的系统信息(那类走输入框上方那条窄条,见 hsEnvironment)。
// 与 isInjected 的分工:那个判「整条都是注入」,整块丢掉;这个剥的是挂在正常回复尾巴上的
// 一小段,整条丢会把回复一起丢掉,所以只能剥。
// 只认成对闭合的标签,不认半截:流式期间标签可能分两片到达,认半截会把还没说完的正文误伤。
const FRAMING_RE = /(?:^|\n)[^\S\n]*(?:system)?[^\S\n]*<total_tokens>[\s\S]*?<\/total_tokens>[^\S\n]*/gi;
// 历史证据引用给审计与调试用,不该直接进入用户可见正文。
const SHEPHERD_HISTORY_RE = /(?:^|\n)[^\S\n]*\[Shepherd history:\s*[A-Za-z0-9_-]+#\d+\][^\S\n]*/g;
function stripFraming(text) {
  const s = String(text == null ? "" : text);
  if (s.indexOf("<total_tokens>") < 0 && s.indexOf("[Shepherd history:") < 0) return s;
  return s.replace(FRAMING_RE, "\n").replace(SHEPHERD_HISTORY_RE, "\n").replace(/\n{3,}/g, "\n\n").trim();
}

function isInjected(text) {
  const t = (text || "").trimStart();
  if (LOCAL_CMD_OUT.some((m) => t.startsWith(m))) return true;
  return INJECT_MARKERS.some((m) => t.startsWith(m) || t.includes("MAXIMIZE SEARCH EFFORT"));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
}
function themePreference() {
  try {
    const id = localStorage.getItem("cd_theme_pref");
    // 兼容已经保存了旧 system 值、但尚未经过新版 index.html 迁移的页面。
    // 固化当前画面而不是继续问系统，保证这条兼容路也不会恢复自动跟随。
    if (id === "system") {
      const fixed = document.documentElement.dataset.theme === "light" ? "light" : "dark";
      localStorage.setItem("cd_theme_pref", fixed);
      return fixed;
    }
    return THEME_PREFERENCE_IDS.has(id) ? id : null;
  } catch { return null; }
}

function setThemePreference(preference) {
  if (!THEME_PREFERENCE_IDS.has(preference)) return;
  try { localStorage.setItem("cd_theme_pref", preference); } catch {}
}

// 手机上顶部那一条(状态栏 / 浏览器工具条)的底色要跟主题走,但它只在页面启动时取一次:
// iOS 主屏网页读 apple-mobile-web-app-status-bar-style,普通 Safari 标签读 theme-color,
// 两者运行时改 meta 都不重画(2026-09-03 用户实见:切到浅色后顶上仍压着一块黑)。
// 原来只对 standalone 重载,普通 Safari 标签被漏掉了——判据改成「手机端」整体,与 CSS
// 那条媒体查询同一口径(MOBILE_MQ);standalone 仍单独列着,桌面尺寸的主屏应用也算。
// 重载前 setThemePreference 已经把偏好落进 localStorage,index.html 启动那段读的就是它,
// 所以重来一次 meta 就是对的。桌面壳(WKWebView)与桌面浏览器改 meta 即时生效,不重载。
// 会话在服务端继续跑,页面回来后按导航栈恢复当前视图。
function refreshStandaloneThemeChrome() {
  const mobile = typeof isMobileView === "function" && isMobileView();
  if (navigator.standalone === true || mobile) setTimeout(() => location.reload(), 40);
}

function resolveTheme(theme) {
  const preference = themePreference();
  if (preference === "light") return "light";
  if (preference === "dark") return "cyan";
  // 没有端侧偏好时沿用服务端旧值,避免升级后擅自改变既有用户的外观。
  const id = THEME_PRESET_IDS.has(theme) ? theme : "cyan";
  return id;
}

function applyTheme(theme) {
  const id = resolveTheme(theme);
  document.documentElement.dataset.theme = id;
  // 状态栏底色跟着切(见 index.html 里那段说明);顺手缓存,下次启动能在首屏前就落对
  try { localStorage.setItem("cd_theme", id); } catch {}
  try { cdStatusBar(id === "light"); } catch {}
  // 原生窗口那层 vibrancy 也要跟着切:页面里半透明的区域(rail / panel / 页面)透出的正是它,
  // 它停在 darkAqua 的话,浅色下透出来的是深色磨砂,半透明处整片发灰。
  // 旧壳没有这条通道,postMessage 抛错被 nb 吞掉,行为不变(那时浅色只能靠不透明兜)。
  const mode = id === "light" ? "light" : "dark";
  try { nb({ action: "appearance", mode }); } catch {}
}

// 文件名 → highlight.js 语言 id(扩展名精确映射,不走 highlightAuto:auto 慢且偶尔误判)。
// 命中的扩展名才高亮,其余回退纯文本,确定可控。
const _CODE_LANG = {
  py: "python", pyi: "python", go: "go", rs: "rust",
  js: "javascript", jsx: "javascript", mjs: "javascript", cjs: "javascript",
  ts: "typescript", tsx: "typescript", mts: "typescript", cts: "typescript",
  java: "java", c: "cpp", h: "cpp", hpp: "cpp", cc: "cpp", cpp: "cpp", cxx: "cpp",
  json: "json", jsonc: "json", yml: "yaml", yaml: "yaml",
  sh: "bash", bash: "bash", zsh: "bash", swift: "swift",
};
const _CODE_HL_MAX = 150 * 1024;   // 超过此尺寸不高亮,直接纯文本——避免大文件同步高亮阻塞主线程
// 把一段文本按文件类型渲染进 pre.hljs:命中已注册语言且不超限则高亮,否则纯文本转义。
function renderCode(text, filename) {
  const ext = String(filename || "").split(".").pop().toLowerCase();
  const lang = _CODE_LANG[ext];
  if (lang && text.length <= _CODE_HL_MAX) {
    try {
      if (hljs.getLanguage(lang))
        return `<pre class="hljs"><code>${hljs.highlight(text, { language: lang }).value}</code></pre>`;
    } catch {}
  }
  return `<pre class="hljs"><code>${escapeHtml(text)}</code></pre>`;
}
function fmtTitle(s) {
  return (s.title || s.summary?.title || s.slug || s.id).toString().slice(0, 40);
}

// ---- 工作区(域)注册表数据层:分发版+运行态合并、本地应用并入、打开项目(自 app-settings 归位) ----
// 本地应用注册表（.workbench/apps.json）并入发行版 domains/desktopApps。
// 发行版 id 是产品契约，始终以官方定义为准；这也会自然吞掉旧版本曾写入 apps.json 的
// 7 个内置 App 副本。其余 id 仍是用户本地资产，照常追加，不改写、不删除注册表数据。
// pre:已经取回来的那份 /workbench/apps。开机时这一份与 domains 是一起发的
// (见 app-init.js 的那一波 Promise.all),传进来就不必再跑一趟——经隧道时一趟就是一秒多。
// 不传照旧自己取,别处的调用点一个字都不用改。
async function mergeLocalApps(pre) {
  let local = {};
  if (pre && typeof pre === "object") local = pre;
  else try { local = await api("/workbench/apps"); } catch { return; }
  const mergeInto = (arr, extra, protectOfficial = false) => {
    if (protectOfficial) {
      const official = (arr || []).map((a) => ({ ...a, _source: "official" }));
      const officialIds = new Set(official.map((a) => a.id));
      const localOnly = (extra || [])
        .filter((a) => !officialIds.has(a.id))
        .map((a) => ({ ...a, _source: "local" }));
      return official.concat(localOnly);
    }
    const ids = new Set((extra || []).map((a) => a.id));
    return (arr || []).filter((a) => !ids.has(a.id)).concat(extra || [])
      .map((a) => ({ ...a, _source: "local" }));
  };
  for (const d of domains) d.apps = mergeInto(d.apps, local[d.id]);
  desktopApps = mergeInto(desktopApps, local._desktop, true);
}

function mergeDomains(base, runtime) {
  // 分发版为底,运行态同 id 覆盖(字段合并、标 official),运行态独有的追加(标 local)。
  const out = (base || []).map((d) => ({ ...d, _source: "official" }));
  const idx = new Map(out.map((d, i) => [d.id, i]));
  for (const r of runtime || []) {
    if (idx.has(r.id)) out[idx.get(r.id)] = { ...out[idx.get(r.id)], ...r, _source: "official" };
    else { idx.set(r.id, out.length); out.push({ apps: [], ...r, _source: "local" }); }
  }
  return out;
}

async function loadHidden() {
  try { const h = await api("/workbench/hidden"); hiddenDomains = Array.isArray(h) ? h : []; }
  catch { hiddenDomains = []; }
}

async function reloadDomains() {
  // 重拉工作区(分发版 + 运行态),刷新 rail。用于打开/移除项目后即时反映
  let base = [];
  try { const wb = await api("/workbench.json"); base = wb.domains || []; } catch {}
  let extra = [];
  try { const e = await api("/workbench/domains"); if (Array.isArray(e)) extra = e; } catch {}
  domains = mergeDomains(base, extra);
  await loadHidden();
  await mergeLocalApps();
  renderRail();
}


// ---- 任务写入:所有入口共用的一份 ----------------------------------------
// SoT 是 domains/<id>/TASKS.md,写它的地方有四处(TaskDock、工作流页、会话输入框、
// TaskPaper 应用)加一个不经 HTTP 的 agent。前三处原来各写了一份「发请求 + 自己那块重绘」,
// 于是同一件事有三种写法:改名的 prompt 抄了两份,删除有的问有的不问,而且在 A 处改完 B 处
// 不会跟着变(TaskDock 勾一条,开着的工作流页仍显示旧状态)。这里收成一份:
// 写回统一走 taskWrite,改完广播一次,在场的视图各自重绘。
// 行级双校验(line + expect)不变——agent 并行改过文件时后端报 409,前端一律重拉,不盲改。
async function taskWrite(domainId, body) {
  let r = null, err = null;
  try {
    r = await api(`/workbench/domains/${encodeURIComponent(domainId)}/tasks`, {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  } catch (e) { err = e; }
  // 成功与失败都要刷新:409 说明文件已经被别处改过,界面上那一份更该退回文件的实况。
  try { await taskViewsRefresh(domainId, r); } catch {}
  return { ok: !!(r && r.ok), data: r, error: err };
}

// 写完之后谁该重绘。各视图把自己的刷新登记进来(挂 null 的表示该视图这次没在场)。
// 用登记表而不是在 taskWrite 里直接点名,是因为 app-core 先于那几个视图加载,
// 这里不该知道它们叫什么。
const _taskViewHooks = [];
function taskViewRegister(fn) { if (typeof fn === "function") _taskViewHooks.push(fn); }
async function taskViewsRefresh(domainId, resp) {
  for (const fn of _taskViewHooks) {
    try { await fn(domainId, resp); } catch {}
  }
}

// 语义化的几个动作。文案与确认口径也在这里统一:改名用同一句提示,删除一律先问一次
// ——删掉的是一行文本,回不来。
function taskSetState(domainId, t, state) {
  return taskWrite(domainId, { op: "set", line: t.line, expect: t.title, state });
}
function taskRename(domainId, t) {
  let v = "";
  try { v = (window.prompt("任务标题", t.title) || "").trim(); } catch {}
  if (!v || v === t.title) return Promise.resolve({ ok: false });
  return taskWrite(domainId, { op: "set", line: t.line, expect: t.title, title: v });
}
function taskDelete(domainId, t, ask = true) {
  if (ask && !window.confirm(`删除「${t.title}」?`)) return Promise.resolve({ ok: false });
  return taskWrite(domainId, { op: "del", line: t.line, expect: t.title });
}
function taskAdd(domainId, title) {
  const v = String(title || "").trim();
  if (!v) return Promise.resolve({ ok: false });
  return taskWrite(domainId, { op: "add", title: v });
}
// 行尾白名单注记(note: / due:)。value 空 = 清除。
function taskAnnot(domainId, t, key, value) {
  return taskWrite(domainId, { op: "annot", line: t.line, expect: t.title, key, value });
}

// ---- 添加到主屏 --------------------------------------------------------------
// 手机上从主屏图标进的是独立窗口:没有地址栏与底部工具栏,竖向多出约 15%,浏览器的
// 边缘返回手势也不再和侧栏抽屉抢。值得让人装一次。
//
// iOS 没有触发安装的 API,网页只能把那三步说清楚;Android Chrome 有 beforeinstallprompt,
// 那条才是真一键。所以这里两条路并存,能弹就弹、不能弹就讲。
let _a2hsPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); _a2hsPrompt = e; });

function a2hsStandalone() {
  try {
    return window.navigator.standalone === true
      || window.matchMedia("(display-mode: standalone)").matches;
  } catch { return false; }
}
// 能不能装:窄屏、还没装、并且这一份前端确实是给手机看的那一份。
function a2hsEligible() {
  if (a2hsStandalone()) return false;
  return typeof isMobileView === "function" ? isMobileView() : window.innerWidth <= 820;
}
// 装之前自检:manifest 的 start_url 得带票。经 relay 进来时,PWA 与浏览器不共享 Cookie,
// start_url 不带票的话,装上去的图标下次打开就是 401——那比不提示更糟。
// 本机直连(没有票)不需要票,那时 start_url 是干净的 "/",同样算通过。
async function a2hsCheck() {
  const loopback = /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])\b/.test(location.origin);
  if (loopback) return { ok: true, why: "" };
  let m = null;
  try { m = await (await fetch("/manifest.json", { credentials: "same-origin" })).json(); } catch {}
  if (!m) return { ok: false, why: "读不到 manifest,装了也起不来。" };
  if (!String(m.start_url || "").includes("token=")) {
    return { ok: false, why: "manifest 的启动地址还不带票——装上去下次打开会是 401。"
                          + "多半是 Desk 后端还没重启(动态 manifest 在那一侧),重启后再装。" };
  }
  return { ok: true, why: "" };
}

const A2HS_STEPS = "① 点浏览器底部的分享按钮　② 选「添加到主屏幕」　③ 右上角「添加」";

// 真正执行:Android 弹原生安装框,iOS 只能给步骤。
async function a2hsInstall() {
  const chk = await a2hsCheck();
  if (!chk.ok) { alert("现在还不宜添加到主屏:\n\n" + chk.why); return; }
  if (_a2hsPrompt) {
    try {
      _a2hsPrompt.prompt();
      const r = await _a2hsPrompt.userChoice;
      _a2hsPrompt = null;
      if (r && r.outcome === "accepted") return;
    } catch {}
  }
  alert("把 Desk 添加到主屏:\n\n" + A2HS_STEPS
        + "\n\n之后从图标进就是全屏,没有地址栏,也不用再输地址。");
}

// 一次性提示条:首次在手机浏览器里打开时从底部升起,关掉就不再出现。
// 不做成常驻入口——装一次就完了的事,常驻只会变成永远在那儿的一条横幅;
// 想过后再装的走设置页那条备份入口。
const A2HS_KEY = "cd_a2hs_dismissed";
function a2hsBarShow() {
  if (!a2hsEligible()) return;
  try { if (localStorage.getItem(A2HS_KEY) === "1") return; } catch {}
  if (document.getElementById("a2hs-bar")) return;
  const bar = document.createElement("div");
  bar.id = "a2hs-bar";
  bar.innerHTML = '<span class="a2-t"></span>'
    + '<button class="a2-go" type="button">添加</button>'
    + '<button class="a2-x" type="button" aria-label="不再提示">×</button>';
  bar.querySelector(".a2-t").textContent = "一键同步到主屏幕,便捷访问";
  bar.querySelector(".a2-go").onclick = () => a2hsInstall();
  bar.querySelector(".a2-x").onclick = () => {
    try { localStorage.setItem(A2HS_KEY, "1"); } catch {}
    bar.remove();
  };
  document.body.appendChild(bar);
  requestAnimationFrame(() => bar.classList.add("on"));
  // 停一会儿就收,不赖着。它是固定在底部的浮层,常驻就等于把每一页的底部都盖掉一条
  // ——设置页的最后一节、任务页的待办、应用与工坊的列表,全被它压着(2026-08-24 手机实见)。
  // 收起时记一笔「提示过了」:同一件事说一次就够,再想装从设置页进(那儿有常驻入口)。
  setTimeout(() => {
    if (!bar.isConnected) return;
    try { localStorage.setItem(A2HS_KEY, "1"); } catch {}
    bar.classList.remove("on");
    setTimeout(() => bar.remove(), 300);
  }, 10000);
}
// 晚一点再冒出来:刚进来那几秒人在看内容,横幅横插一杠只会被随手关掉,
// 而关掉就再也不提示了——那等于把这个功能浪费在最不合适的时刻。
setTimeout(() => { try { a2hsBarShow(); } catch {} }, 12000);
