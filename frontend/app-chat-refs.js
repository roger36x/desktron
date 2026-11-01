// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 5/13 片
// ---- 文件引用 --------------------------------------------------------------
// Codex 的标志性呈现:正文里提到的源文件渲染成带类型角标的链接,点开直接看那一行。
// 只处理裸文本节点,code / pre / a 里的原样不动——那些位置的路径本来就是要照抄的字面量。

const HS_EXT = "py|js|mjs|ts|tsx|jsx|rs|go|md|json|css|html|sh|bash|toml|yaml|yml|txt|c|cc|cpp|h|hpp|java|rb|swift|sql|ini|conf";
const HS_FILE_RE = new RegExp(
  "((?:[~.]{0,2}/)?(?:[\\w.@-]+/)*[\\w.@-]+\\.(?:" + HS_EXT + "))" +   // 路径,含起手的 / ~/ ./
  "(?:\\s*\\((?:line\\s*)?(\\d+)\\)|:(\\d+))?", "g");  // (line 12) 或 :12
// 行内码里的路径改走文件引用那套着色文字。hsLinkify 明确跳过 code/pre(那里通常是代码,
// 逐字变蓝会花),但模型提到目录与绝对路径时惯用行内码包起来,落在这条排除里,于是同一段话里
// 「CLAUDE.md」是蓝字、「/Users/.../workspace」却是灰块,两种写法指的是同一类东西。
// 判定放得比 HS_FILE_RE 宽:带扩展名的文件、以 / ~/ ./ 开头的路径、以 / 结尾的目录都算。
// 能定位到具体文件的顺带可点,目录只着色——点开一个目录在文件查看页里没有意义。
const HS_PATHY_RE = new RegExp("^(?:[~.]?/|(?:[\\w.@-]+/)+)[\\w.@/-]*$|^[\\w.@-]+\\.(?:" + HS_EXT + ")$");
function hsCodePaths(root) {
  root.querySelectorAll("code").forEach((c) => {
    if (c.closest("pre")) return;
    const txt = (c.textContent || "").trim();
    if (!txt || txt.length > 120 || /\s/.test(txt)) return;
    // 行号属于引用位置，不属于路径；先剥掉它再判定路径，完整引用仍交给下方解析。
    if (!HS_PATHY_RE.test(txt.replace(/:\d+$/, ""))) return;
    c.classList.add("hs-path");
    // 从根写起的绝对路径放不进正文那一列,而路径片默认不断行(见 .hs-file 的注释)。
    // 句中的相对引用仍连着读,这一种放开。
    if (/^[~/]/.test(txt)) c.classList.add("hs-path-full");
    HS_FILE_RE.lastIndex = 0;
    const m = HS_FILE_RE.exec(txt);
    if (!m || m[0] !== txt) return;   // 只有整段就是一个真实文件时才挂点击
    c.classList.add("hs-path-a");
    c.title = txt;
    c.onclick = () => hsOpenFile(m[1], m[2] || m[3] || "", c);
  });
}

function hsLinkify(root) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && n.parentElement.closest("code, pre, a"))
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
  });
  const hits = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    HS_FILE_RE.lastIndex = 0;
    if (HS_FILE_RE.test(n.nodeValue || "")) hits.push(n);
  }
  for (const n of hits) {
    const frag = document.createDocumentFragment();
    let last = 0;
    const txt = n.nodeValue;
    HS_FILE_RE.lastIndex = 0;
    for (let m = HS_FILE_RE.exec(txt); m; m = HS_FILE_RE.exec(txt)) {
      if (m.index > last) frag.appendChild(document.createTextNode(txt.slice(last, m.index)));
      // 整段就是一条路径时显示全路径:那种消息的用意就是把路径交给人,只留文件名
      // 等于把要用的东西吞掉,复制下来也不是原来那条。夹在句子里的引用仍只显示文件名。
      frag.appendChild(hsFileLink(m[1], m[2] || m[3] || "", m[0].trim() === txt.trim()));
      last = m.index + m[0].length;
    }
    if (last < txt.length) frag.appendChild(document.createTextNode(txt.slice(last)));
    n.replaceWith(frag);
  }
}

