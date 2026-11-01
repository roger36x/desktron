// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 8/13 片
// ---- 发送 ------------------------------------------------------------------

// ---- 附件 --------------------------------------------------------------
// 图片作为 image 块直接进模型;其它文件落盘后把路径给模型,让它用读文件的工具去看
// (文本、代码、日志动辄几十 KB,塞进上下文既贵又常超限,而引擎本来就会读文件)。
const HS_ATT_MAX = 12 * 1024 * 1024;   // 单个附件上限,超过就别往 base64 里塞

function hsAttAdd(v, files) {
  v.atts = v.atts || [];
  for (const f of files || []) {
    if (!f || f.size > HS_ATT_MAX) { if (f) alert(`${f.name} 超过 12MB,先放到磁盘再把路径贴进来`); continue; }
    const rd = new FileReader();
    rd.onload = () => {
      v.atts.push({ type: (f.type || "").startsWith("image/") ? "image" : "file",
                    name: f.name || "file", media_type: f.type || "",
                    data_b64: String(rd.result).split(",")[1] || "" });
      hsAttPaint(v);
    };
    rd.readAsDataURL(f);
  }
}

function hsAttPaint(v) {
  hsSendSync(v);   // 只挂了附件、一个字没写时也能发
  const box = v.el.querySelector(".hs-atts");
  if (!box) return;
  const list = v.atts || [];
  box.hidden = !list.length;
  box.innerHTML = "";
  list.forEach((a, i) => {
    const el = document.createElement("span");
    el.className = "hs-att";
    if (a.type === "image") {
      const im = document.createElement("img");
      im.src = a.data_b64 ? `data:${a.media_type || "image/png"};base64,${a.data_b64}` : (a.url || "");
      el.appendChild(im);
    }
    const nm = document.createElement("span");
    nm.className = "hs-att-n";
    nm.textContent = a.name;
    el.appendChild(nm);
    const x = document.createElement("button");
    x.className = "hs-att-x"; x.textContent = "×";
    x.onclick = () => { v.atts.splice(i, 1); hsAttPaint(v); };
    el.appendChild(x);
    box.appendChild(el);
  });
  if (typeof hsLarkRefContextState === "function") hsLarkRefContextState(v);
}

// 回合里的图有两种形态：本地刚发出的带 base64；从历史重建出来的只有
// `/session/<id>/asset/<name>` 引用（见 hsImgSrc）。输入框与发送只认 base64，把引用原样放回
// 输入框，缩略图画不出来，发送时后端也拿不到字节（Shepherd 拒收，其余引擎丢图）。
// 所以放回输入框之前，先按引用把字节取回来；取不回的明确告诉用户重新添加。
const HS_ATT_FETCH_MS = 15000;

function hsBlobB64(blob) {
  return new Promise((resolve, reject) => {
    const rd = new FileReader();
    rd.onload = () => resolve(String(rd.result).split(",")[1] || "");
    rd.onerror = () => reject(rd.error || new Error("读取附件失败"));
    rd.readAsDataURL(blob);
  });
}

async function hsAttHydrateOne(a) {
  if (!a) return null;
  if (a.data_b64) return a;
  if (a.type !== "image" || !a.url) return null;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), HS_ATT_FETCH_MS);
  try {
    const r = await fetch(a.url, { signal: ctrl.signal });
    if (!r.ok) return null;
    const blob = await r.blob();
    if (!blob.size || blob.size > HS_ATT_MAX) return null;
    const data = await hsBlobB64(blob);
    if (!data) return null;
    const base = decodeURIComponent(String(a.url).split("/").pop() || "");
    const mt = (blob.type || "").startsWith("image/") ? blob.type : (a.media_type || "image/png");
    return { type: "image", media_type: mt, data_b64: data,
             name: a.name || base.replace(/^\d+_[0-9a-f]+_/, "") || "image" };
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}

// 把附件接回输入框，返回取不回的个数。进行中的这一次挂在 v.attRestoring 上，发送会先等它。
function hsAttRestore(v, atts) {
  const list = (atts || []).filter(Boolean);
  if (!list.length) return Promise.resolve(0);
  const job = (async () => {
    const got = await Promise.all(list.map(hsAttHydrateOne));
    const ok = got.filter(Boolean);
    const lost = list.length - ok.length;
    if (ok.length) { v.atts = (v.atts || []).concat(ok); hsAttPaint(v); hsDraftSave(v, true); }
    if (lost) {
      hsEnvironment(v, { state: "warning", stage: "attachment",
        label: `有 ${lost} 个附件未能取回，请重新添加后再发送` });
    }
    return lost;
  })();
  const chained = (v.attRestoring || Promise.resolve()).then(() => job, () => job);
  v.attRestoring = chained;
  chained.finally(() => { if (v.attRestoring === chained) v.attRestoring = null; });
  return chained;
}

// ---- 草稿与排队 ------------------------------------------------------------
// 没发出去的字属于用户,不属于这一次页面加载。刷新、切视图、误关标签都不该把它冲掉,
// 所以随手存进本地。附件不存:base64 动辄几 MB,localStorage 塞不下也不该塞。
const HS_DRAFT_KEY = (sid) => "cd_hsdraft:" + sid;

// 草稿里的附件也存:截图是重新截一张才能补回来的东西,丢了比丢几个字麻烦得多。
// localStorage 每个源大约 5MB,存 base64 要留余地——超过 2MB 的那次就只存正文,
// 宁可少一张图也别把整份草稿(连正文一起)写失败。
const HS_DRAFT_ATT_MAX = 2_000_000;

function hsDraftAtts(list) {
  const out = [];
  let sum = 0;
  for (const a of list || []) {
    const n = (a.data_b64 || "").length;
    if (sum + n > HS_DRAFT_ATT_MAX) break;
    sum += n;
    out.push(a);
  }
  return out;
}

// 草稿是按键路径上最重的一件事:正文、队列连同附件的 base64 一起序列化后同步写进
// localStorage,贴过图之后每敲一个字符就是一次两兆的同步写,手感上就是打字一顿一顿。
// 改成停手之后再写。真正不能等的时刻(发送前、切走、关页面)显式 flush 一次。
const HS_DRAFT_MS = 300;

function hsDraftSave(v, now) {
  hsSendSync(v);   // 草稿存不存另说;调到这里就说明内容动过了
  if (now) {
    clearTimeout(v.draftTimer); v.draftTimer = 0;
    return hsDraftWrite(v);
  }
  if (v.draftTimer) return;
  v.draftTimer = setTimeout(() => { v.draftTimer = 0; hsDraftWrite(v); }, HS_DRAFT_MS);
}

