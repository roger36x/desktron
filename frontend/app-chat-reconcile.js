// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 7/13 片
// ---- 回合末对账自愈 --------------------------------------------------------
// idle 之后用引擎的权威转录重建一次,流式期间的任何漂移(乱序 / 丢帧 / 串台)活不过一个回合。
// 保护:绝不用更穷的记录覆盖更富的界面——引擎中途崩溃没落盘时,界面反而是完整的那一份。

// 两边的口径要一致:界面那份是流式收到的全文,/message 那份把长的工具输出与思考
// 截断过并标出全长(outputMore / textMore,见 cli_store._trim_outputs)。有全长标记时按
// 全长算,否则凡是回合里出过长输出就会在收尾时误判成漂移、整页重建一次。
//
// 工具入参不参与 shape。实时事件保留执行器外壳,历史重放会把同一调用归一化成展示字段；
// 两份语义相同但字符长度不同。拿这种表示差异对账,会让每轮完成都 replaceChildren 整页,
// 即便正文完全相同也造成视口跳动。工具是否缺失由 tools 数量判断,执行结果是否缺失由
// output 长度判断。
function hsProcessShape(parts) {
  const s = { text: 0, reasoning: 0, tools: 0, output: 0, returns: 0, returnText: 0, returnMeta: "", approvals: "", compacts: "" };
  for (const q of parts || []) {
    if (q.type === "compact") {
      const m = q.compact || {};
      s.compacts += JSON.stringify([q.text || "", m.trigger || "", m.pre || 0, m.post || 0]);
    }
    if (q.type === "execution-result") {
      s.returns++;
      s.returnText += q.textMore || String(q.text || "").length;
      s.returnMeta += JSON.stringify([q.status || "", q.source || {}, q.exit_code ?? null]);
    }
    else if (q.type === "approval") s.approvals += JSON.stringify([q.id, q.resolution]);
    else if (q.type === "text") s.text += String(q.text || "").length;
    else if (q.type === "reasoning") s.reasoning += q.textMore || String(q.text || "").length;
    else if (q.type === "tool") {
      s.tools++;
      const st = q.state || {};
      s.output += st.outputMore || String(st.output || "").length;
      // 回填到这一步的返回也算:实时先有、记录重建后也有,两边都数,才不会判成漂移
      if (q.returned) s.output += q.returnedMore || String(q.returned.text || "").length;
    }
  }
  return s;
}

function hsShapeSame(a, b) {
  return a.text === b.text && a.reasoning === b.reasoning
    && a.tools === b.tools && a.output === b.output
    && a.returns === b.returns && a.returnText === b.returnText && a.returnMeta === b.returnMeta
    && a.approvals === b.approvals && a.compacts === b.compacts;
}

// 自续轮那条消息在记录里 role=user,但它的 parts 是引擎的产出,界面上并进上一个回合、
// 照普通回合铺开(见 hsAsideFill)。所以这里要数进去——不数,界面比记录「富」出一整轮,
// 对账那侧每个回合都判成漂移,收尾就整页重建一次。
function hsMessageShape(msgs) {
  const parts = [];
  for (const m of msgs || []) {
    const info = m.info || {};
    if (info.compact) parts.push({ type: "compact", compact: info.compact,
      text: (m.parts || []).filter(p => p.type === "text").map(p => p.text || "").join("") });
    if (info.role === "assistant" || info.role === "tool" || info.aside) parts.push(...(m.parts || []));
  }
  return hsProcessShape(parts);
}

function hsViewShape(v) {
  const parts = [];
  for (const t of v.turns) parts.push(...t.parts.values());
  return hsProcessShape(parts);
}

function hsReconcile(v) {
  if (v.recTimer) clearTimeout(v.recTimer);
  v.recTimer = setTimeout(async () => {
    v.recTimer = 0;
    await hsReconcileNow(v);
  }, 600);
}