// 只留一个蓝色文件名链接。原先前面还挂一枚语言角标,信息价值低于它占的视觉重量:
// 扩展名在文件名里本来就写着,一行里出现两三个引用时,几块彩色小方块比正文还抢眼。
function hsFileLink(path, line, full) {
  const a = document.createElement("a");
  // full 那一支显示的是整条路径,长度常常超过正文一列。路径片默认不断行(句中引用连着
  // 读更清楚),但一条放不下的路径不断行只会把尾巴推出可视区,右边看不见也复制不全
  // (2026-09-10 用户实见)。所以整条交出去的那种单独放开断行。
  a.className = "hs-file" + (full ? " hs-path-full" : "");
  a.href = "javascript:void(0)";
  a.textContent = (full ? path : path.split("/").pop()) + (line ? ` (line ${line})` : "");
  a.title = path + (line ? ":" + line : "");
  a.onclick = (ev) => { ev.preventDefault(); hsOpenFile(path, line, a); };
  return a;
}

// Markdown links written by the assistant use the clickable-file convention
// `/absolute/path/file.ext:12`. The generic anchor router receives that suffix as
// part of the filename, unlike hsLinkify above which already separates it. Peel
// the location off before resolving the file so an arbitrary local absolute path
// keeps working even when it is outside every registered workflow repository.
// 裸文件名带行号（README.md:12）也符合 URI scheme 的字面规则。
// 仅放行已知文件扩展名 + 数字行号，其他自定义协议保留原路由。
function hsIsFileLineHref(href) {
  return new RegExp("^[\\w.@-]+\\.(?:" + HS_EXT + "):\\d+$", "i").test(href);
}