// 读输入框的正文。不能用 textContent:contenteditable 里的换行有三种形态——文本节点里的
// \n、<br>、以及块级元素的边界,而 textContent 只认第一种。粘贴走的 execCommand("insertText")
// 会把换行变成一串 <div>,Shift+Enter 走浏览器默认也是,于是框里看着是多行,取出来却是一整行,
// 发给模型的就是没有换行的一坨。这里按三种形态一并还原。
const HS_INP_BLOCK = new Set(["DIV", "P", "LI", "PRE", "BLOCKQUOTE"]);
function hsInpText(el) {
  if (!el) return "";
  let out = "";
  const walk = (node) => {
    for (const n of node.childNodes) {
      if (n.nodeType === 3) { out += n.nodeValue; continue; }
      if (n.nodeType !== 1) continue;
      if (n.tagName === "BR") { out += "\n"; continue; }
      if (HS_INP_BLOCK.has(n.tagName) && out && !out.endsWith("\n")) out += "\n";
      walk(n);
    }
  };
  walk(el);
  return out;
}

function hsDraftWrite(v, strict = false) {
  if (v.ephemeral) return;   // 空聊天切走即消失，不把未创建的伪会话写进本地恢复记录
  const t = v.inp ? hsInpText(v.inp) : "";
  const a = hsDraftAtts(v.atts);
  const q = (v.queue || []).filter((x) => x.text || (x.atts || []).length || x.missingAttachments)
                           .map((x) => ({ t: x.text || "", a: hsDraftAtts(x.atts),
                             missing: !!x.missingAttachments || hsDraftAtts(x.atts).length < (x.atts || []).length }));
  // s = 此刻正在飞的那一句。原来发送前先清输入框、把「空」立刻写进这里,那句话在 POST
  // 落地之前就只活在内存里:请求失败或页面被关掉,草稿是空的、后端也没有,重开就没了
  // (2026-08-25 用户实见:显示处理中,过一会儿什么都不显示,退出重开消息不见了)。
  // 隧道断的时候尤其容易——POST 要等满 60 秒才报错,人不会等那么久。
  const sd = v.sending
    ? { t: v.sending.text || "", a: hsDraftAtts(v.sending.atts), rid: v.sending.requestID || "",
        autoRetry: v.sending.autoRetry !== false, missing: !!v.sending.missingAttachments || hsDraftAtts(v.sending.atts).length < (v.sending.atts || []).length }
    : null;
  try {
    if (t.trim() || a.length || q.length || sd || v.queueRecovery || v.backendDraft) {
      localStorage.setItem(HS_DRAFT_KEY(v.sid), JSON.stringify({ t, a, q, s: sd, recovery:v.queueRecovery || null, backendDraft:v.backendDraft || null,
        boot: typeof _hsRuntime !== "undefined" && _hsRuntime ? _hsRuntime.boot_id : "",
        qe: v.queueEditIndex, qi: v.queueIssue || null, qr: v.queueRetries || 0, qt: v.queueRetryAt || 0, qp: !!v.queuePaused }));
    } else localStorage.removeItem(HS_DRAFT_KEY(v.sid));
    return true;
  } catch {
    if (strict || v.queueRecovery) return false;
    // 多半是配额满了。退一步只存正文——正文丢了话就得重打,附件丢了还能重截一张。
    try { localStorage.setItem(HS_DRAFT_KEY(v.sid), JSON.stringify({ t, recovery:v.queueRecovery || null, backendDraft:v.backendDraft || null, q: q.map((x) => ({ t: x.t, a: [], missing: x.missing || x.a.length > 0 })),
                                                                    boot: typeof _hsRuntime !== "undefined" && _hsRuntime ? _hsRuntime.boot_id : "",
                                                                    qe: v.queueEditIndex, qi: v.queueIssue || null, qr: v.queueRetries || 0, qt: v.queueRetryAt || 0, qp: !!v.queuePaused,
                                                                    s: sd ? { ...sd, a: [], missing: sd.missing || sd.a.length > 0 } : null })); } catch {}
  }
}

function hsDraftLoad(v) {
  let d = null;
  try { d = JSON.parse(localStorage.getItem(HS_DRAFT_KEY(v.sid)) || "null"); } catch {}
  if (!d) return;
  v.queueRecovery = d.recovery || null;
  v.backendDraft = d.backendDraft || ((d.edit || d.uploads?.length || d.s?.editID)
    ? {edit:d.edit, uploads:d.uploads || [], sending:d.s} : null);
  // 已排队但未派发的内容不因重启要求人工续发。旧版暂停原因不可推断，保留一次确认。
  v.queueIssue = d.qi || (d.qp ? { kind: "legacy", message: "请确认旧版保留的待发消息" } : null);
  v.queuePaused = !!v.queueIssue && ["legacy", "action", "edit"].includes(v.queueIssue.kind);
  v.queueEditIndex = Number.isInteger(d.qe) ? d.qe : null;
  v.queueRetries = Number(d.qr) || 0; v.queueRetryAt = Number(d.qt) || 0;
  v.turnReady = false; // 恢复后必须重新取得后端 ready，不能复用页面旧状态。
  if (d.t) { v.inp.textContent = d.t; v.draftFresh = true; hsCmdPaint(v); }
  if (Array.isArray(d.a) && d.a.length) { v.atts = d.a.slice(); hsAttPaint(v); }
  // 上次关掉时还有一句在飞,而「在飞」不等于「没到」。直接回填的话,那一句可能已经
  // 在会话里跑着了,人再按一次就是第二遍。挂成待判,由 hsFollowTick 那一路问过后端
  // 再决定:到了就悄悄撤掉,没到才还给输入框。
  if (d.s && (d.s.t || (d.s.a || []).length || d.s.missing)) {
    v.sending = { text: d.s.t || "", atts: d.s.a || [], requestID: d.s.rid || "", autoRetry: d.s.autoRetry !== false, missingAttachments: !!d.s.missing };
    v.pendingSend = { ...v.sending };
    hsQPaint(v);
  }
  // 旧格式的队列存的是一串正文字符串,新的是 {t, a}。两种都认,不然升一次版草稿全丢。
  if (Array.isArray(d.q) && d.q.length) {
    v.queue = d.q.map((x) => (typeof x === "string" ? { text: x, atts: [] }
                                                    : { text: x.t || "", atts: x.a || [], missingAttachments: !!x.missing }));
    hsQPaint(v);
  }
  if ((v.queue || []).some((q) => q.missingAttachments)) hsQIssue(v, "edit", "附件未能完整保存，请编辑消息并重新添加");
}