async function hsReconcileNow(v, opts) {
  if (!v.el.isConnected) return false;
  // 首屏历史还在分批向前补时，v.turns 只是 /message 的一个尾部子集。拿部分 DOM 与完整
  // 记录对账必然判成漂移并整页重建；Codex 的过程区更重，更容易撞进这个窗口。
  if (!v.historyReady || v.pendRest) { v.recAfterLoad = true; return false; }
  // 回合跑着时不重建(会和流式写入打架),但要记下欠着这一次——收尾时补。
  // 只挡不记的话,断线窗口里丢掉的内容就永久缺失了。
  if (v.busy) { v.recOwed = true; return false; }
  if (hsHidden(v)) { v.followLater = true; return false; }   // 盖住时不重建,露出来再说
  if (v.paged) return false;
  // 请求返回时仍须属于同一份视图、同一回合。只复查 busy 不够：短回合可能在请求
  // 期间已经开始并结束，或用户已重新加载历史。并发对账只允许最后一次提交。
  const token = v.recToken = (v.recToken || 0) + 1;
  const turns = v.turns, generation = v.turnGeneration, eventAt = v.lastEventAt;
  const anchorToken = v.anchorToken;
  const adopt = !opts || opts.adopt !== false;
  // 认领过的会话(有引擎锚点)先从 transcript 重建一次再读。引擎的记录是权威:我们这侧
  // 既会在认领时导入(id 用 uuid),又会在实跑后自己追加(id 用时间戳),两套 id 互不相认,
  // 同一回合于是留下两条、界面上重复渲染。以 transcript 为准重建,重复从构造上就不存在。
  const esid = adopt ? await hsEngineSid(v).catch(() => "") : "";
  if (esid) {
    try {
      // 带上本会话的 id:搬迁按引擎选适配器,而当前偏好的引擎未必是这段会话的归属,
      // 切过引擎之后不说明就会拿错的读法去找记录。
      await api("/session/adopt", { method: "POST", headers: { "content-type": "application/json" },
                                    body: JSON.stringify({ engine_sid: esid, sid: v.sid }),
                                    timeout: 30000 });
    } catch {}
  }
  let msgs = null;
  try { msgs = await api(`/session/${encodeURIComponent(v.sid)}/message`); } catch { return false; }
  // 取数这几步(hsEngineSid / adopt / message)都是网络往返,手机经隧道要几秒。
  // 这期间下一个回合一开始,busy 就回到真,整次重建在这里被丢弃——而它正是要补齐
  // 断线窗口里丢掉的那截内容。连续对话时尤其容易撞上(2026-09-03 用户实见:
  // 显示已完成但正文只有几行,退出重开才是完整的)。先记下来,复现时看得见。
  if (!Array.isArray(msgs)) return false;
  if (v.busy) {
    // 丢弃是对的:重建会和正在写的流打架。错的是丢完就不再补——那截内容于是永久缺失,
    // 只能退出重开(hsLoad 冷加载不受 busy 限制,所以那条路总是完整的)。
    // 记一笔欠账,回合收尾时补上(见 hsTurnSettled 里的 v.recOwed)。
    v.recOwed = true;
    if (typeof deskLog === "function") {
      deskLog(`[reconcile] 取完数又忙了,这次丢弃、记欠账 sid=${(v.sid || "").slice(0, 8)}`);
    }
    return false;
  }
  if (!v.el.isConnected || hsHidden(v) || v.paged) return false;
  if (!v.historyReady || v.pendRest) { v.recAfterLoad = true; return false; }
  if (token !== v.recToken) return false;
  if (turns !== v.turns || generation !== v.turnGeneration || eventAt !== v.lastEventAt
      || anchorToken !== v.anchorToken) {
    hsReconcile(v);
    return false;
  }
  const dbShape = hsMessageShape(msgs), domShape = hsViewShape(v);
  // 正常收尾时实时视图与落盘投影内容相同。此前仍无条件清空并重画整页，表现为过程与正文
  // 一起消失后再闪现。只有结构确实漂移时才重建。
  if (hsShapeSame(dbShape, domShape)) return false;
  let dbChars = 0;
  for (const m of msgs) {
    const info = m.info || {};
    if (info.role !== "assistant" && !info.aside) continue;   // 自续轮同上,它也在界面上
    for (const q of m.parts || []) if (q.type === "text") dbChars += (q.text || "").length;
  }
  let domChars = 0, uniqueChars = 0;
  const seen = new Set();
  let dup = false;
  for (const t of v.turns) {
    const texts = [...t.parts.values()].filter((p) => p.type === "text")
      .map((p) => String(p.text || "") + String(p.buf || ""));
    const n = texts.reduce((sum, text) => sum + text.length, 0);
    domChars += n;
    // 字数相同不代表消息重复。只有同一消息身份、提问和全文均相同才可扣除副本；
    // 即使确有重复，也必须保护其余独立回合，不能借一次去重擦掉另一次完整回复。
    const id = t.msgId || t.uid;
    const key = id && JSON.stringify([id, t.user || "", texts]);
    if (key && seen.has(key)) dup = true;
    else uniqueChars += n;
    if (key) seen.add(key);
  }
  // 现场取证:「从后台回来当前回合整个消失」这一现象查到这里,两道守卫单看都成立,
  // 推演不出能擦掉内容的路径(2026-09-03)。与其继续猜,不如让复现时留下实据——
  // 重建会换掉界面上已有的内容,那一刻的字数对比与 dup 判定是唯一能定位它的东西。
  // 只在「界面上原本有内容」时记:空页重建是正常开局,记了只是噪声。
  if (domChars > 0 && typeof deskLog === "function") {
    deskLog(`[reconcile] 重建 sid=${(v.sid || "").slice(0, 8)} 记录=${dbChars} 界面=${domChars}`
      + ` 差=${dbChars - domChars} dup=${dup} busy=${!!v.busy}`
      + ` 拦=${dbChars < uniqueChars - 8 ? "是" : "否"}`);
  }
  if (dbChars < uniqueChars - 8) return false;   // 记录比界面穷,疑似引擎丢内容,不覆盖
  // 上面那道只数助手正文。回合失败时只多出一条用户消息、助手一个字都没有,字数相同,
  // 于是守卫放行、重建把刚打的那句话抹掉(codex 报 active writer 时实见)。
  // 人说过几句话是我们自己知道的事,记录比它少就不能覆盖。
  const dbAsk = msgs.filter((m) => (m.info || {}).role === "user" && !(m.info || {}).compact
                                    && !(m.info || {}).goal && !(m.info || {}).aside).length;
  const domAsk = v.turns.filter((t) => t.user).length;
  if (dbAsk < domAsk) return false;
  hsRebuild(v, msgs);
  return true;
}