function hsOpenFileHref(href, source) {
  let path = String(href || "").trim().replace(/^\.\//, "");
  let line = "";
  const hash = path.match(/#L(\d+)$/i);
  if (hash) {
    line = hash[1];
    path = path.slice(0, hash.index);
  } else {
    path = path.split("#")[0];
    const suffix = path.match(/^(.*\.(?:py|js|mjs|ts|tsx|jsx|rs|go|md|json|css|html|sh|bash|toml|yaml|yml|txt|c|cc|cpp|h|hpp|java|rb|swift|sql|ini|conf)):(\d+)$/i);
    if (suffix) { path = suffix[1]; line = suffix[2]; }
  }
  // markdown-it 会把空格等字符编码进 href；文件接口接收的是本地路径，不是 URL。
  // decodeURI 保留斜杠等路径分隔符，只还原路径字符。坏编码按原字面继续解析。
  try { path = decodeURI(path); } catch {}
  hsOpenFile(path, line, source);
}

// 一条相对文件引用的解释基准只能来自它所在的会话。侧栏清单同时保存 Desk 会话 id、
// 引擎侧 id 与真实 cwd；任一种 id 命中都算同一段会话。找不到 cwd 时才退到会话归属的
// 工作流根。这里在点击时读取，不在渲染时固化：清单可能在页面建好后才异步回来。
function hsFileContextRoot(source) {
  if (!source || typeof source.closest !== "function" || typeof _hsViews === "undefined") return "";
  const page = source.closest(".hs-page");
  if (!page) return "";
  const view = [..._hsViews.values()].find((x) => x && x.el === page);
  if (!view) return "";
  const rows = (typeof _termsList !== "undefined" && Array.isArray(_termsList)) ? _termsList : [];
  const sid = String(view.sid || "");
  const row = rows.find((x) => x && [x.id, x.resume_id, x.session_id]
    .filter(Boolean).map(String).includes(sid));
  const cwd = String((row && row.cwd) || view.fileCwd || "").trim();
  if (cwd) return "abs:" + cwd;
  const domain = String((row && row.domain) || view.draftDomain || "").trim();
  return domain || "";
}

// 引用路径的落点解析。后端没有「工作区根」这个端点,所以按候选根逐个探:
// 绝对路径直接用自己的目录;相对路径拿各工作流登记的代码仓路径去试,谁读得到算谁。
// 命中的根缓存起来,同一段会话后续的引用不再重复探。
const _hsRootHit = new Map();

// 裸名搜过一次就记住:那是一次全仓 walk,同一篇正文里同名引用往往出现好几处。
const _hsBareHit = new Map();
// 把「文件所在的那一层」换成「.git 所在的那一层」。IDE 以哪儿为根,决定了左树看得见多少、
// 差异比不比得成:以文件所在目录为根时,树上只有同目录那几个文件(2026-08-19 用户提)。
// 问的是目录本身(path 传空),后端回仓根与「这个目录相对仓根在哪儿」——同一个目录下的第二个
// 文件因此不用再问一次。相对路径交给后端算:根可能写成 `abs:~/work/…`,而 git 报的是展开后的
// 绝对路径,两边字符串在前端对不上。不在仓里(比如 /tmp 下的临时文件)就原样用,不强求。
const _hsRepoRoot = new Map();
async function hsRepoRoot(loc) {
  if (!_hsRepoRoot.has(loc.root)) {
    let r = null;
    try {
      r = await api(`/workbench/fs?root=${encodeURIComponent(loc.root)}&path=&gitroot=1`,
                    { timeout: 5000 });
    } catch {}
    _hsRepoRoot.set(loc.root, (r && r.root) ? { top: r.root, dir: r.rel || "" } : null);
  }
  const hit = _hsRepoRoot.get(loc.root);
  if (!hit) return loc;
  return { root: "abs:" + hit.top, rel: hit.dir ? hit.dir + "/" + loc.rel : loc.rel };
}

async function hsResolveFile(path, source) {
  // `~/…` 与 `/…` 同属绝对路径,走同一条分支。后端两侧都认:/workbench/view 开头就 expanduser,
  // /workbench/fs 的 `abs:` 根同样 expanduser。前端原来只认斜杠开头,`~` 会被当成仓内相对路径
  // 去拼,于是明明存在的文件报找不到(2026-08-19 实见)。
  if (path.startsWith("~/") || path.startsWith("/")) {
    const i = path.lastIndexOf("/");
    const abs = { root: "abs:" + path.slice(0, i), rel: path.slice(i + 1) };
    try {
      await api(`/workbench/fs?root=${encodeURIComponent(abs.root)}`
                + `&path=${encodeURIComponent(abs.rel)}&file=1`, { timeout: 5000 });
      return await hsRepoRoot(abs);
    } catch {}
    // 开头带斜杠的未必是绝对路径:文档里的 /docs/x.md 说的是仓内的根相对位置。
    // 磁盘上没有这份的话,剥掉斜杠按仓内路径再找一遍——两种写法因此都能开。
    // `~/` 没有这层歧义(它只可能是家目录),但同样退回去找一遍无害;剥的是 "~/" 两个字符,
    // 按 slice(1) 剥会留下 "/work/…",那既不是绝对路径也不是仓内路径,两头都找不到。
    path = path.startsWith("~/") ? path.slice(2) : path.slice(1);
  }
  // 相对路径先按链接所属会话的 cwd 解释。这是 shell / agent 实际采用的语义；旧实现只在
  // 全部已登记仓库里逐个碰运气，同名路径会命中错误仓库，工作流目录里的文档则完全找不到。
  const contextRoot = hsFileContextRoot(source);
  if (contextRoot) {
    try {
      await api(`/workbench/fs?root=${encodeURIComponent(contextRoot)}`
                + `&path=${encodeURIComponent(path)}&file=1`, { timeout: 5000 });
      return await hsRepoRoot({ root: contextRoot, rel: path });
    } catch {}
  }
  const top = path.split("/")[0];
  // 兜底缓存也按会话根隔离。否则 A 会话先打开 docs/x.md 后，B 会话的 docs/y.md 会被
  // 无条件送到 A 的仓库；只有刷新页面才可能恢复。
  const rootKey = contextRoot + "\0" + top;
  if (_hsRootHit.has(rootKey)) return { root: _hsRootHit.get(rootKey), rel: path };
  let doms = [];
  try { doms = await api("/workbench/domains"); } catch {}
  const cands = [];
  for (const d of doms || []) for (const r of d.repos || []) if (r.path) cands.push("abs:" + r.path);
  for (const c of cands) {
    try {
      await api(`/workbench/fs?root=${encodeURIComponent(c)}&path=${encodeURIComponent(path)}&file=1`, { timeout: 5000 });
      _hsRootHit.set(rootKey, c);
      return { root: c, rel: path };
    } catch {}
  }
  // 裸文件名(正文里常写成 style.css,不带目录)按上面的拼法必然落空——它其实在
  // frontend/style.css。退一步用文件名全仓搜一次,只认整名相同的命中;多处同名时取路径最短的,
  // 通常就是最外层那份。搜不到才认输。
  if (!path.includes("/")) {
    if (_hsBareHit.has(path)) return _hsBareHit.get(path);
    const want = path.toLowerCase();
    for (const c of cands) {
      let r = null;
      try {
        r = await api(`/workbench/fs?root=${encodeURIComponent(c)}&q=${encodeURIComponent(path)}`,
                      { timeout: 8000 });
      } catch { continue; }
      const hit = ((r || {}).items || [])
        .filter((x) => x.type === "file" && String(x.name || "").toLowerCase() === want)
        .sort((a2, b2) => String(a2.path).length - String(b2.path).length)[0];
      if (hit) { _hsBareHit.set(path, { root: c, rel: hit.path }); return _hsBareHit.get(path); }
    }
  }
  return null;
}

// 文件查看页:等宽正文 + 行号,被引用的那一行高亮并滚到视野中央
async function hsOpenFile(path, line, source) {
  const loc = await hsResolveFile(path, source);
  // 本地文件首先是阅读动作：统一交给极简文件浏览器。它是单例应用，连续点链接只切换
  // 当前文件；需要改动时，应用内再明确交接给 Monaco IDE。应用尚未装好时保留下面的
  // 旧查看器兜底，避免本地定制缺失导致链接完全打不开。
  if (loc && typeof openFileBrowser === "function") {
    openFileBrowser(loc.root, loc.rel, line || "", true);
    return;
  }
  // html 直接交给浏览器应用。查看器是给「读代码、读文档」用的,一份网页要看的是它渲染成
  // 什么样,而浏览器本来就是干这个的;内嵌 iframe 那一版拿不到同目录的图片与样式(见 asHtml
  // 处的说明)。/workbench/view 对 .html 按 text/html 原样直出,拿它当标签地址即可。
  // 带行号的不走这里:那是要定位到某一行,得看源码。
  if (loc && !line && /\.html?$/i.test(path) && typeof browserOpenTab === "function") {
    // root 与 rel 原样透传:/workbench/view 认 root 参数,解析口径与 /workbench/fs 同源。
    // 自己拼绝对路径是不行的——root 未必是 abs: 形式,也可能是工作流 id,那时 rel 是仓内相对
    // 路径,拼出来会相对仓根解析、落到别处。
    browserOpenTab(`${location.origin}/workbench/view?root=${encodeURIComponent(loc.root)}`
                   + `&path=${encodeURIComponent(loc.rel)}`, path.split("/").pop());
    return;
  }
  const key = "hsfile:" + path;
  const name = path.split("/").pop();
  // markdown 渲染成正文,不摆源码:点开一份文档是来读的,`##` 和 `**` 摆在字面上只是噪音。
  // 带行号那一路除外——要定位到某一行就得有行,渲染之后行不存在了(工具卡里的 file:line
  // 走的正是这一路)。同一份文件因此可能两种样子,取决于你从哪儿点进来,这是有意的。
  const asMd = !line && /\.(md|markdown|mdx)$/i.test(path);
  // html 交给浏览器应用,不在查看器里内嵌。08-18 那一版用 iframe 的 srcdoc 渲染,
  // 问题是 srcdoc 没有基准地址,同目录的图片、样式、脚本一律取不到——自包含的单页看着正常,
  // 引用外部资源的页面就会缺样式,而那恰恰是需要认真看的那一类。
  // /workbench/view 对 .html 本来就按 text/html 原样直出,拿它当标签地址即可,后端不必改。
  // 带行号那一路仍走源码:要定位到某一行就得有行。
  const asHtml = !line && /\.html?$/i.test(path);
  // app:true 只为把页头那对「收起 / 关闭」放出来(见 style.css 的 #overlay.app-page)。
  // 漏了这一项的后果是这一页没有来处:不进标签条,页头又是空的,打开之后只能靠切到别的页
  // 才走得掉(2026-08-15 用户实见)。终端、文件、备忘几个页面本来都带着它。
  openContentPage(key, name, "≡", async (el) => {
    el.classList.add("hs-file-page");
    // 钮上那枚图标直接取应用面板里 Monaco IDE 的那一枚(_appIconHTML 按名字查同一张表),
    // 不另画:交接的去处和面板上那个应用是同一个,标记不一致等于让人再认一次。
    // 图标就站在句子里「IDE」那两个字的位置上——名字由图标自己说,不再重复一遍。
    // 取不到图标时退回文字,免得读成「在 中打开」。
    const ideIco = typeof _appIconHTML === "function"
      ? _appIconHTML("Monaco IDE", "ide", "", "", "") : "";
    el.innerHTML = '<div class="hs-fp-head"><span class="hs-fp-p"></span>'
      + '<button type="button" class="hs-fp-ide" hidden><span>在</span>'
      + (ideIco || "<span>IDE</span>") + '<span>中打开</span></button></div>'
      + (asMd ? '<div class="hs-fp-md md"></div>'
         : asHtml ? '<iframe class="hs-fp-web" sandbox="allow-scripts allow-popups allow-forms"></iframe>'
         : '<pre class="hs-fp"></pre>');
    el.querySelector(".hs-fp-p").textContent = path + (line ? ":" + line : "");
    // 出错要有地方说话,而这一页的主体是三选一的:md 是 div、html 是 iframe、其余是 pre。
    // 一律换成 pre 再写字,免得往 iframe 上塞文本(不显示)。
    const fail = (msg) => {
      const box = el.querySelector(".hs-fp, .hs-fp-md, .hs-fp-web");
      const pre = document.createElement("pre");
      pre.className = "hs-fp";
      pre.textContent = msg;
      if (box) box.replaceWith(pre); else el.appendChild(pre);
    };
    if (!loc) {
      fail("没在已登记的代码仓里找到这个路径。");
      return;
    }
    // 交接给编辑器:这一页只管读(markdown 按文档渲染、:line 定位),要改、要在同一个仓里
    // 跳来跳去就该换个地方。找不到这个路径时不出这枚钮——交接过去也是空的。
    const ide = el.querySelector(".hs-fp-ide");
    if (typeof openFileInIde === "function") {
      ide.hidden = false;
      ide.onclick = () => openFileInIde(loc.root, loc.rel);
    }
    let txt = "";
    try {
      txt = await api(`/workbench/fs?root=${encodeURIComponent(loc.root)}&path=${encodeURIComponent(loc.rel)}&file=1`);
    } catch (e) {
      fail("打不开:" + (e && e.message ? e.message : e));
      return;
    }
    if (asMd) {
      el.querySelector(".hs-fp-md").innerHTML = md.render(String(txt));
      return;
    }
    if (asHtml) {
      el.querySelector(".hs-fp-web").srcdoc = String(txt);
      return;
    }
    const pre = el.querySelector(".hs-fp");
    pre.innerHTML = "";
    String(txt).split("\n").forEach((ln, i) => {
      const row = document.createElement("div");
      row.className = "hs-fp-l" + (String(i + 1) === String(line) ? " on" : "");
      row.innerHTML = '<span class="hs-fp-n"></span><span class="hs-fp-c"></span>';
      row.querySelector(".hs-fp-n").textContent = i + 1;
      row.querySelector(".hs-fp-c").textContent = ln;
      pre.appendChild(row);
    });
    const hit = pre.querySelector(".hs-fp-l.on");
    if (hit) setTimeout(() => hit.scrollIntoView({ block: "center" }), 30);
  }, true, { app: true });
}

// ---- 消息操作行 ------------------------------------------------------------
// 悬停才出现,平时不占视觉。复制正文 / 用同一句再问一次 / 这条的时间。

// 消息 id 里带着毫秒时间戳(适配器建 id 用 msg_u_<ms> / msg_a_<ms>),解析不出就是 0。
function hsMsgMs(id) {
  return Number(String(id || "").replace(/^msg_[ua]_/, "")) || 0;
}
// 取不到时间就返回空串,由调用方决定整块不显示——宁可没有,也别露出 NaN 或空占位。
function hsWhen(ms) {
  if (!ms) return "";
  const d = new Date(ms);
  return `${d.getMonth() + 1}月${d.getDate()}日 ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

// 这条回复由哪个引擎生成。回合自己记着的为准(Shepherd 会话里各回合可能不同);
// 普通会话只有一个引擎,/config 回来之后用会话引擎补。认不出的引擎名不显示,不猜。
const HS_GEN_NAMES = { claude: "Claude", codex: "Codex", pi: "Pi" };
function hsCompactCount(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n < 0) return "";
  const units = [[1e9, "b"], [1e6, "m"], [1e3, "k"]];
  for (const [base, unit] of units) {
    if (n >= base) return (Math.round(n / base * 10) / 10).toFixed(1).replace(/\.0$/, "") + unit;
  }
  return String(Math.round(n));
}
function hsUsageLabel(t) {
  const usage = t && t.usage;
  if (!usage || typeof usage !== "object") return "";
  const tokens = hsCompactCount(usage.tokens);
  const cached = hsCompactCount(usage.cached);
  const parts = [];
  if (tokens) parts.push("tokens", tokens);
  if (cached) parts.push("cached", cached);
  return parts.join(" ");
}
function hsGenModelLabel(v, t) {
  const raw = String(t.model || "").trim();
  let model = "";
  if (raw && typeof hsRuntimeModelLabel === "function") {
    const resolved = hsRuntimeModelLabel(v, raw);
    if (resolved && resolved !== raw) model = resolved;
  }
  if (!model && raw && typeof hsPrettyModel === "function") model = hsPrettyModel(raw) || "";
  if (model && typeof hsCollapsedModelLabel === "function") model = hsCollapsedModelLabel(model);
  if (!model) {
    const eng = t.engine || (v.shepherdMode === false ? v.engine : "");
    model = HS_GEN_NAMES[eng] || "";
  }
  return model;
}
function hsGenLabel(v, t) {
  return [hsUsageLabel(t), hsGenModelLabel(v, t)].filter(Boolean).join(" ");
}
function hsGenPaint(v, t) {
  const el = t.footEl && t.footEl.querySelector(".hs-gen");
  if (!el) return;
  const usageEl = el.querySelector(".hs-gen-usage");
  const modelEl = el.querySelector(".hs-gen-model");
  const usageLabel = hsUsageLabel(t);
  const model = hsGenModelLabel(v, t);
  if (usageEl) {
    hsUiText(usageEl, usageLabel);
    hsUiHidden(usageEl, !usageLabel);
  }
  if (modelEl) {
    hsUiText(modelEl, model);
    hsUiHidden(modelEl, !model);
  }
  hsUiHidden(el, !(usageLabel || model));
  if (!usageEl || !modelEl) hsUiText(el, [usageLabel, model].filter(Boolean).join(" "));
  const usage = t && t.usage;
  const tokens = usage && hsCompactCount(usage.tokens);
  const cached = usage && hsCompactCount(usage.cached);
  const title = [tokens ? "Tokens " + tokens : "", cached ? "Cached " + cached : "", model].filter(Boolean).join(" / ");
  el.title = title;
}

// 用户发言的操作行:气泡下面一行,跟着气泡靠右。时间 / 复制原文。
// 显隐跟助手那条同一套(.hs-turn.hov 驱动),样式也复用 .hs-fb / .hs-time,不另起一套。
// 能力位到界面:回退按钮建行时先摆好(那时 /config 未必回来),拿到能力后统一显隐
function hsCapsPaint(v) {
  const caps = v.caps || {};
  // Shepherd 切换会从乐观状态、SSE、PUT 回包和 /config 多次走到这里。
  // 能力与生成者标签的输入没变时直接退出，避免每次都在长会话里 querySelectorAll，
  // 再把几十个相同文本节点和 hidden 属性重写一遍。手机 WebKit 会把这些空写放大成整页重排。
  const sig = [caps.rewind ? 1 : 0, caps.fork ? 1 : 0,
    v.shepherdMode ? "shepherd" : (v.engine || "")].join(":");
  if (v.capsPaintSig === sig) return;
  v.capsPaintSig = sig;
  v.el.querySelectorAll('[data-a="rewind"]').forEach((b) => hsUiHidden(b, !caps.rewind));
  // 普通引擎使用各自的原生分支；Shepherd 复制已校验历史并建立独立同引擎绑定。
  v.el.querySelectorAll('[data-a="fork"]').forEach((b) => hsUiHidden(b, !caps.fork));
  // 操作行可能早于 /config 建好,那时还不知道普通会话的引擎,这里补上。
  // Shepherd 每轮自带生成引擎，切 Producer 不改变旧回合标签，因此同一模式只需扫一次。
  for (const t of v.turns || []) hsGenPaint(v, t);
}

// 回退文件到某条发言之前。先 dry_run 拿清单给人看,确认了再执行。
// 只回退文件,不动对话记录——引擎给的是 rewind_files,它管的就是文件那一半;
// 记录仍留在会话里,所以按钮的说法是「回退文件」,不写成「回到这条」。
async function hsRewind(v, t) {
  if (!t.uid) return;
  // 回合跑着的时候不给回退:控制帧与回合帧共用一条流,两个读者会抢;何况在半途的回合上
  // 改文件,结果本来就难说清。后端也拦一道,这里先说明白,免得点了才被拒。
  if (v.busy) { alert("这一回合还在跑,等它结束再回退"); return; }
  const url = `/session/${encodeURIComponent(v.sid)}/rewind`;
  const body = (dry) => ({ method: "POST", headers: { "content-type": "application/json" },
                           body: JSON.stringify({ message_id: t.uid, dry_run: dry }), timeout: 90000 });
  let pre = null;
  try { pre = await api(url, body(true)); } catch (e) { alert("回退失败:" + e.message); return; }
  if (!pre || pre.error) { alert("回退失败:" + ((pre && pre.error) || "未知原因")); return; }
  if (!pre.canRewind) { alert("这条之前没有可回退的文件检查点"); return; }
  const files = pre.filesChanged || [];
  if (!files.length) { alert("这条之后没有文件改动,无需回退"); return; }
  const list = files.slice(0, 12).map((f) => "  " + f.split("/").slice(-3).join("/")).join("\n");
  const more = files.length > 12 ? `\n  …另有 ${files.length - 12} 个` : "";
  if (!confirm(`把这 ${files.length} 个文件回退到这条发言之前?\n\n${list}${more}\n\n`
               + `共 +${pre.insertions || 0} / -${pre.deletions || 0} 行。对话记录不变,只改文件。`)) return;
  let r = null;
  try { r = await api(url, body(false)); } catch (e) { alert("回退失败:" + e.message); return; }
  if (!r || r.error) { alert("回退失败:" + ((r && r.error) || "未知原因")); return; }
  hsStatus(v, "done", `已回退 ${files.length} 个文件`);
}

function hsUFoot(v, t, u) {
  const el = document.createElement("div");
  el.className = "hs-foot hs-foot-u";
  el.innerHTML =
    '<span class="hs-time"></span>' +
    '<button class="hs-fb" data-a="ucopy" data-tip="复制">' + HS_ICONS.copy + "</button>";
  const time = el.querySelector(".hs-time");
  time.textContent = hsWhen(t.uat || hsMsgMs(t.uid) || t.uts);
  if (!time.textContent) time.remove();
  el.querySelector('[data-a="ucopy"]').onclick = () => hsCopy(t.user || "");
  u.appendChild(el);
  t.uFootEl = el;
}

// 自续轮不给这一行:复制、分支、回退都以「这一轮是用户发起的」为前提,而它的 msgId 是
// 影子 ctx 自己造的,拿去分支或回退,后端在记录里根本找不到这条。
function hsFoot(v, t) {
  if (t.footEl || !t.msgId || t.aside) return;
  const el = document.createElement("div");
  el.className = "hs-foot";
  el.innerHTML =
    '<button class="hs-fb" data-a="copy" data-tip="复制">' + HS_ICONS.copy + "</button>" +
    '<button class="hs-fb" data-a="fork" data-tip="分支">' + HS_ICONS.fork + "</button>" +
    // 回退挂在回复下面而不是发言下面:要撤的是这一轮产生的文件改动,
    // 它是回复的结果,人也是看完回复才决定不要的。
    '<button class="hs-fb" data-a="rewind" data-tip="回退" hidden>' + HS_ICONS.rewind + "</button>" +
    '<span class="hs-time"></span>' +
    '<span class="hs-gen" hidden><span class="hs-gen-usage" hidden></span><span class="hs-gen-model" hidden></span></span>';
  el.querySelector(".hs-time").textContent = hsWhen(t.at || hsMsgMs(t.msgId));
  const rw = el.querySelector('[data-a="rewind"]');
  rw.onclick = () => hsRewind(v, t);
  // 直接按能力位定这一枚的显隐。原来调 hsCapsPaint,而它是在整页里找回退钮——
  // 此刻这一行还没挂进页面(挂载在函数末尾),自然找不到自己,于是永远留在隐藏态。
  rw.hidden = !(v.caps && v.caps.rewind);
  el.querySelector('[data-a="copy"]').onclick = () => {
    const txt = [...t.parts.values()].filter((x) => x.type === "text").map((x) => x.text || "").join("\n\n");
    hsCopy(txt);
  };
  const fk = el.querySelector('[data-a="fork"]');
  fk.hidden = !(v.caps && v.caps.fork);   // 同上:这一行还没挂进页面,只能就地定
  fk.onclick = () => hsFork(v, t);
  t.holder.appendChild(el);
  t.footEl = el;
  hsGenPaint(v, t);
}

// Shepherd 从所选回复处分支，保留当前执行引擎；普通引擎沿用各自的分支语义。
async function hsFork(v, t) {
  let s = null;
  try {
    s = await api(`/session/${encodeURIComponent(v.sid)}/fork`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ msg_id: t.msgId || "" }), timeout: 90000 });
  } catch (e) {
    alert("分支失败:" + (e && e.message ? e.message : e));
    return;
  }
  if (s && s.id) openHtmlSession(s.id, s.title || "分支");
}

// http 下 navigator.clipboard 不可用(非安全上下文),回落到隐藏 textarea,同终端那条复制
function hsCopy(text) {
  // 复制走同一道剥离:界面上看不见的框架碎片,粘出去也不该有(见 stripFraming)
  text = stripFraming(text);
  if (!text) return;
  try {
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(text); return; }
  } catch {}
  try {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    document.execCommand("copy");
    ta.remove();
  } catch {}
}