// 只用于撤回后端队列版本的单次交接；正常发送不进入此接口。
async function hsRecoverServerQueue(v) {
  if (v.queueRecovering) return false;
  v.queueRecovering = true;
  v.queueRecoveryBlocked = true;
  try {
    if (v.queueRecoveryStale) throw Error("草稿已在另一页面变更，请刷新后恢复");
    const initialDraft = localStorage.getItem(HS_DRAFT_KEY(v.sid));
    const path = `/session/${encodeURIComponent(v.sid)}/queue-recovery`;
    const check = await api(path, {timeout:8000});
    if (!check.available && !v.queueRecovery && !v.backendDraft) {
      v.queueRecoveryBlocked = false; return true;
    }
    let owner = localStorage.getItem("desk.queue.recovery-owner");
    if (!owner) {
      owner = "browser_" + crypto.randomUUID();
      localStorage.setItem("desk.queue.recovery-owner", owner);
    }
    const exchange = (ack = "") => api(path, {
      method:"POST", headers:{"content-type":"application/json"},
      body:JSON.stringify({owner, ack}), timeout:8000 });
    const recover = async () => {
      if (localStorage.getItem(HS_DRAFT_KEY(v.sid)) !== initialDraft) {
        v.queueRecoveryStale = true;
        throw Error("草稿已在另一页面变更，请刷新后恢复");
      }
      // 回执丢失只补确认。只有完整写入浏览器的数据才有 recovery 标记。
      if (v.queueRecovery) await exchange(v.queueRecovery);
      else {
        const saved = JSON.parse(localStorage.getItem(HS_DRAFT_KEY(v.sid)) || "null");
        if (saved?.recovery) throw Error("另一页面已接收旧队列，请在该页面完成恢复或刷新当前页面");
        const result = await exchange();
        const draft = v.backendDraft || {};
        const sending = draft.sending;
        const editID = sending?.editID || draft.edit?.id;
        const ids = new Set((result.items || []).map(q => q.request_id));
        const uploads = [];
        for (const q of draft.uploads || []) {
          if (ids.has(q.requestID)) continue;
          const sent = await hsSentOrNot({...v, pendingSend:q}, q.text, {});
          if (sent === "unknown") throw Error("仍有旧上传需要核验，已保留原记录");
          if (sent === "not-sent") uploads.push(q);
        }
        const before = {queue:v.queue, pendingSend:v.pendingSend, sending:v.sending,
          backendDraft:v.backendDraft, queuePaused:v.queuePaused, queueIssue:v.queueIssue,
          queueRecovery:v.queueRecovery, atts:v.atts};
        const input = hsInpText(v.inp);
        v.queue = [...(v.queue || [])];
        for (const q of result.items || []) {
          if (q.id === editID) {
            if (sending?.editID) {
              v.inp.textContent = (sending.t || "") + (input ? "\n\n" + input : "");
              v.atts = [...(sending.a || []), ...(v.atts || [])];
            }
          } else v.queue.push({text:q.text, atts:q.atts});
        }
        v.queue.push(...uploads);
        if (v.pendingSend && (ids.has(v.pendingSend.requestID) || sending?.editID)) {
          v.pendingSend = null; v.sending = null;
        }
        v.backendDraft = null;
        if ((result.items || []).length || uploads.length) {
          v.queuePaused = true;
          v.queueIssue = {kind:"legacy", message:"旧待发消息已恢复，请确认后发送"};
        }
        v.queueRecovery = result.token || null;
        if (!hsDraftWrite(v, true)) {
          Object.assign(v, before); v.inp.textContent = input;
          throw Error("本地空间不足，旧待发消息仍保留在后端");
        }
        if (v.queueRecovery) await exchange(v.queueRecovery);
        if (result.blocked) hsErrorNotice(v, "有旧消息执行结果待核验，未重新入队", "delivery");
      }
      v.queueRecovery = null; v.queueRecoveryBlocked = false;
      hsDraftSave(v, true); hsQPaint(v); hsAttPaint(v);
    };
    if (navigator.locks) await navigator.locks.request("desk.queue.recovery:" + v.sid, recover);
    else await recover();
    return true;
  } catch (error) {
    const status = Number(error?.status || 0);
    const reason = status === 404
      ? "当前服务无法恢复旧待发消息（HTTP 404），请确认运行端版本后重试。"
      : status ? `${error.detail || "恢复请求失败"}（HTTP ${status}）`
        : String(error?.message || error);
    hsErrorNotice(v, "旧待发消息恢复未完成", "delivery", [reason]);
    return false;
  } finally { v.queueRecovering = false; }
}

// 排队:一回合在跑的时候接着输入,不必等。TUI 是这么做的,GUI 没理由更笨——
// 想到什么就写下来,轮到它自然会发。清单摆在输入框上方,发出去之前都还能撤。
function hsQPaint(v) {
  const box = v.el.querySelector(".hs-queue");
  if (!box) return;
  const list = v.queue || [];
  const legacy = v.pendingSend && !v.pendingSend.requestID;
  // 发送与回执核对由过程状态承载；这里仅保留待发消息和旧版人工核对入口。
  box.hidden = !list.length && !legacy && !Number.isInteger(v.queueEditIndex);
  box.innerHTML = "";
  const issue = v.queueIssue;
  const editing = Number.isInteger(v.queueEditIndex);
  const repairIndex = issue?.kind === "edit" ? Math.max(0, list.findIndex((q) =>
    q.missingAttachments || (q.atts || []).some((a) => a?.url && !a.data_b64))) : -1;
  const addAction = (parent, label, run) => {
    const action = document.createElement("button");
    action.type = "button"; action.className = "hs-q-action";
    action.textContent = label;
    action.onclick = (e) => { e.stopPropagation(); run(); };
    parent.appendChild(action);
  };
  list.forEach((q, i) => {
    const row = document.createElement("div");
    row.className = "hs-q";
    // 最后一条(队尾,最新排的那条)点击即回输入框重编辑,行为与 ↑ 键完全一致(同一个
    // hsQPop)。原来这条路只有键盘能走,触屏没有物理键位,排错了的排队消息在手机上
    // 只能整条撤销重打,改不了字(2026-08-24 用户实报)。只给最后一条:hsQPop 本身也
    // 只弹队尾那条,点其余几条会跟它的语义对不上——点了却编辑的是另一条。
    if (i === list.length - 1) {
      row.classList.add("hs-q-editable");
      row.title = "点击编辑（同 ↑ 键）";
      row.onclick = (e) => { if (e.target.closest(".hs-q-x")) return; hsQPop(v); };
    }
    const t = document.createElement("span");
    t.className = "hs-q-t";
    t.textContent = q.text || (q.atts && q.atts[0] ? q.atts[0].name : q.missingAttachments ? "附件需重新添加" : "");
    row.appendChild(t);
    if (q.missingAttachments || i === repairIndex) {
      addAction(row, "补充附件", () => hsQEdit(v, i));
    }
    const x = document.createElement("button");
    x.type = "button"; x.className = "hs-q-x"; x.dataset.tip = "取消"; x.textContent = "×";
    x.setAttribute("aria-label", "取消待发消息");
    // 阻止冒泡:队尾那一行现在也挂着 onclick,点 × 不该顺带触发它上面那条编辑逻辑。
    x.onclick = (e) => { e.stopPropagation(); v.queue.splice(i, 1); if (Number.isInteger(v.queueEditIndex) && i < v.queueEditIndex) v.queueEditIndex--; hsQPaint(v); hsDraftSave(v, true); };
    row.appendChild(x);
    box.appendChild(row);
  });
  // 操作跟随具体状态，正常待发清单不占用额外一行。
  if (legacy || (v.queuePaused && (list.length || editing))) {
    const status = document.createElement("div");
    status.className = "hs-q-status";
    const label = document.createElement("span");
    label.textContent = legacy ? "旧消息的发送结果待核对" : editing ? "正在编辑待发消息" :
      issue?.message || "待发消息已暂停";
    status.appendChild(label);
    if (legacy) addAction(status, "保留为草稿", () => hsLegacyDraft(v));
    else if (issue?.kind !== "edit") {
      const actionLabel = editing ? (list.length ? "发送其余消息" : "保留为草稿") :
        issue?.kind === "legacy" ? "确认发送" : "重试";
      addAction(status, actionLabel, () => {
        if (v.interruptRequest || v.pendingSend) return;
        v.queueEditIndex = null;
        hsQResume(v);
      });
    }
    box.appendChild(status);
  }
}