function hsReconcileAfterLoad(v) {
  if (!v.recAfterLoad || !v.historyReady || v.pendRest) return;
  v.recAfterLoad = false;
  if (!v.paged) hsReconcile(v);
}

function hsRebuild(v, msgs) {
  return hsViewportChange(v, () => {
    const expanded = new Set(v.turns.filter((t) => t.el.querySelector(".hs-u-open"))
      .map((t) => JSON.stringify([t.uid, t.user])));
    const status = { state: v.statusState, label: v.statusLabel, tail: v.statusTail };
    const real = v.stream;
    const scratch = document.createElement("div");
    hsStatusClear(v);
    v.stream = scratch;
    v.turns = []; v.domTrimmed = false;
    v.roles = new Map();
    // 自续轮的认领表指着旧的那批回合对象,整页重建之后它们已经不在文档里了
    v.asideOf = new Map();
    hsPour(v, msgs);
    real.replaceChildren(...scratch.childNodes);
    v.stream = real;
    for (const t of v.turns) {
      if (expanded.has(JSON.stringify([t.uid, t.user]))) t.el.querySelector(".hs-bubble")?.classList.add("hs-u-open");
    }
    hsIndex(v);
    // done 已由 hsPour 从消息 info.ms 建成静态历史行；再放回旧的活状态会把它遮住，
    // 而旧状态依赖 v.stats，刷新后通常没有数据，于是耗时看起来消失。
    if (status.state && status.state !== "done") hsStatus(v, status.state, status.label, status.tail);
    // 审批卡是账本投影，不属于引擎消息。replaceChildren 会把它一并摘掉，重建完成后必须
    // 按稳定回合锚点挂回，否则一次正常对账就会让信息卡与正文中的同一张审批同时消失。
    if (typeof apprRenderView === "function") apprRenderView(v);
  }, true, "rebuild");
}

// 把一份转录倒进视图。历史回填与对账重建共用,不写两套渲染。
// 历史回合的顶行。活的那一回合由 hsStatus 写(它带着阶段与秒表),历史没有,
// 于是这里补一条静态的「已处理」加落盘的耗时——刷新之后那一行不该只剩一个箭头。
// 既没有思考也没有工具调用的回合,收尾后把这一条整个撤掉:它此刻只剩一句「已处理」和
// 一条分隔线,而没有任何可展开的东西——过程为空时,这一行讲不出信息,只是占位。
// 跑动期间不撤,那时它是唯一的反馈。
function hsProcTrim(t) {
  const box = t && t.procEl;
  if (!box) return;
  if (box.querySelector(".hs-proc-body").children.length) return;
  box.remove();
  t.procEl = null;
}

function hsProcDone(t, ms) {
  const has = [...t.parts.values()].some((q) => q.type === "tool" || q.type === "reasoning" || q.type === "approval");
  if (!has) return;   // 过程为空的历史回合不补这一条
  const box = hsProcBox(t);   // 没有工具调用的回合还没有这一条,补建
  const slot = box.querySelector(".hs-proc-slot");
  if (!slot || slot.firstChild) return;
  const dur = ms ? hsClock(ms, true) : "";
  const el = document.createElement("div");
  el.className = "hs-status st-done";
  el.innerHTML = '<span class="hs-dot"></span><span class="hs-st-t"></span>';
  // 与实时那条路同一个词。终态就是终态,不该因为「谁在什么时候渲染」而改叫法:
  // 刚跑完叫「已完成」、刷新之后同一回合变成「已处理」,是同一件事两个名字。
  hsStTxt(el.querySelector(".hs-st-t"), "已完成", dur ? " " + dur : "");
  slot.appendChild(el);
}

function hsProcFailed(t, ms) {
  const box = hsProcBox(t);
  const slot = box.querySelector(".hs-proc-slot");
  if (!slot || slot.firstChild) return;
  const dur = ms ? hsClock(ms, true) : "";
  const el = document.createElement("div");
  el.className = "hs-status st-error";
  el.innerHTML = '<span class="hs-dot"></span><span class="hs-st-t"></span>';
  hsStTxt(el.querySelector(".hs-st-t"), "已停止", dur ? " " + dur : "");
  slot.appendChild(el);
}

function hsProcInterrupted(t, ms) {
  const box = hsProcBox(t);
  const slot = box.querySelector(".hs-proc-slot");
  if (!slot || slot.firstChild) return;
  const dur = ms ? hsClock(ms, true) : "";
  const el = document.createElement("div");
  el.className = "hs-status st-done";
  el.innerHTML = '<span class="hs-dot"></span><span class="hs-st-t"></span>';
  hsStTxt(el.querySelector(".hs-st-t"), "已中断", dur ? " " + dur : "");
  slot.appendChild(el);
}

// 首屏分两批铺。一段长会话的记录有几 MB,五十个回合各要过一遍 markdown 渲染与代码高亮,
// 一次铺完那段时间里页面是空的——切引擎之所以慢,时间几乎全花在这里,与引擎冷热无关。
// 视口停在末尾,所以先把看得见的那几个回合铺出来交给浏览器绘制,更早的等空闲帧再补进去,
// 补的时候走向上翻页同一条路(离屏铺好、插到最前、按高度差把滚动位置补回来),画面不跳。
const HS_FIRST_TURNS = 4;

function hsHistoryTurnStart(m) {
  return (m.info || {}).role === "user"
    || ((m.parts || [])[0] || {}).type === "execution-result";
}