function hsLegacyDraft(v) {
  const pending = v.pendingSend;
  if (!pending || pending.requestID) return;
  if (!confirm("旧版请求没有受理编号，无法确认是否执行。保留为草稿后，请先核对历史，再决定是否重发。")) return;
  v.pendingSend = null; v.sending = null;
  hsQIssue(v, "legacy", "请先核对历史，再确认后续消息");
  hsRestoreUnsent(v, pending.text, pending.atts);
  hsQPaint(v); hsDraftSave(v, true);
}

// queuePaused 只表示需要用户决定；等待连接、状态或问询由系统恢复。
function hsQIssue(v, kind, message) {
  if (["state", "peer", "retry"].includes(kind) && v.queuePaused) return;
  v.queueIssue = kind ? { kind, message } : null;
  v.queuePaused = kind === "action" || kind === "legacy" || kind === "edit";
  hsQPaint(v); if (!v.qSending) hsDraftSave(v, true);
}

function hsQRetry(v) {
  v.queueRetries = (v.queueRetries || 0) + 1;
  v.queueRetryAt = Date.now() + Math.min(30000, 2000 * 2 ** (v.queueRetries - 1));
  hsQIssue(v, v.queueRetries > 5 ? "action" : "retry",
    v.queueRetries > 5 ? "暂时无法发送，请稍后重试" : "等待连接恢复后发送");
}

// 上一条请求尚未收口（request_pending）：不是网络问题，不按指数退避。
// 短暂等待后核对一次状态；后端报 ready 时 hsTurnReady 会重发队列。
function hsQPendingRetry(v) {
  v.queueRetries = (v.queueRetries || 0) + 1;
  const giveUp = v.queueRetries > 5;
  v.queueRetryAt = giveUp ? 0 : Date.now() + 300;
  hsQIssue(v, giveUp ? "action" : "retry",
    giveUp ? "上一轮迟迟未结束，请稍后重试" : "等待上一轮结束后发送");
  hsQPaint(v); hsDraftSave(v, true);
  // ready 可能在 409 返回前后任一时刻到达：已就绪就直接重发，否则问一次状态。
  if (!giveUp) setTimeout(() => {
    if (v.busy) return;
    if (v.turnReady) hsQFlush(v); else hsStateSync(v);
  }, 400);
}

function hsQResume(v) {
  if (!v.queuePaused || v.interruptRequest || v.pendingSend) return;
  v.queueRetries = 0; v.queueRetryAt = 0;
  hsQIssue(v, null);
  return hsQFlush(v);
}

// 上键:把排在队里的最后一条要回输入框重编辑。队空了才轮到翻历史,
// 那是这个键原本的用途——排队的消息比历史更近,先给它。
function hsQPop(v) {
  return hsQEdit(v, (v.queue || []).length - 1);
}

function hsQEdit(v, index) {
  if (index < 0 || !(v.queue || [])[index]) return false;
  if (hsInpText(v.inp).trim() || (v.atts || []).length) {
    hsEnvironment(v, { state: "warning", stage: "delivery", label: "请先处理输入框中的草稿，再编辑待发消息" });
    return false;
  }
  const [q] = v.queue.splice(index, 1);
  v.queueEditIndex = index;
  // 编辑期间保留其余队列，避免后面的内容抢先发送。
  hsQIssue(v, "action", "待发消息正在编辑");
  hsFill(v, q.text);
  if (q.missingAttachments) hsEnvironment(v, { state: "warning", stage: "attachment", label: "请重新添加未能保存的附件，再发送消息" });
  if (q.atts && q.atts.length) { v.atts = q.atts; hsAttPaint(v); }
  hsDraftSave(v, true);
  return true;
}