function hsPourFast(v, msgs) {
  const list = msgs || [];
  // 切在这条用户消息「之前」,不是之后。切在之后的话,它留在前一批、它的回答分到后一批,
  // 而后一批先铺:回答找不到自己的用户回合,hsTurnFor 只好另开一个没有提问的回合,前一批
  // 又留下一个没有回答的回合——同一次交换被拆成两轮。每个批次边界都拆一对,而 hsPour
  // 一次性铺同一份记录时是合并的。两种铺法结构不同,此后任何一次对账重建都要重排整页,
  // 视口锚点按回合顶边复位,于是跳到别处(2026-09-07 探针实测:31 轮重建成 25 轮)。
  let seen = 0, cut = 0;
  for (let i = list.length - 1; i >= 0; i--) {
    if (hsHistoryTurnStart(list[i]) && ++seen > HS_FIRST_TURNS) { cut = i; break; }
  }
  if (!cut) { hsPour(v, list); hsHistoryReveal(v); return; }   // 本来就没几回合,分批反而多一次重排
  hsPour(v, list.slice(cut));
  // 待补的那批挂在视图上,谁先到谁消费。用户在空闲帧到来之前就往上翻的话,翻页那一路
  // 会先把它插进去——两边都插等于同一批回合出现两次,而翻页取的 have 又按 turns 数算,
  // 一旦重复,后续翻页的起点也跟着错。
  v.pendRest = list.slice(0, cut);
  // 第二批要尽快落地:刻度那一列等它画全(见 hsLoad),拖着不补等于刻度迟迟不出来。
  // 但不能把几十个回合塞进同一个空闲回调:真实 Codex 会话仍会形成 60ms 长任务。
  // 交给分批调度器,每批之间明确让出一帧。
  hsPourRestSoon(v);
}

function hsPourRestSoon(v) {
  if (!v.pendRest || !v.pendRest.length || v.restTimer) return;
  v.restTimer = setTimeout(() => {
    v.restTimer = 0;
    // 人已经切去另一段会话时暂停；再次露面由 hsShown 接着补。
    if (!v.el.isConnected || hsHidden(v)) return;
    hsPourRest(v);
  }, 32);
}

function hsPourRest(v, all = false) {
  const rest = v.pendRest;
  if (!rest || !rest.length) { v.pendRest = null; return; }
  let cut = 0, seen = 0;
  if (!all) {
    for (let i = rest.length - 1; i >= 0; i--) {
      // 同 hsPourFast:边界落在用户消息之前,一次交换不跨批
      if (hsHistoryTurnStart(rest[i]) && ++seen > 4) { cut = i; break; }
    }
  }
  const chunk = cut ? rest.slice(cut) : rest;
  v.pendRest = cut ? rest.slice(0, cut) : null;
  // 刻度只在最后一批重建。正文可以分帧长出来,位置尺不必跟着跳四五次。
  hsPrepend(v, chunk, !v.pendRest);
  if (v.pendRest) hsPourRestSoon(v);
  else { hsHistoryReveal(v); hsReconcileAfterLoad(v); }
}