// Esc 只作用在当前回合:已经跑出内容的是中断,还没出内容的是撤回。
// 排队消息不被取回输入框,仍由队列自己的 × / 上键管理。
// 中断请求返回不代表进程已经停稳；队列仍由 busy/turnReady 栅栏等到权威 ready，
// 然后自动续发。只有请求失败、回执未知或进程重启等无法证明边界的情况才暂停。
async function hsInterrupt(v, keepMessage = false) {
  if (!v.busy) return false;
  if (v.interruptRequest) return true;
  if (v.interrupted && v.interruptDrop) return true;   // 已经发过中断,等后端收尾即可
  const t = v.turns[v.turns.length - 1];
  const fresh = (v.caps || {}).recall !== false && !keepMessage && !!t && !t.parts.size && !(v.queue || []).length;
  if (fresh) v.noDrain = true;
  // 撤回之后到这一回合真正收尾之间,引擎那侧可能还在跑(打断未必立刻落地)。那段时间里
  // 迟到的正文与工具帧不能再画:它们属于一个已经被摘掉的回合,画出来就是「消息回到了
  // 输入框,上面那条却重新转成处理中」。收尾的 ready 会把这个开关关掉(见 hsApply)。
  v.interruptDrop = true;
  if (!fresh) {
    v.interrupted = true;
    hsFlush(v);
    hsFinalize(v);
    hsStatus(v, "done", "已中断");   // UI 立即响应；队列仍等后端 ready 后再发
    hsIndex(v);
    v.interruptAt = Date.now();
  }
  let r = null;
  v.interruptRequest = true;
  try {
    r = await api(`/session/${encodeURIComponent(v.sid)}/abort`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ recall: fresh,
        client_request_id: v.promptDispatch && v.promptDispatch.turn === t
          ? v.promptDispatch.id : (v.sending || {}).requestID || "" }) });
  } catch {
    v.noDrain = false;
    v.interrupted = false;
    v.interruptDrop = false;
    // 中断回执丢失时继续查询权威状态，不重发取消，也不让用户解除执行栅栏。
    v.turnReady = false;
    hsQIssue(v, "state", "正在确认上一轮状态");
    if (!fresh) hsStatus(v, "working", "处理中");
    return false;
  } finally {
    v.interruptRequest = false;
  }
  if (r && r.recall) hsRecallStateSync(v, r);
  if (fresh) {
    if (v.promptDispatch && v.promptDispatch.turn === t) {
      const attempt = v.promptDispatch;
      attempt.recalled = true;
      if ((v.pendingSend || {}).requestID === attempt.id) v.pendingSend = null;
      if ((v.sending || {}).requestID === attempt.id) v.sending = null;
    }
    const text = (r && r.text) || t.user || "";
    // 附件跟着这句话一起要回来:撤回是为了重编辑,把图丢掉等于让人重截一张
    // 重建过的回合里图只剩引用，经 hsAttRestore 取回字节再放进输入框。
    if ((t.atts || []).length) hsAttRestore(v, t.atts);
    t.el.remove();
    v.turns = v.turns.filter((x) => x !== t);
    hsIndex(v);
    hsFill(v, text);
    // 记下是哪一代被撤回的:收尾时按代认领,认不上的一律照常收尾(见 hsTurnSettled)。
    // 代号取 abort 的回执(它就是那一轮的 turn_state),不取本地的 turnGeneration——
    // Esc 抢在这一轮首个状态帧之前时,本地那个还是 hsDispatch 自增出来的临时值。
    v.recallGen = hsStateGeneration(r) || v.turnGeneration || 0;
    v.recallIdle = (v.recallIdle || 0) + 1;
    hsBusy(v, false);
    hsDraftSave(v, true);
  } else {
    hsScheduleBusyVerify(v, HS_INTERRUPT_VERIFY_MS);
  }
  // ready 可能先于 abort HTTP 回执到达；那一帧因 interruptRequest 栅栏没有续发。
  // 回执已经确认且权威状态也已空闲时，在这里补上唯一一次出队。
  if (!v.queuePaused && !v.busy && v.turnReady) hsQFlush(v);
  return true;
}

function hsEscape(v, ev) {
  if (!v || ev.isComposing) return false;
  if (v.slashEl) {
    hsSlashHide(v);
    if (ev) ev.preventDefault();
    return true;
  }
  if (!v.busy) return false;
  if (ev) ev.preventDefault();
  hsInterrupt(v);
  return true;
}

// 填回输入框并把光标放到末尾
function hsFill(v, text) {
  v.inp.textContent = text || "";
  hsCmdPaint(v);
  v.inp.focus();
  const sel = getSelection(), r = document.createRange();
  r.selectNodeContents(v.inp); r.collapse(false);
  sel.removeAllRanges(); sel.addRange(r);
}

// 队列出栈。先摘掉再派发,免得下一拍把同一批又发一遍;但派发没成的话必须放回去——
// 摘掉是界面动作,发出去才是结果,两者之间任何一步出错都不能让消息无声消失
// (2026-08-17 实见:清单里那条不见了,后端存储里却没有对应的用户消息,也没有任何提示)。
async function hsQFlush(v) {
  if (v.queueRecoveryReady) await v.queueRecoveryReady;
  if (v.queueRecoveryBlocked) return;
  // 冷页不会露面也可以续发；只补配置，不恢复整份历史。
  if (v.needConfig) {
    v.queueNeedsConfig = true; v.needConfig = false; v.configReady = hsEngineSync(v);
  }
  if (v.configReady) await v.configReady;
  if (v.queueNeedsConfig && v.shepherdMode === undefined) { v.needConfig = true; return; }
  v.queueNeedsConfig = false;
  // 草稿先于 /config 恢复；模式尚未知时也必须尊重已经持久化的暂停。
  if (v.queuePaused || v.pendingSend) return;
  if (v.interruptRequest) return;
  if (v.recallResolving || (v.recallState && v.recallState.phase !== "done")) return;
  const list = (v.queue || []).filter((q) => (q.text || "") || (q.atts || []).length || q.missingAttachments);
  if (!list.length || v.qSending || v.busy || !v.turnReady) return;
  if (list.some((q) => q.missingAttachments)) {
    hsQIssue(v, "edit", "附件未能完整保存，请编辑消息并重新添加"); return;
  }
  if (v.queueRetryAt > Date.now()) return;
  if (v.queueIssue && v.queueIssue.kind !== "action" && v.queueIssue.kind !== "legacy") hsQIssue(v, null);
  v.qSending = true;
  // 迁回的附件保留后端引用，浏览器存储不必再次容纳大图；发送前才取字节。
  try {
    for (const q of list) {
      if (!(q.atts || []).some(a => a?.url && !a.data_b64)) continue;
      const hydrated = await Promise.all((q.atts || []).map(hsAttHydrateOne));
      if (hydrated.some(a => !a)) throw Error("待发附件未能取回，请重新添加");
      q.atts = hydrated;
    }
  } catch (error) {
    v.qSending = false; hsQIssue(v, "edit", error.message); return;
  }
  // 附件下载期间可能按了编辑/取消/中断，重新核对原队列及执行栅栏。
  if (v.queuePaused || v.pendingSend || v.interruptRequest || v.busy || !v.turnReady ||
      list.length !== (v.queue || []).length || list.some((q, i) => v.queue[i] !== q)) {
    v.qSending = false; return;
  }
  v.queue = [];
  hsQPaint(v);
  // 保留持久队列，直到 hsDispatch 将其原子替换为带请求编号的发送存根。
  const text = list.map((q) => q.text || "").filter(Boolean).join("\n\n");
  const atts = [];
  list.forEach((q) => atts.push(...(q.atts || [])));
  let ok = false, err = null;
  try { ok = await hsDispatch(v, text, atts); } catch (e) { err = e; }
  if (!ok) {
    v.queue = list.concat(v.queue || []);   // 放回原位,人还能改、还能撤、下一拍再试
    if (!v.queueIssue) hsQIssue(v, "action", "消息未发送，请检查后重试");
    hsQPaint(v);
    hsDraftSave(v, true);
    // 半路出错时 hsDispatch 已经把界面切成「处理中」了(它在 POST 之前就切),
    // 不还原的话这一页会一直转,而根本没有回合在跑
    if (err) { hsBusy(v, false); hsErrorNotice(v, "队列发送失败：" + (err.message || err), "delivery"); }
  }
  v.qSending = false;
}