function hsPour(v, msgs) {
  // 在跑的那一回合另有一份内存快照(读消息时后端补的)。引擎的记录是边跑边写的,认领时
  // 会把它的半截也读进存储,两份 id 不同——都铺出来就是同一回合出现两次:前一份带着
  // 「已处理 18.3s」和时间戳像是跑完了,后一份才是真在跑的。以快照为准,丢掉落在它时间窗里的。
  const live = (msgs || []).find((m) => (m.info || {}).running);
  let t0 = live ? (live.info.started || 0) : 0;
  // 陈快照不认。后端那份快照只在回合正常收尾时摘掉,收尾发生在跑这一回合的线程里;
  // 线程被外部因素带走(进程被杀、引擎服务重启)时快照会留下,而这道过滤会把它时间窗
  // 之后的助手消息全部隐藏——界面上就是「AI 的回复全没了,用户消息还在」。
  // 判据看快照自己的心跳(info.beat,后端每次写快照都盖一次),不看它跑了多久:
  // 回合上限已经从「总时长」改成「多久没动静」,一个跑了两小时的目标回合完全正常,
  // 起始时刻不再说明任何事。心跳缺席的是旧版本写下的快照,退回原来那条时间窗。
  const beat = live ? (live.info.beat || 0) : 0;
  const stale = beat ? (Date.now() - beat > 3 * 60 * 1000)
                     : (t0 && Date.now() - t0 > 20 * 60 * 1000);
  if (stale) t0 = 0;
  if (t0) {
    msgs = msgs.filter((m) => m === live || (m.info || {}).role !== "assistant"
                              || ((m.info || {}).at || 0) < t0);
  }
  let pendGoal = null;
  // 原生摘要会把一轮 assistant 拆成两段。压缩不是新提问，续写仍归原过程区；
  // 若中间出现真正的用户消息，立即结束这层认领。
  let compactHost = null;
  const wasPouring = !!v.pouring;
  v.pouring = true;
  try {
  for (const m of msgs || []) {
    const info = m.info || {};
    if (info.id) {
      v.roles.set(info.id, info.role || "assistant");
      if (v.mobileDroppedMids) v.mobileDroppedMids.delete(info.id);
    }
    if (info.aside) {
      hsAsideRow(v, Object.assign({}, info.aside,
                                 { parts: m.parts || [], msgId: info.id, at: info.at }), true);
      continue;
    }
    if (info.role === "user") {
      const txt = (m.parts || [])
        .filter((q) => q.type === "text" && !isInjected(q.text))
        .map((q) => q.text || "").join("");
      if (info.goal) {
        // 设与达成两条,落位规则相反。
        // 设定:哨兵在记录里排在用户那句话之前,照原样铺就落在气泡上方,读起来像先有结论后有
        // 请求——所以推迟到下一句用户消息建出回合之后再挂。
        // 达成:它发生在上一轮收尾的那一刻,立即交给 hsGoalRow 归进上一回合末尾。
        // 不依赖哨兵与助手消息的物理先后:有些引擎先落目标哨兵、后补助手记录,照流顺序
        // 直接追加会把「目标已达成」夹在状态行与回答正文之间。
        if (info.goal.met) hsGoalRow(v, info.goal, null);
        else pendGoal = info.goal;
        continue;
      }
      if (info.compact) {
        const el = hsCompactRow(v, txt, info.compact, undefined, info.id);
        const host = el._compactPart.turn;
        compactHost = host === v.turns[v.turns.length - 1] && host.msgId ? host : null;
        continue;
      }
      // 随这句话一起发出去的截图:重建之后照样贴在气泡里
      const ims = (m.parts || []).filter((q) => (q.type === "image" && (q.url || q.data_b64))
                                                  || (q.type === "file" && q.url))
                                 .map((q) => ({ type: q.type, name: q.name,
                                                context_kind: q.context_kind,
                                                media_type: q.media_type,
                                                url: q.url, data_b64: q.data_b64 }));
      if (txt || ims.length) {
        compactHost = null;
        const peerHead = ((m.parts || []).find((q) => q.type === "text" && q.peer_head) || {}).peer_head;
        const t = hsNewTurn(v, txt, info.id, ims, info.at, { peerHead });
        t.seq = info.seq || 0;   // 重建之后消息 id 换了一套,自续轮仍按序号认得回来
        if (pendGoal) { hsGoalRow(v, pendGoal, t); pendGoal = null; }
      }
      continue;
    }
    let t = null;
    if (compactHost && info.role === "assistant") {
      (compactHost.compactMids ||= new Set()).add(info.id);
    }
    for (const q of m.parts || []) {
      if (hsSkipPart(q)) continue;
      if (q.type === "text" && (!q.text || isInjected(q.text))) continue;
      const part = hsPart(v, info.id, q.id, q.type || "text");
      t = part.turn;
      if (info.done_at || info.at) t.at = info.done_at || info.at;   // done_at:实时收尾落的成文时刻
      if (info.engine) t.engine = info.engine;
      if (info.model) t.model = info.model;
      if (info.usage) t.usage = info.usage;
      Object.assign(part, q);
      part.done = true;   // 历史一律 markdown
      hsPaint(v, part);
    }
    if (info.role === "tool") continue;  // 返回卡不附带主代理的分支、评价或处理状态条。
    if (t && [...t.parts.values()].every((p) => p.type === "execution-result")) continue;
    if (!t) t = hsTurnFor(v, info.id);
    // 无可见正文、仅有状态/工具结果的助手回合也保留自己的生成来源。
    if (info.engine) t.engine = info.engine;
    if (info.model) t.model = info.model;
    if (info.usage) t.usage = info.usage;
    if (info.running) {
      // 还在跑的那一回合:后端补的内存快照。不收尾——不给行内动作、不写「已处理」,
      // 状态行交回给流。之后的增量按 part id 落回这些块上,接得上。
      hsBusy(v, true);
      v.turnReady = false;
      // 计时接着算:起点用后端给的回合起始时刻,不是这次刷新的时刻。
      // hsStatus 只在 statusT0 为空时才自己取 now,所以先摆在这里。
      if (info.started) v.statusT0 = info.started;
      if (info.retry) hsRetrySync(v, info.retry);
      else hsRetrySync(v, {active: false, generation: info.generation});
      if (info.activity) hsActivitySync(v, info.activity);
      const q = [...t.parts.values()].reverse().find((x) => x.type === "tool");
      const run = q && (q.state || {}).status === "running";
      hsStatus(v, "working", run ? "执行中" : "处理中");   // 同上:没有依据就不说在思考
      if (q) hsLogStep(v, hsToolOutcome(q, run) + " " + hsToolObj(q).t);
      // 压缩中刷新:进度接着原来的起点算,不从零重来
      if (info.compacting) hsCompactOn(v, info.compacting);
      if (info.environment) hsEnvironment(v, info.environment);
      // 重放「还在跑」的回合是最容易断的一段路:切引擎会整页重载,重载后这一回合的过程区
      // 时常展不开、也没有摘要(2026-08-12 实见)。后端那侧已经排除——/session/{id}/message
      // 返回的这条 running 消息带着 reasoning/tool/text 的 parts,问题在前端重放。
      // 下面三个数正好把病灶分开,不必再靠猜:
      //   parts=0                      → hsPour 根本没把 parts 收进来,查上面的 hsSkipPart/过滤
      //   parts>0 且过程区不在文档、步骤=0 → 节点留在某个脱离文档的旧盒子里,hsProcRepaint 没兜住
      //   两个都不为 0                  → 数据与 DOM 都在,是 CSS 把它藏了
      // 走 deskLog 而不是 console:这条现场只出现在重载后的头几百毫秒,开检查器往往已经错过。
      const body = t.procEl && t.procEl.querySelector(".hs-proc-body");
      deskLog(`[hs] 重放 ${v.sid} 回合 ${v.turns.indexOf(t) + 1}:parts=${t.parts.size}`
        + ` 类型=${[...t.parts.values()].map((x) => x.type).join(",")}`
        + ` 过程区在文档=${!!(t.procEl && t.procEl.isConnected)}`
        + ` 步骤=${body ? body.children.length : 0}`);
    } else if (info.interrupted) {
      if (t.parts.size) hsFoot(v, t);
      hsProcInterrupted(t, info.ms);
    } else if (info.failed) {
      if (t.parts.size) hsFoot(v, t);
      hsProcFailure(t, info.error);
      hsProcFailed(t, info.ms);
    } else if (!t.parts.size) {   // 空的助手消息不留空回合(在跑的那条除外,上面已经处理)
      t.el.remove();
      v.turns = v.turns.filter((x) => x !== t);
    } else {
      hsFoot(v, t);
      hsProcDone(t, info.ms);   // 历史回合没有活的状态行,顶行由这里补一条静态的
    }
  }
  } finally {
    v.pouring = wasPouring;
  }
  const forks = v.stream.querySelectorAll('.hs-fb[data-a="fork"]');
  forks.forEach((x, i) => { if (i < forks.length - 1) x.remove(); });
  if (!v.pouring) hsTrimDom(v);   // 整批历史落地后再按手机/桌面预算一次性裁，避免逐回合抖动
}