async function hsSend(v) {
  if (v.queueRecoveryReady) await v.queueRecoveryReady;
  if (v.queueRecoveryBlocked && !(await hsRecoverServerQueue(v))) return;
  // 露面即可输入，但 Shepherd/普通会话的判定来自异步 /config。先等它，避免把本应带
  // engine 的消息按普通会话发出，也避免斜杠命令在错误分支执行。
  if (v.configReady) await v.configReady;
  if (v.queuePaused && v.shepherdMode === undefined) {
    hsEnvironment(v, { state: "warning", stage: "delivery",
      label: "会话配置尚未读取，暂停的队列和草稿已保留，请稍后发送" });
    return;
  }
  if (v.pendingSend && !v.busy) {
    hsEnvironment(v, { state: "warning", stage: "delivery",
      label: "上一条消息是否送达尚未确认，请等待核对；当前草稿已保留" });
    return;
  }
  if (v.recallResolving || (v.recallState && v.recallState.phase !== "done")) {
    hsRecallPanel(v); return;
  }
  // 撤回刚把附件往回取时就按了发送：等取完再读 v.atts，否则这一条会少图。
  if (v.attRestoring) await v.attRestoring;
  // 没有字节的附件发出去必然失败或丢图。先尝试按引用取回；仍取不回就不发，留给用户处理。
  if ((v.atts || []).some((a) => !a || !a.data_b64)) {
    const pending = v.atts;
    v.atts = [];
    hsAttPaint(v);
    if (await hsAttRestore(v, pending)) return;
  }
  const text = hsInpText(v.inp).trim();
  const atts = (v.atts || []).slice();
  if (!text && !atts.length) return;
  if (v.materializing) return;   // 双击发送只允许一次创建，避免落出两段空会话
  // 这一条已经发走,它就不再是「翻历史之前手上那段」了。三条分支都要过这里:
  // 不清的话,发完直接按 ↑ 再按 ↓,会把早已发出去的那段草稿又填回来。
  v.histIdx = -1; v.histDraft = "";
  // 附件在场时不认本地命令:那多半是想把命令当正文发。控制类除外——打断这件事与
  // 挂没挂图无关,而挂着图就打不了断说不通。它们不清 v.atts,图留在框里等下一次发。
  const localCmd = hsSlashLookup(v, text);
  // Keep a typed trial /compact on the product-owned route even when the
  // selected engine cannot yet produce a restorable unified checkpoint.
  if (!localCmd && v.shepherdTrial && /^\/compact\s*$/.test(text)) {
    v.inp.textContent = "";
    hsSlashHide(v);
    hsDraftSave(v, true);
    hsCompact(v);
    return;
  }
  if (localCmd && (localCmd.cmd.ctl || !atts.length)) {
    v.inp.textContent = "";
    hsSlashHide(v);
    hsDraftSave(v, true);
    hsRunLocalSlash(v, localCmd.cmd, localCmd.args);
    return;
  }
  // 与后端 command_manifest.command_token 同一口径：只有开头整段是命令名才算命令。
  // 单独的“/”、“/ 文字”、“/tmp/a.txt” 这类绝对路径都是正文，照常发送。
  const shepherdCmd = v.shepherdMode && /^\/[\w:-]+(?=\s|$)/.exec(text);
  if (shepherdCmd) {
    hsErrorNotice(v, shepherdCmd[0] + " 是斜杠命令，Shepherd 不把它作为消息转交原生引擎；可用命令见输入框的“/”补全", "command");
    return;
  }
  // 面板还挂着时打的字,是在回答那个问题,不是新起一轮:引擎那边认 response 这一路,
  // 结果会写成「The user responded: …」。不这么接的话,这句会变成排队消息,
  // 而问题仍卡在那里等人。
  if (v.ask && !v.ask.permission) {
    v.inp.textContent = "";
    hsDraftSave(v, true);
    hsAskSend(v, {}, text);
    return;
  }
  // 本地命令不产生聊天记录；真正要发给引擎的第一条内容到这里才创建会话。
  // 创建失败时保留输入与附件，用户可直接重试。
  if (v.ephemeral && !(await hsMaterializeDraft(v))) return;
  v.inp.textContent = "";
  v.atts = []; hsAttPaint(v);
  if (Number.isInteger(v.queueEditIndex)) {
    (v.queue = v.queue || []).splice(Math.min(v.queueEditIndex, v.queue.length), 0, { text, atts });
    v.queueEditIndex = null; v.queueRetries = 0; v.queueRetryAt = 0;
    hsQIssue(v, null); await hsQFlush(v); return;
  }
  if (v.busy || !v.turnReady || (v.queue || []).length) {
    (v.queue = v.queue || []).push({ text, atts });
    hsQPaint(v);
    hsDraftSave(v, true);
    if (!v.busy && v.turnReady) await hsQFlush(v);
    return;
  }
  if (v.queuePaused) { v.queueRetries = 0; v.queueRetryAt = 0; hsQIssue(v, null); }
  hsDraftSave(v, true);
  // 直接发的这一路把话留在了输入框之外,出错时至少要说一声:静默的 rejection 会让
  // 界面停在「处理中」,人以为在跑
  hsDispatch(v, text, atts).then((ok) => {
    // hsDispatch 自己吞掉 POST 的异常并回 false(状态已经改成「发送失败」),所以这一句
    // 要看返回值,不能只挂 catch——只挂 catch 的话失败时什么都不做,话就没了。
    if (!ok) hsRestoreUnsent(v, text, atts);
  }).catch((e) => {
    hsBusy(v, false);
    hsErrorNotice(v, "发送失败：" + ((e && e.message) || e), "delivery");
    hsStatus(v, "error", "已停止");
    hsRestoreUnsent(v, text, atts);
  });
}

async function hsDispatch(v, text, atts) {
  if (v.configReady) await v.configReady;   // 队列、重试等非 hsSend 调用方也守同一道配置闸
  if (v.recallResolving || (v.recallState && v.recallState.phase !== "done")) {
    hsRecallPanel(v); return false;
  }
  const previousGeneration = v.turnGeneration;
  v.interruptDrop = false;  // 新回合开始后,旧回合的迟到帧不该继续影响后续输出
  // 状态查询与 SSE 都可能迟到。先把代际推进到「至少下一轮」，旧回合的 ready 即使在
  // POST 之后才回来，也过不了 hsStateTake。这里只沿用后端上一轮序号递增，不能拿浏览器
  // Date.now() 作下界：远端访问时浏览器与 Desk 服务可能不在同一台机器、时钟也不同。
  v.turnGeneration = Number(v.turnGeneration || 0) + 1;
  v.turnReady = false;      // 后端发布这一轮的 ready 之前，后续输入只能排队
  if (!v.turns.length) hsRename(v, text || atts[0].name);
  hsStatusClear(v);          // 清掉上一回合的终态行
  // 用量与待办属于回合,在这里清而不是在建回合里清:回合末的对账会重建整棵 DOM、
  // 再走一遍建回合的路径,写在那儿会把刚显示出来的耗时与 token 一起抹掉。
  v.stats = null;
  v.todoNow = v.todoNext = "";
  const t0 = hsNewTurn(v, text, "", atts);
  // 这一轮是自己发的,正等后端回一个 id。认领判据靠它,见 hsApply 的 message.updated。
  // 只在这条真正会产生后端用户消息的路上打:/compact 那一轮后端不经 SSE 发消息,
  // 不打标记它就不会去认领别人的 id。
  t0.mine = true;
  // /goal 的那一行当场就画出来,不等这一回合收尾后的对账。目标是这句话立刻产生的结果,
  // 隔着一整轮才出现,人会以为没设上。收尾重建时它会按记录再画一次,位置一致。
  const g = /^\/goal\s+([\s\S]+)$/.exec(text || "");
  if (g) {
    const pending = { condition: g[1].trim(), met: false, before: v.goalSig,
                      expires: Date.now() + 120000 };
    v.goalPending = pending;
    hsGoalRow(v, pending, t0);
    hsGoalPaint(v, pending);   // Codex 要等模型调用 create_goal；等待期间先显示用户刚设的条件
  }
  hsBusy(v, true);
  hsSetStick(v, true);
  hsTouch(v);
  hsStatus(v, "working", "发送中");
  hsScroll(v);
  // Claude 会很快写 goal attachment；Codex 则由模型稍后调用 create_goal,长会话里可能要
  // 数十秒。两边共用同一条同步契约,这里只按递增间隔等后端的权威记录。
  if (/^\/goal\b/.test(String(text || "").trim())) {
    [800, 2500, 6000, 15000, 30000, 60000].forEach((ms) => setTimeout(() => hsGoalSync(v), ms));
  }
  // 返回「有没有真的发出去」。队列那一路靠它决定要不要把消息放回清单:发失败却当成
  // 发成功,消息就从界面上蒸发了,而后端一无所知。
  // 先把这一句落进草稿再发。直发与队列出队两条路都经过这里,守在这一处就够。
  // 收不到回执时它是唯一的存根:页面被关掉、请求失败,重开都还找得回来。
  v.sending = { text, atts };
  const attempt = {
    id: "prompt_" + Date.now().toString(36) + "_" + Math.random().toString(36).slice(2),
    turn: t0, previousGeneration, generation: v.turnGeneration,
    sending: v.sending, rejected: false,
  };
  if (attempt) v.promptDispatch = attempt;
  if (attempt) v.sending.requestID = attempt.id;
  hsDraftSave(v, true);
  try {
    await api(`/session/${encodeURIComponent(v.sid)}/prompt_async`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ parts: [{ type: "text", text }, ...atts],
        client_request_id: attempt.id,
        ...(v.shepherdMode ? { engine: v.engine } : {}) }), timeout: 60000 });
    if (attempt && (attempt.rejected || attempt.recalled || v.promptDispatch !== attempt)) return true;
    v.queueRetries = 0; v.queueRetryAt = 0;
    v.pendingSend = { ...attempt.sending }; // All GUI engines verify durable acceptance across restart.
    if (v.busy && v.statusLabel === "发送中") hsStatus(v, "working", "处理中");
    hsQPaint(v);
    hsDraftSave(v, true);
    return true;
  } catch (e) {
    if (attempt && (attempt.rejected || attempt.recalled || v.promptDispatch !== attempt)) return true;
    const inputRejected = e && ["invalid_image", "invalid_attachment", "turn_input_too_large", "prompt_body_too_large", "image_transport_too_large"].includes(e.code);
    if (e && (e.code === "recall_pending" || e.code === "exp_conflict"
              || inputRejected || e.code === "delivery_uncertain"
              || e.code === "delivery_cleanup")) {
      // 服务端明确未接收，不再按文本与上一条同文消息比对，草稿由调用方恢复。
      v.turnGeneration = previousGeneration;
      t0.el.remove(); v.turns = v.turns.filter((t) => t !== t0); hsIndex(v);
      if (g) { v.goalPending = null; hsGoalSync(v); }
      v.sending = null; hsBusy(v, false); v.turnReady = true;
      const deliveryBlocked = e.code === "delivery_uncertain" || e.code === "delivery_cleanup";
      if (e.code === "recall_pending") hsQIssue(v, "state", "等待撤回处理完成");
      else if (deliveryBlocked) hsQIssue(v, "state", "正在核对上一轮执行结果");
      else hsQIssue(v, inputRejected ? "edit" : "action", inputRejected ? "输入无法发送，请编辑消息或减少附件" : "消息未发送，请检查会话状态");
      if (!v.qSending) hsDraftSave(v, true);
      if (e.code === "recall_pending") {
        hsRecallStateSync(v, { recall: (e.data || {}).recall });
        hsRecallPanel(v);
      } else if (deliveryBlocked) {
        hsStatusClear(v);
        const delivery = (e.data || {}).delivery;
        if (delivery) hsDeliveryBlocked(v, delivery);
        else hsStateSync(v);
      } else if (inputRejected) {
        v.noDrain = true;
        hsStatusClear(v);
        hsEnvironment(v, { state: "warning", stage: "attachment",
          label: "消息未发送：" + (e.detail || e.message) });
      } else {
        hsStatusClear(v);
        hsErrorNotice(v, "消息未发送：" + e.message, "delivery");
      }
      return false;
    }
    if (e && e.code === "request_pending") {
      // 服务端明确未接收：上一条请求还没收口。撤掉乐观回合，原样放回队首，
      // 等后端再报 ready 时由 hsTurnReady 重发；不走 hsSentOrNot，也不显示“已停止”。
      t0.el.remove(); v.turns = v.turns.filter((t) => t !== t0); hsIndex(v);
      v.turnGeneration = previousGeneration; v.promptDispatch = null;
      if (g) { v.goalPending = null; hsGoalSync(v); }
      v.sending = null; hsBusy(v, false); v.turnReady = false;
      if (!v.qSending) (v.queue = v.queue || []).unshift({ text, atts });
      hsQPendingRetry(v);
      return !v.qSending;
    }
    // 请求失败 ≠ 没发出去。prompt_async 是先起回合、再回 204:204 在隧道里丢掉时
    // 回合已经在跑了。当成「没发出去」把话退回输入框,那条消息其实还留在会话里,
    // 人再按一次就发了两遍。问一句后端,别猜。
    attempt.sending.autoRetry = !e.status || [408, 429, 500, 502, 503, 504].includes(e.status);
    const evidence = {};
    const got = await hsSentOrNot(v, text, evidence);
    if (attempt.rejected || attempt.recalled || v.promptDispatch !== attempt) return true;
    if (got === "sent") {
      v.queueRetries = 0; v.queueRetryAt = 0;
      v.sending = null;
      hsDraftSave(v, true);
      // 后端收下了,这一轮就照常走。状态交回给控制面:回执丢了而已,回合是好的。
      hsStateSync(v);
      return true;
    }
    if (got === "unknown") {
      // 问不到——网络还没回来。这时两种做法都会错一次:退回输入框可能变成发两遍,
      // 当成发成功可能永远等一个不存在的回合。选择不动界面、把存根留在草稿里,
      // 并说清楚现在是什么处境;等能问到的时候再判(见 hsFollowTick 那一路的复核)。
      v.pendingSend = { ...attempt.sending };
      hsBusy(v, false);
      v.turnReady = true;
      hsStatus(v, "working", "发送中");
      hsQPaint(v); hsDraftSave(v, true);
      return true;   // 不当失败:调用方一退回,话就落到输入框或队列里,那正是要避免的
    }
    const transient = !e.status || [408, 429, 500, 502, 503, 504].includes(e.status);
    if (transient && evidence.retryable) {
      // 旧请求已被持久取消；迟到 POST 不能再执行，才可让新请求自动重试。
      t0.el.remove(); v.turns = v.turns.filter((t) => t !== t0); hsIndex(v);
      v.turnGeneration = previousGeneration; v.promptDispatch = null;
      v.sending = null; hsBusy(v, false); v.turnReady = false;
      if (!v.qSending) (v.queue = v.queue || []).unshift({ text, atts });
      hsQRetry(v);
      return !v.qSending; // 队列路径由 hsQFlush 原位放回；直发路径已转入待发区。
    }
    t0.mine = false;   // 确认没发出去,后端不会有这条消息;标记留着会让它认领别处那一句
    if (g) { v.goalPending = null; hsGoalSync(v); }
    hsBusy(v, false);
    v.turnReady = true;
    hsErrorNotice(v, "发送失败：" + (e && e.message ? e.message : e), "delivery");
    hsStatus(v, "error", "已停止");
    hsQIssue(v, "action", "消息未发送，请检查错误后重试");
    // 存根转交给调用方各自的去处:直发那路回输入框,队列那路回队列(见 hsQFlush)。
    // 这里先撤掉,免得两处都放一份。
    v.sending = null;
    if (!v.qSending) hsDraftSave(v, true);
    return false;
  }
}

// 仅处理服务端明确“尚未开跑”的拒绝，不按正文猜测，也不把执行失败当作未发送。
function hsPromptRejected(v, p) {
  if ((p.error || {}).code !== "prompt_not_started") return false;
  const attempt = v.promptDispatch;
  if (!attempt || !p.requestID || attempt.id !== p.requestID || attempt.rejected || attempt.recalled) return true;
  const turn = attempt.turn;
  if (turn.uid || v.turnGeneration !== attempt.generation) return true;
  attempt.rejected = true;
  turn.mine = false;
  turn.el.remove(); v.turns = v.turns.filter((t) => t !== turn); hsIndex(v);
  v.turnGeneration = attempt.previousGeneration;
  if (v.sending === attempt.sending) v.sending = null;
  if ((v.pendingSend || {}).requestID === attempt.id) v.pendingSend = null;
  if (v.goalPending) { v.goalPending = null; hsGoalSync(v); }
  hsBusy(v, false); v.turnReady = true; v.noDrain = true;
  hsQIssue(v, "action", "消息未发送，请处理上方提示");
  hsQPaint(v);
  hsStatusClear(v);
  hsEnvironment(v, { state: "warning", stage: (p.error || {}).stage || "switch",
    label: "消息未发送：" + ((p.error || {}).message || "引擎切换未完成") });
  hsRestoreUnsent(v, attempt.sending.text, attempt.sending.atts);
  return true;
}

// 按请求 ID 查询持久回执；not_found 必须先写取消墓碑，才能排除迟到 POST。
// evidence.retryable 只证明未派发且本次恢复可重试，不把明确拒收或用户取消重放。
async function hsSentOrNot(v, text, evidence = {}) {
  const rid = (v.pendingSend || {}).requestID || (v.sending || {}).requestID;
  if (rid) {
    try {
      const path = `/session/${encodeURIComponent(v.sid)}/request/${encodeURIComponent(rid)}`;
      let r = await api(path, { timeout: 8000 });
      const recovered = r.status === "rejected" && (r.retryable === true
        || ["restart_before_store", "restart_before_dispatch"].includes(r.reason));
      if (r.status === "not_found" || recovered) {
        // 先留下取消回执，再归还草稿；迟到 POST 不能在用户重发后突然执行。
        // 若请求刚好已开跑，服务端只返回原状态，不中断它。
        r = await api(path + "/cancel-unsent", { method: "POST", timeout: 8000 });
        evidence.retryable = r.status === "cancelled" && r.retryable === true;
      }
      evidence.receipt = r;
      if (["stored", "dispatched", "started", "finished", "uncertain"].includes(r.status)) return "sent";
      if (["rejected", "cancelled"].includes(r.status)) return "not-sent";
      return "unknown"; // accepted / not_found 都不能证明没有迟到的 POST。
    } catch { return "unknown"; }
  }
  return "unknown"; // 旧草稿没有请求 ID，任何引擎都不能按忙闲或同文历史猜测。
}

// 没发出去的那一句放回输入框。框里已经有别的东西就接在前面,不覆盖——人可能在等待
// 期间又打了一句,把它冲掉等于丢第二次。
function hsRestoreUnsent(v, text, atts) {
  if (!v || !v.inp) return;
  const now = hsInpText(v.inp);
  const back = String(text || "");
  if (back && now.indexOf(back) < 0) {
    v.inp.textContent = back + (now ? "\n\n" + now : "");
    v.draftFresh = true;
    if (typeof hsCmdPaint === "function") hsCmdPaint(v);
  }
  if ((atts || []).length) {
    v.atts = (atts || []).concat(v.atts || []);
    if (typeof hsAttPaint === "function") hsAttPaint(v);
  }
  hsDraftSave(v, true);
}

// 会话改名:后端在首条消息时也会给会话定标题,这里同步到页头与落盘记录,免得一直叫「对话」
function hsRename(v, text) {
  const name = String(text).replace(/\s+/g, " ").slice(0, 20);
  if (!name) return;
  const pg = pages.find((x) => x.url === HS_PAGE(v.sid));
  if (!pg || pg.title !== "对话") return;
  pg.title = name;
  persistPages();
  if (typeof pActive !== "undefined" && pActive === pg) setOverlayTitle(pg);
}
