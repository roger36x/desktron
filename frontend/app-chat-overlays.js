// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 6/13 片
// ---- 权限询问 --------------------------------------------------------------

// AskUserQuestion 走的是同一条权限通道,但它问的不是「要不要执行」,而是「把你的选择填进入参」
// ——工具自己不问人,它只负责把入参里的 answers 转成结果。所以这里不能给「允许 / 拒绝」两个钮,
// 要把问题和选项摆出来,选完连同 answers 一起回帧。实测 auto 模式下别的工具一个请求都不发,
// 唯独它照发,正因为它不是权限询问。
// 提问面板:贴在输入框上方,一次一个问题(照 claude.ai 的做法)。
// 不摆进消息流——它要人现在回应,而消息流是往上滚的历史;贴着输入框才在视线里,
// 也才好和「直接打字回答」并存。选项前给编号,底下一行可以自己写,右边跳过。
function hsAskQuestions(v, p) {
  const qs = ((p.input || {}).questions || []).filter((q) => q && q.question);
  if (!qs.length) return false;
  hsAskClose(v);
  const st = { id: p.id, qs, i: 0, ans: {}, sel: qs.map(() => new Set()) };
  const el = document.createElement("div");
  el.className = "hs-qp hs-col";
  el.innerHTML =
    '<div class="hs-qp-h"><span class="hs-qp-q"></span>'
    + '<span class="hs-qp-nav"><button class="hs-qp-a" data-d="-1">' + HS_ICONS.qprev + "</button>"
    + '<span class="hs-qp-n"></span>'
    + '<button class="hs-qp-a" data-d="1">' + HS_ICONS.qnext + "</button>"
    + '<button class="hs-qp-x">' + HS_ICONS.qclose + "</button></span></div>"
    + '<div class="hs-qp-list"></div>';
  v.el.querySelector(".hs-composer").insertBefore(el, v.el.querySelector(".hs-box"));
  st.el = el;
  v.ask = st;
  el.querySelectorAll(".hs-qp-a").forEach((b) => {
    b.onclick = () => hsAskGo(v, st.i + Number(b.dataset.d));
  });
  el.querySelector(".hs-qp-x").onclick = () => hsAskSend(v, {});
  hsAskPaint(v);
  v.inp.dataset.ph = v.inp.getAttribute("data-ph");
  v.inp.setAttribute("data-ph", "或者直接回复…");
  hsScroll(v);
  return true;
}

// 画当前这一问。整块重画:一次只有三五行,重画比就地改省心,也不会留下上一问的选中态。
function hsAskPaint(v) {
  const st = v.ask;
  if (!st) return;
  const q = st.qs[st.i];
  st.el.querySelector(".hs-qp-q").textContent = q.question;
  st.el.querySelector(".hs-qp-n").textContent = `${st.i + 1} / ${st.qs.length}`;
  st.el.querySelector('[data-d="-1"]').disabled = st.i === 0;
  st.el.querySelector('[data-d="1"]').disabled = st.i >= st.qs.length - 1;
  const list = st.el.querySelector(".hs-qp-list");
  list.innerHTML = "";
  (q.options || []).forEach((o, n) => {
    const row = document.createElement("div");
    row.className = "hs-qp-r" + (st.sel[st.i].has(o.label) ? " on" : "");
    row.innerHTML = '<span class="hs-qp-i"></span><span class="hs-qp-t"><b></b><i></i></span>'
      + '<span class="hs-qp-go">' + HS_ICONS.qnext + "</span>";
    row.querySelector(".hs-qp-i").textContent = String(n + 1);
    row.querySelector("b").textContent = o.label || "";
    const d = row.querySelector("i");
    d.textContent = o.description || "";
    d.hidden = !o.description;
    row.onclick = () => hsAskPick(v, o.label, !!q.multiSelect);
    list.appendChild(row);
  });
  // 多选没有「选中即走」这一步,推进全靠别处的按钮。而单问题时翻页钮是禁用的,唯一按得动的
  // 是那颗写着「跳过」的——语义正好相反,于是选完之后界面上找不到交上去的地方
  // (2026-08-23 用户实报)。这里补一行明确的:只在多选、且真的选中了东西时出现。
  // 一个都没选时不出,那种情形本来就该走「跳过」。
  if (q.multiSelect && st.sel[st.i].size) {
    const done = document.createElement("div");
    const last = st.i >= st.qs.length - 1;
    done.className = "hs-qp-r hs-qp-done";
    done.innerHTML = '<span class="hs-qp-i">' + HS_ICONS.check + "</span>"
      + '<span class="hs-qp-t"><b></b><i></i></span>'
      + '<span class="hs-qp-go">' + HS_ICONS.qnext + "</span>";
    done.querySelector("b").textContent = last ? "提交" : "下一问";
    done.querySelector("i").textContent = `已选 ${st.sel[st.i].size} 项`;
    done.onclick = () => hsAskNext(v);
    list.appendChild(done);
  }
  // 讨论一下:这个问题本身要先聊清楚,才谈得上选哪个。收面板、把问题原文填进输入框,
  // 接着说就是普通一轮对话。工具那边收到的是「没作答」,模型看得懂——它随后就读到你的话。
  const chat = document.createElement("div");
  chat.className = "hs-qp-r hs-qp-chat";
  chat.innerHTML = '<span class="hs-qp-i">' + HS_ICONS.qchat + "</span>"
    + '<span class="hs-qp-t"><b>讨论一下</b></span>'
    + '<span class="hs-qp-go">' + HS_ICONS.qnext + "</span>";
  chat.onclick = () => { const t = q.question; hsAskSend(v, {}); hsFill(v, t); };
  list.appendChild(chat);
  // 自己写的那一行:点开变成输入框,回车即作答。右边是跳过——两者都是「不选上面那些」。
  const own = document.createElement("div");
  own.className = "hs-qp-r hs-qp-own";
  own.innerHTML = '<span class="hs-qp-i">' + HS_ICONS.qpen + "</span>"
    + '<input class="hs-qp-in" placeholder="自己写一个…">'
    + '<button class="hs-qp-skip">跳过</button>';
  const inp = own.querySelector(".hs-qp-in");
  inp.onkeydown = (e) => {
    if (e.key !== "Enter") return;
    e.preventDefault();
    const t = inp.value.trim();
    if (t) hsAskPick(v, t, false, true);
  };
  // 跳过 = 不选上面那些。原来它直接走 hsAskNext,而那一步会把已经点上的多选项结算并交出去——
  // 按钮说的和做的相反。先把这一问的选中态清干净再走。
  own.querySelector(".hs-qp-skip").onclick = (e) => {
    e.stopPropagation();
    st.sel[st.i].clear();
    delete st.ans[q.question];
    hsAskNext(v);
  };
  list.appendChild(own);
}

// 选一项。单选选完就走下一问(最后一问直接交);多选就地开关,由翻页或跳过推进。
function hsAskPick(v, label, multi, free) {
  const st = v.ask;
  if (!st) return;
  const set = st.sel[st.i];
  if (multi) {
    if (set.has(label)) set.delete(label); else set.add(label);
    hsAskPaint(v);
    return;
  }
  set.clear(); set.add(label);
  st.ans[st.qs[st.i].question] = label;
  if (free) st.free = true;
  hsAskNext(v);
}

function hsAskNext(v) {
  const st = v.ask;
  if (!st) return;
  // 多选那一问离开时才结算:走的时候把这一问选中的都记下
  const q = st.qs[st.i];
  if (q.multiSelect && st.sel[st.i].size) st.ans[q.question] = [...st.sel[st.i]];
  if (st.i >= st.qs.length - 1) { hsAskSend(v, st.ans); return; }
  hsAskGo(v, st.i + 1);
}

function hsAskGo(v, i) {
  const st = v.ask;
  if (!st || i < 0 || i >= st.qs.length) return;
  const q = st.qs[st.i];
  if (q.multiSelect && st.sel[st.i].size) st.ans[q.question] = [...st.sel[st.i]];
  st.i = i;
  hsAskPaint(v);
}

// 收面板并还原输入框的占位文字
function hsAskClose(v) {
  const st = v.ask;
  if (!st) return;
  if (st.el && !st.historyID) st.el.remove();
  if (st.historyID) st.el?.querySelector(".hs-acts")?.remove();
  v.ask = null;
  if (v.inp && v.inp.dataset.ph) {
    v.inp.setAttribute("data-ph", v.inp.dataset.ph);
    delete v.inp.dataset.ph;
  }
}

// 交答案。answers 为空、又带着 response 的那种,是人直接在输入框里回的——
// 工具认这一路(结果会写成 The user responded: …),不必先勉强对上某个选项。
async function hsAskSend(v, answers, response) {
  const st = v.ask;
  if (!st || st.permission) return;
  const id = st.id;
  hsAskClose(v);
  v.awaitAsk = false;
  hsStatus(v, "working", "处理中");
  try {
    await api(`/session/${encodeURIComponent(v.sid)}/permission`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ id, allow: true, answers: answers || {},
                             response: response || "" }) });
  } catch {}
}
function hsApprovalLabel(resolution) {
  return { pending: "等待审批", approved: "已允许", denied: "已拒绝",
    timeout: "审批已过期，操作未执行", cancelled: "审批已取消",
    connection_error: "审批回执发送失败", unavailable: "请求已失效，未记录处理结果"
  }[resolution] || "审批已结束";
}

function hsPaintApproval(v, part) {
  if (v.ask?.historyID === part.id && part.resolution === "pending") {
    // 整页对账会重建 part/turn，但正在等待用户点击的按钮仍在 v.ask.el 上。把同一节点
    // 认领到新过程盒，不能因「避免历史快照覆盖活控件」这道早退把卡片一起丢掉。
    if (v.ask.el) { part.el = v.ask.el; hsProcPlacePart(part, part.el); }
    return;
  }
  const el = part.el || document.createElement("div");
  part.el = el;
  el.className = "hs-card hs-ask";
  el.innerHTML = '<div class="hs-ico"></div><div class="hs-card-t">'
    + '<div class="hs-t1"></div><div class="hs-t2"></div><div class="hs-t2 hs-t3"></div></div>';
  el.querySelector(".hs-ico").innerHTML = part.resolution === "approved" ? HS_ICONS.unlock : HS_ICONS.lock;
  el.querySelector(".hs-t1").textContent = part.reason || `请求执行 ${hsToolLabel(part.tool)}`;
  el.querySelector(".hs-t2").textContent = hsToolArg({ state: { input: part.input || {} } }) || "无参数";
  const at = part.resolvedAt || part.at;
  el.querySelector(".hs-t3").textContent = hsApprovalLabel(part.resolution)
    + (at ? " · " + new Date(at).toLocaleString() : "");
  hsProcPlacePart(part, el);
}

function hsAsk(v, p) {
  if (p.tool === "AskUserQuestion" && hsAskQuestions(v, p)) return;
  if (v.ask?.id === p.id) return;
  if (v.ask) return; // 其余并发审批由服务端 asks 快照依次呈现。
  const part = p.historyID ? hsPart(v, p.messageID, p.historyID, "approval") : null;
  const t = part?.turn || v.turns[v.turns.length - 1] || hsNewTurn(v, null);
  const el = part?.el || document.createElement("div");
  if (part) { Object.assign(part, p, {id: p.historyID, requestID: p.id, type: "approval", resolution: "pending"}); part.el = el; }
  el.className = "hs-card hs-ask";
  v.ask = {id: p.id, el, permission: true, historyID: p.historyID};
  v.awaitAsk = true;
  el._host = hsProcHost(t);
  el.innerHTML = '<div class="hs-ico">' + HS_ICONS.lock + "</div>"
    + '<div class="hs-card-t"><div class="hs-t1"></div><div class="hs-t2"></div></div>'
    + '<div class="hs-acts"><span class="hs-lnk">拒绝</span><button class="hs-pill">允许</button></div>';
  // reason 是引擎自己写好的那句问话(codex 家族给,如「是否允许在 X 中创建 a.txt?」)。
  // 有就用它:比「请求执行 Bash」加一串参数更接近人要判断的那件事。
  el.querySelector(".hs-t1").textContent = p.reason || `请求执行 ${hsToolLabel(p.tool)}`;
  el.querySelector(".hs-t2").textContent = hsToolArg({ state: { input: p.input || {} } }) || "无参数";
  const reply = async (allow) => {
    const actions = el.querySelector(".hs-acts");
    if (!actions || v.ask?.submitting) return;
    v.ask.submitting = true;
    for (const button of actions.querySelectorAll("button")) button.disabled = true;
    try {
      const result = await api(`/session/${encodeURIComponent(v.sid)}/permission`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ id: p.id, allow }) });
      if (result?.ok === false) throw new Error("请求已结束或引擎未接收回执");
      actions.remove();
      if (allow) el.querySelector(".hs-ico").innerHTML = HS_ICONS.unlock;
      el.querySelector(".hs-t2").textContent = allow ? "已允许" : "已拒绝";
      if (v.ask?.id === p.id) { v.ask = null; v.awaitAsk = false; }
      hsStatus(v, "working", "处理中");
    } catch (error) {
      if (v.ask?.id === p.id) {
        v.ask.submitting = false;
        for (const button of actions.querySelectorAll("button")) button.disabled = false;
        el.querySelector(".hs-t2").textContent = "审批提交失败：" + (error.message || "请重试");
      }
    }
    // 回应之后核一次状态。等待期间到过的收尾帧被那道 awaitAsk 闸挡下了,而它不会重来
    // ——不核的话界面就一直停在「处理中」,人明明已经点过(2026-09-03 用户实见)。
    // 拒绝那一路更要核:回合到此为止,再没有别的事件会来了。
    if (typeof hsStateSync === "function") hsStateSync(v).catch(() => {});
  };
  el.querySelector(".hs-lnk").onclick = () => reply(false);
  el.querySelector(".hs-pill").onclick = () => reply(true);
  if (part) hsProcPlacePart(part, el);
  else hsProcPlace(t, el, t.parts ? t.parts.size : 0);
  hsProcReveal(t);
  hsScroll(v);
}

// ---- 发言索引(刻度) --------------------------------------------------------

// 刻度 = 一排短横杠,当前那条更长更亮。悬停单条出预览卡(这条发言的开头 + 回复的开头),
// 不再整条展开成列表:要找的是「哪一段在讲什么」,一次看一条就够,展开全部反而遮住正文。
function hsIndex(v) {
  v.toc.innerHTML = "";
  v.tkOn = null;   // 刻度整排重建,原来那根已经不在文档里了
  v.tocAt = null;
  v.turns.forEach((t) => {
    if (!t.user) return;
    const tk = document.createElement("div");
    tk.className = "hs-tk";
    tk._turn = t;   // 命中判定在容器上做(见 hsTocHit),刻度自己带着所属回合
    v.toc.appendChild(tk);
  });
  hsTocSync(v);
  hsTocFade(v);
}

// 顶端渐隐,表示上面还有没载入的回合。前提是这一列自己已经装不下——短会话整列都摆得开,
// 再淡出去会读成「上面还有」,而其实没有。moreDone 是往上翻到底之后置的,置了就收掉渐隐。
function hsTocFade(v) {
  if (!v.toc) return;
  const overflow = v.toc.scrollHeight > v.toc.clientHeight + 1;
  v.toc.classList.toggle("more", !v.moreDone && overflow);
}

// 刻度只有 2px 高、间隔 7px,真正落在杠上的只有两成多的纵向范围,靠 :hover 逐条判定
// 会让指针在间隙里时什么都不亮,来回移动就是一闪一闪。改成在整列上按 Y 找最近的一根:
// 指针进了这一列就必定有一根处于命中态,间隙归给最近的那根。命中态因此是类驱动(.on),
// CSS 里不能再留 :hover——留了会出现「指到间隙亮 A、指到杠上又亮 B」两套判定打架。
function hsTocHit(v, y) {
  let best = null, bd = Infinity;
  v.toc.querySelectorAll(".hs-tk").forEach((tk) => {
    const r = tk.getBoundingClientRect();
    const d = Math.abs(y - (r.top + r.height / 2));
    if (d < bd) { bd = d; best = tk; }
  });
  if (!best) return;
  const shown = v.peekEl && v.peekEl.classList.contains("on");
  if (best === v.tkOn && shown) return;   // 还在同一根上,不必每帧重排预览卡
  v.toc.querySelectorAll(".hs-tk.on").forEach((x) => x.classList.remove("on"));
  best.classList.add("on");
  v.tkOn = best;
  hsPeek(v, best, best._turn);
}

// 容器上一次性挂好,刻度本身每次 hsIndex 都会重建,监听不能挂在它们身上。
// 鼠标移动节流 30ms；使用 PointerEvent 区分鼠标与触摸，避免触摸后的兼容鼠标事件取消收起。
function hsTocBind(v) {
  let last = 0;
  v.toc.addEventListener("pointermove", (e) => {
    if (e.pointerType !== "mouse") return;
    const now = Date.now();
    if (now - last < 30) return;
    last = now;
    clearTimeout(v.peekT);
    hsTocHit(v, e.clientY);
  });
  // 点击也落在容器上,跳的是当前命中的那根。挂在刻度本身的话,横杠只有 2px 高,
  // 指针落在缝隙里就点不动,而那时预览卡明明已经亮着——看得见却点不着。
  v.toc.addEventListener("click", () => {
    const t = v.tkOn && v.tkOn._turn;
    if (t && t.el) { hsPeekHide(v); hsUnstick(v); hsViewportReveal(v, t.el, "start"); }
  });
  v.toc.addEventListener("pointerenter", (e) => {
    if (e.pointerType !== "mouse") return;
    clearTimeout(v.peekT); hsTocHit(v, e.clientY);
  });
  // 离开这一列不立刻收:预览卡在右边 4px 处,指针要够得着它(卡片自己会取消这个定时器)
  v.toc.addEventListener("pointerleave", (e) => {
    if (e.pointerType === "mouse") hsPeekLater(v, 140);
  });
  // 触摸:上面那套全靠指针位置,而触屏没有 mousemove/mouseenter,tkOn 一直是空的,
  // 于是点下去 click 里取不到目标——看着能点,点了不动。按下时先按落点判一次命中,
  // 后面的 click 就有目标可跳了。滑动中持续跟随:手指沿这一列拖,预览卡跟着走,
  // 相当于触屏上的「悬停」。preventDefault 只在触到这一列时生效,不影响正文滚动。
  const touchHit = (e) => {
    const t = e.touches && e.touches[0];
    if (!t) return;
    clearTimeout(v.peekT);
    hsTocHit(v, t.clientY);
  };
  v.toc.addEventListener("touchstart", touchHit, { passive: true });
  v.toc.addEventListener("touchmove", (e) => { touchHit(e); e.preventDefault(); }, { passive: false });
  v.toc.addEventListener("touchend", () => hsPeekLater(v, 1600));
  v.toc.addEventListener("touchcancel", () => hsPeekLater(v, 1600));
  v.el.addEventListener("touchstart", (e) => {
    if (!v.toc.contains(e.target) && !v.peekEl?.contains(e.target)) hsPeekHide(v);
  }, { passive: true, capture: true });
}

function hsPeekLater(v, ms) {
  clearTimeout(v.peekT);
  v.peekT = setTimeout(() => hsPeekHide(v), ms);
}

function hsPeek(v, tk, t) {
  clearTimeout(v.peekT);
  if (!v.peekEl) {
    v.peekEl = document.createElement("div");
    v.peekEl.className = "hs-peek";
    v.peekEl.innerHTML = '<div class="hs-peek-u"></div><div class="hs-peek-a"></div>';
    v.peekEl.onpointerenter = (e) => { if (e.pointerType === "mouse") clearTimeout(v.peekT); };
    v.peekEl.onpointerleave = (e) => { if (e.pointerType === "mouse") hsPeekLater(v, 140); };
    // 卡片本身也要能点:它压在刻度那一列上,人看到的是卡片,真正带 onclick 的却是背后
    // 那根 2px 的横杠,点上去没反应。所属回合每次 hsPeek 都会更新(见下方 _turn)。
    v.peekEl.onclick = () => {
      const t2 = v.peekEl._turn;
      if (!t2 || !t2.el) return;
      hsPeekHide(v);
      hsUnstick(v);
      hsViewportReveal(v, t2.el, "start");
    };
    v.el.querySelector(".hs-wrap").appendChild(v.peekEl);
  }
  v.peekEl._turn = t;   // 卡片的点击目标随悬停的刻度走
  v.peekEl.querySelector(".hs-peek-u").textContent = t.user || "";
  const reply = [...t.parts.values()].filter((x) => x.type === "text")
    .map((x) => x.text || "").join(" ").replace(/\s+/g, " ").trim();
  const a = v.peekEl.querySelector(".hs-peek-a");
  a.textContent = reply.slice(0, 160);
  a.style.display = reply ? "" : "none";
  v.peekEl.classList.add("on");
  // 竖向对齐这条刻度,超出上下边界就顶回来
  const wr = v.el.querySelector(".hs-wrap").getBoundingClientRect();
  const r = tk.getBoundingClientRect();
  const h = v.peekEl.offsetHeight;
  let top = r.top - wr.top + r.height / 2 - h / 2;
  top = Math.max(8, Math.min(top, wr.height - h - 8));
  v.peekEl.style.top = Math.round(top) + "px";
}

function hsPeekHide(v) {
  clearTimeout(v.peekT);
  v.peekT = null;
  if (v.peekEl) v.peekEl.classList.remove("on");
  // 命中态跟着预览卡一起收,再把位置态还回来:指针走了之后刻度该回到「我正在读第几段」
  if (v.tkOn) { v.tkOn.classList.remove("on"); v.tkOn = null; }
  hsTocSync(v);
}

// 位置高亮:滚动到哪一段,哪一根刻度亮。刻度条是位置尺,不标出当前位置就只剩装饰。
// 判定线取视口下缘往上 60px:算的是「已经进入视野的最后一段」。用上缘那条线会偏早——
// 新一段的开头刚露出来时它还没越过上缘,刻度仍指着上一段,而人眼看的已经是新的这段了。
function hsTocSync(v) {
  if (!v.toc || v.tkOn) return;   // 悬停期间由 hsTocHit 接管,不跟它抢
  const ticks = [...v.toc.querySelectorAll(".hs-tk")];
  if (!ticks.length) return;
  // 贴底时答案是最后一根,不必为一次程序性钉底逐个读取所有回合的几何位置。
  if (v.stick) {
    const next = ticks[ticks.length - 1];
    if (v.tocAt !== next) {
      if (v.tocAt) v.tocAt.classList.remove("at");
      next.classList.add("at");
      v.tocAt = next;
    }
    hsSetScrollBottom(v.toc);
    return;
  }
  const r = v.body.getBoundingClientRect();
  const line = r.bottom - 60;
  let cur = 0;
  ticks.forEach((tk, i) => {
    const t = tk._turn;
    if (t && t.el && t.el.getBoundingClientRect().top <= line) cur = i;
  });
  const next = ticks[cur];
  if (v.tocAt !== next) {
    if (v.tocAt) v.tocAt.classList.remove("at");
    if (next) next.classList.add("at");
    v.tocAt = next;
  }
  // 这一列自己是可滚的(max-height 74%,一屏大约放得下 65 根),超出的部分滚在容器外面。
  // 不跟随的话,长会话里看到的最末一根并不是最后一轮——它只是恰好停在可见区末尾的那一根,
  // 而这一列没有滚动条,人也看不出还有更下面。
  // 只挪自己的 scrollTop,不用 scrollIntoView:那个会把外层正文区一起带着滚。
  const el = ticks[cur];
  if (!el) return;
  const cr = v.toc.getBoundingClientRect(), er = el.getBoundingClientRect();
  if (er.top < cr.top + 6) v.toc.scrollTop -= cr.top + 6 - er.top;
  else if (er.bottom > cr.bottom - 6) v.toc.scrollTop += er.bottom - (cr.bottom - 6);
}

// scroll 事件可以很密,而刻度同步要读正文和刻度两边的几何。直接在 scroll 回调里跑,
// 会和流式贴底、浏览器布局、刻度自身 scrollTop 回写互相插队。统一并到下一帧,一帧只算一次。
function hsTocSchedule(v) {
  if (!v || !v.toc || v.tocRaf) return;
  v.tocRaf = requestAnimationFrame(() => {
    v.tocRaf = 0;
    hsTocSync(v);
  });
}

// ---- 滚动 ------------------------------------------------------------------
// 贴底状态由滚动事件维护,不在每个流式事件里读布局——高频 read→write 会把页面拖到抖。
// 尺寸变化由统一 ResizeObserver 在绘制前提交；rAF 只作为旧 WebView 的后备路径。

function hsSetScrollBottom(el) {
  if (!el) return;
  const bottom = Math.max(0, el.scrollHeight - el.clientHeight);
  if (Math.abs(el.scrollTop - bottom) > 1) el.scrollTop = bottom;
}

// 运行期的跟随步进。贴底这件事本身没问题,问题在「一次到位」:正文的高度按整行增长,
// 硬贴底就把每一行都变成视口的一次瞬移。窗口越窄,一行占视口的比例越大、换行也越频繁,
// 两头一叠就是用户看到的抖动(2026-09-03 录屏实测,333px 高的窗口:32% 的帧完全不动,
// 其余帧一次跳 25~90px,中位数 37px,约每秒七八次)。
//
// 这里把「跳一行」摊成几帧的逼近:每帧吃掉当前欠量的一半,不足 1.5px 按 1.5px 走,
// 一行约五帧收敛。同一段录屏比对:静止帧从 32% 降到 7%,位移中位数从 37px 降到 19px,
// 且分布从 0/25/37/75 这几个行高倍数变成 8~34px 连续铺开。
//
// 两条硬边界,都是为了不拿「顺滑」换掉「看得见最新的字」:
//   欠量 > 一屏——切页、恢复、整块贴进来,那不是「又长了一行」,直接归位;
//   欠量 > CAP——出字快过缓动能吸收的量,先把视口拉到「离底 CAP」再继续缓动,
//   最新那一行半始终在视野里,不会为了顺滑把人落在半空。
const HS_FOLLOW_K = 0.34;     // 欠量很小时每帧吃掉的比例
const HS_FOLLOW_MIN = 1.5;    // 最小步长:纯比例逼近的尾巴走不完
function hsFollowCap(el) {
  // 允许落后底部多少,也就是缓动的蓄水池:欠量涨到这里时比例升到 1(当帧追平)。
  // 按视口取半屏,64px 兜住极矮的窗口。用相对量而不是定值,是因为「落后多少算多」
  // 本来就跟看得见多少有关:同样落后 48px,在 180px 高的窗口里是四分之一屏,
  // 在 900px 的窗口里几乎看不出来。
  // (定值 48px 也实测过,在本文测过的几档里差别很小:静止帧 7% 对 5%。换成相对量
  //  是为了极端窗口高度下不失准,不是因为定值那一版失效。)
  return Math.max(64, el.clientHeight * 0.5);
}
function hsFollowTarget(el) {
  const bottom = Math.max(0, el.scrollHeight - el.clientHeight);
  const d = bottom - el.scrollTop;
  if (d <= 1 || d > el.clientHeight) return bottom;
  // 比例随欠量二次上升,到蓄水池满时正好等于 1(追平)。用渐变而不是「到顶就截断」:
  // 硬截断在出字快的时候等于每帧原样跟住增量,又变回一行一跳,边界两侧的观感是断的。
  const lag = Math.min(1, d / hsFollowCap(el));
  const k = HS_FOLLOW_K + (1 - HS_FOLLOW_K) * lag * lag;
  return Math.min(bottom, el.scrollTop + Math.max(HS_FOLLOW_MIN, d * k));
}

// ---- 视口跳动取证 ----------------------------------------------------------
// 2026-09-07 用户实见:输出期间「处理中」折叠着、画面布局没有可见变化,正文仍会先上跳、
// 再快速回到底部。此前只有模拟用例(临时文本转 Markdown 会缩短 25~36px)能复现一部分,
// 现场的大幅位移与静置时的跳动都没有证据。这里只取证,不改任何滚动行为:
//   每页一个环形缓冲,记最近几十条「写入 / scroll 事件 / 布局变更来源 / 用户输入」;
//   scroll 事件里发现贴底态下 scrollTop 无人操作却下降超过阈值,把缓冲整段倒进 deskLog。
// 只记结构与数值(来源标签、几何、状态位、part 类型与长度),不记正文、工具参数、凭证。
// 浏览器自己夹小 scrollTop(节点摘挂之间撞上一次布局、原生锚定)不经过 JS 写入点,
// 只会以 scroll 事件的形式出现:所以 scroll 行带 sinceW(距上一次 JS 写入的毫秒)与
// echo(位置是否等于上一次写入落点),两者合起来才能分出「自家写的」和「浏览器动的」。
const HS_VPD_KEEP = 80;       // 环形缓冲条数:贴底跟随每帧一条写入,80 条约一秒多的现场
const HS_VPD_DUMP = 28;       // 每次只倒触发前这么多行:日志缓冲共 2000 行,首批 7 次就占了五百行
const HS_VPD_JUMP = 8;        // 贴底态下 scrollTop 下降超过这个值才算异常(跟随环的缓动只会向下)
const HS_VPD_QUIET = 500;     // 最近这么久内有明确用户输入的,算人翻的,不倒
const HS_VPD_GAP = 2000;      // 同一页两次倾倒的最短间隔,避免一次连环抖动刷满日志
const HS_VPD_MAX = 16;        // 每页最多倾倒次数
let _hsVpdBuild = "";

function hsVpdBuild() {
  if (_hsVpdBuild) return _hsVpdBuild;
  let tok = "-";
  try {
    const s = typeof document !== "undefined" && document.querySelector('script[src*="app-chat-overlays"]');
    const m = s && /[?&]v=([^&]+)/.exec(s.getAttribute("src") || "");
    if (m) tok = m[1].slice(0, 12);
  } catch {}
  return (_hsVpdBuild = tok);
}

function hsVpd(v) {
  // lastDump 不能从 0 起:performance.now() 开页头两秒就在 2000 以内,按间隔一算全被吞掉
  if (v.vpd) return v.vpd;
  v.vpd = { ring: [], held: 0, lastDump: -1e9, dumps: 0, input: null, wAt: -1e9, wGot: -1 };
  hsVpdWatchDom(v);
  return v.vpd;
}

// 正文容器里的 DOM 变动,按一次任务合并成一行:加了什么、摘了什么、改了谁的哪个属性。
// 同一批里既被摘又被加回的节点,就是「布局没变、位置却跳」的那种嫌疑——摘掉那一刻
// 若有人读几何触发布局,滚动位置先被夹小,加回去后不会自己长回来。只记标签与类名。
function hsVpdWatchDom(v) {
  if (typeof MutationObserver === "undefined" || !v.body || v.vpdMo) return;
  const tag = (n) => (n && n.nodeType === 1)
    ? (n.tagName.toLowerCase() + "." + String(n.className || "").trim().split(/\s+/).slice(0, 2).join(".")).slice(0, 32)
    : (n && n.nodeType === 3 ? "#text" : "?");
  const height = (n) => (n && n.nodeType === 1 && n.isConnected) ? Math.round(n.offsetHeight) : -1;
  try {
    v.vpdMo = new MutationObserver((recs) => {
      // 回调跑在本轮任务末尾的微任务里:此刻的 top/sh 说明夹位是否已经在这轮任务内发生
      const row = { n: recs.length, cd: 0, add: [], rm: [], at: [], h: [],
                    top: Math.round(v.body.scrollTop), sh: v.body.scrollHeight };
      for (const r of recs) {
        if (r.type === "characterData") { row.cd++; continue; }
        if (r.type === "attributes") {
          if (row.at.length < 6) row.at.push(tag(r.target) + "@" + r.attributeName + "=" + String(r.oldValue == null ? "" : r.oldValue).slice(0, 24));
          if (row.h.length < 6) row.h.push(height(r.target));
          continue;
        }
        for (const n of r.addedNodes) if (row.add.length < 6) { row.add.push(tag(n)); if (row.h.length < 6) row.h.push(height(n)); }
        for (const n of r.removedNodes) if (row.rm.length < 6) row.rm.push(tag(n));
      }
      hsVpdNote(v, "mut", row);
    });
    v.vpdMo.observe(v.body, { subtree: true, childList: true, characterData: true,
                              attributes: true, attributeOldValue: true,
                              attributeFilter: ["hidden", "class", "style", "open", "aria-expanded"] });
  } catch {}
}

function hsVpdState(v) {
  const s = hsViewport(v);
  return { st: v.stick ? 1 : 0, bz: v.busy ? 1 : 0, rs: v.restoringScroll ? 1 : 0,
           dg: v.dragging ? 1 : 0, ep: s.epoch, an: s.anchor ? 1 : 0, dp: s.depth,
           ho: v.holdAnchor && v.holdAnchor.el ? 1 : 0 };
}

// 记一条。调用方保证 data 里只有标签与数字;这里不读布局,免得取证本身逼出一次排版。
// 各处钩子一律写成 globalThis.hsVpdNote?.(...):十个测试按函数名抽取控制器的真实源码
// 在 node 里跑,取证函数不在抽取集里,直呼其名会 ReferenceError;可选调用缺失时静默跳过。
function hsVpdNote(v, kind, data, withState) {
  if (!v || !v.body) return;
  const d = hsVpd(v);
  const row = Object.assign({ t: Math.round(performance.now()), k: kind }, data || {},
                            withState ? hsVpdState(v) : null);
  d.ring.push(row);
  if (d.ring.length > HS_VPD_KEEP) d.ring.splice(0, d.ring.length - HS_VPD_KEEP);
}

// 没有落到具体某一页的事件(全局轮询、选区变化)记到所有开着的页上。
function hsVpdNoteAll(kind, data) {
  if (typeof _hsViews === "undefined") return;
  for (const v of _hsViews.values()) {
    if (v && v.el && v.el.isConnected) hsVpdNote(v, kind, data);
  }
}

// 写入点的记录:before/want/got 三个位置,got≠want 说明被夹住。几何由调用方传入,不再读。
function hsVpdWrite(v, why, before, want, sh, ch) {
  if (!v || !v.body) return;
  const d = hsVpd(v);
  d.wAt = performance.now(); d.wGot = v.lastTop;
  hsVpdNote(v, "write", { why: why || "?", from: Math.round(before), want: Math.round(want),
                         got: Math.round(v.lastTop), sh, ch }, true);
  // 贴底态下写入点自己把位置写小了,也是异常:跟随/贴底只会往下走,往上只可能是
  // scrollHeight 先缩了(bottom 变小,写入被夹到新底)。写入点更新了 v.lastTop,
  // 随后的 scroll 事件看不出下降,所以这一档要在这里判,不能只靠 scroll 那边。
  if (v.stick && v.lastTop < before - HS_VPD_JUMP)
    hsVpdCheck(v, "write:" + (why || "?"), before, { top: Math.round(v.lastTop), sh, ch });
}

// 绕过写入点的滚动来源。首批现场(2026-09-07 18:07)显示:上跳恒为 1072px、scrollHeight 不变、
// 不是写入点的回声、空闲时按 5 秒一次准时出现——这是某段代码或浏览器自己在动位置,
// 不是排版。scroll 事件是异步派发的,拿不到调用栈;能拿到栈的只有同步的那一刻:
// 有人给 .hs-body 写 scrollTop、或对正文里的元素调 scrollIntoView / focus / scrollTo。
// 这里把这几处包一层,只在目标落在会话正文里时记一行(带前几帧的函数名与文件行号),
// 其余原样透传。写入点自己的写走元素上的 _hsVpdOwn 标记,不重复记。
// 只在浏览器里装(有 Element 才装),装失败静默;不改任何行为。
function hsVpdStack() {
  try {
    return String(new Error().stack || "").split("\n").slice(2, 8)
      .map((l) => l.trim().replace(/^at\s+/, "").replace(/\(?https?:\/\/[^/]+\/([^?)]+)\?[^:)]*(:\d+:\d+)\)?/, "$1$2")
                 .replace(/@https?:\/\/[^/]+\//, "@").replace(/\?v=[\w-]+/, ""))
      .join(" < ").slice(0, 240);
  } catch { return ""; }
}
function hsVpdViewOf(el) {
  try {
    const body = el && el.closest ? el.closest(".hs-body") : null;
    if (!body) return null;
    const page = body.closest(".hs-page");
    const v = page && typeof _hsViews !== "undefined" && [..._hsViews.values()].find((x) => x.el === page);
    return v || null;
  } catch { return null; }
}
function hsVpdInstall() {
  if (typeof Element === "undefined" || Element.prototype._hsVpdWrapped) return;
  try {
    Element.prototype._hsVpdWrapped = true;
    const desc = Object.getOwnPropertyDescriptor(Element.prototype, "scrollTop");
    if (desc && desc.set && desc.get) {
      Object.defineProperty(Element.prototype, "scrollTop", {
        configurable: true, enumerable: desc.enumerable, get: desc.get,
        set(val) {
          if (!this._hsVpdOwn && this.classList && this.classList.contains("hs-body")) {
            const v = hsVpdViewOf(this);
            if (v) hsVpdNote(v, "ext", { op: "scrollTop", val: Math.round(Number(val) || 0), stk: hsVpdStack() });
          }
          return desc.set.call(this, val);
        },
      });
    }
    const wrapped = [];
    for (const name of ["scrollIntoView", "scrollIntoViewIfNeeded", "scrollTo", "scrollBy", "scroll"]) {
      const orig = Element.prototype[name];
      if (typeof orig !== "function") continue;
      Element.prototype[name] = function (...args) {
        const v = hsVpdViewOf(this);
        if (v) hsVpdNote(v, "ext", { op: name, cls: String(this.className || "").slice(0, 40), stk: hsVpdStack() });
        return orig.apply(this, args);
      };
      wrapped.push(name);
    }
    for (const proto of [typeof HTMLElement !== "undefined" && HTMLElement.prototype,
                         typeof SVGElement !== "undefined" && SVGElement.prototype]) {
      if (!proto || typeof proto.focus !== "function" || !Object.prototype.hasOwnProperty.call(proto, "focus")) continue;
      const origFocus = proto.focus;
      proto.focus = function (...args) {
        const v = hsVpdViewOf(this);
        if (v) hsVpdNote(v, "ext", { op: "focus", cls: String(this.className || "").slice(0, 40), stk: hsVpdStack() });
        return origFocus.apply(this, args);
      };
      wrapped.push("focus");
    }
    // 不经滚动 API 也能让滚动容器移位的几条路:选区变化会让内核把选区揭示出来,
    // 表单校验会把无效控件卷进视口。这些调用的 this 不是页面元素,记到所有开着的页上。
    if (typeof Selection !== "undefined") {
      for (const name of ["addRange", "collapse", "setBaseAndExtent", "modify", "selectAllChildren", "extend"]) {
        const orig = Selection.prototype[name];
        if (typeof orig !== "function") continue;
        Selection.prototype[name] = function (...args) {
          hsVpdNoteAll("ext", { op: "sel." + name, stk: hsVpdStack() });
          return orig.apply(this, args);
        };
        wrapped.push("sel." + name);
      }
    }
    for (const [ctor, names] of [
      [typeof HTMLInputElement !== "undefined" && HTMLInputElement, ["select", "setSelectionRange"]],
      [typeof HTMLTextAreaElement !== "undefined" && HTMLTextAreaElement, ["select", "setSelectionRange"]],
      [typeof HTMLFormElement !== "undefined" && HTMLFormElement, ["reportValidity", "requestSubmit"]],
    ]) {
      if (!ctor) continue;
      for (const name of names) {
        const orig = ctor.prototype[name];
        if (typeof orig !== "function") continue;
        ctor.prototype[name] = function (...args) {
          const v = hsVpdViewOf(this);
          const row = { op: name, cls: String(this.className || "").slice(0, 40), stk: hsVpdStack() };
          if (v) hsVpdNote(v, "ext", row); else hsVpdNoteAll("ext", row);
          return orig.apply(this, args);
        };
        wrapped.push(name);
      }
    }
    // 安装心跳:没有这一行,「ext 为零」分不清是真没人调,还是包装在这个内核里根本没装上
    if (typeof deskLog === "function")
      deskLog(`[vpd] install scrollTop=${desc && desc.set ? 1 : 0} ${wrapped.join(",")} build=${hsVpdBuild()}`);
  } catch (e) {
    if (typeof deskLog === "function") deskLog("[vpd] install failed " + String(e && e.message || e).slice(0, 80));
  }
}
hsVpdInstall();

// 尺寸观察器的记录:哪个容器变了(s 正文流 / c 输入区 / b 视口 / e 整页)、变成多高。
// contentRect 是观察器给的,不逼布局。
function hsVpdRo(v, entries) {
  if (!v || !v.body || !entries) return;
  const tg = [], h = [];
  for (const e of entries) {
    tg.push(e.target === v.stream ? "s" : e.target === v.body ? "b" : e.target === v.el ? "e" : "c");
    h.push(Math.round(e.contentRect ? e.contentRect.height : 0));
  }
  hsVpdNote(v, "ro", { tg: tg.join(""), h });
}

// scroll 事件的记录与判定。监听器紧接着就要读这几个值,这里先读一遍不多付布局。
function hsVpdScroll(v) {
  if (!v || !v.body) return;
  const d = hsVpd(v);
  const g = { top: Math.round(v.body.scrollTop), sh: v.body.scrollHeight, ch: v.body.clientHeight };
  const prev = v.lastTop === undefined ? null : v.lastTop;
  hsVpdNote(v, "scroll", Object.assign({ prev: prev == null ? -1 : Math.round(prev),
    sinceW: Math.round(performance.now() - d.wAt), echo: Math.abs(g.top - d.wGot) <= 1 ? 1 : 0 }, g), true);
  hsVpdCheck(v, "scroll", prev, g);
}

// 明确的用户输入:滚轮、拖拽、触摸、键盘、滚动条。只记来源,不记按了什么键、滚了多少。
function hsVpdInput(v, kind) {
  if (!v || !v.body) return;
  hsVpd(v).input = { t: performance.now(), k: kind };
  hsVpdNote(v, "input", { src: kind });
}

// 在 scroll 事件里调。prev 是上一次已知位置(v.lastTop),geo 是这次事件读到的几何。
// 判据:贴底态、不在重建恢复期、最近没有用户输入、位置比上一次低超过阈值。
// 不区分「JS 写小了」和「浏览器夹小了」——缓冲里的 write 行会说明是哪一种。
function hsVpdCheck(v, from, prev, geo) {
  if (!v || !v.body || prev == null) return;
  const dy = geo.top - prev;
  if (!(dy < -HS_VPD_JUMP) || !v.stick || v.restoringScroll) return;
  const d = hsVpd(v), now = performance.now();
  if (d.input && now - d.input.t < HS_VPD_QUIET) return;
  if (d.dumps >= HS_VPD_MAX || now - d.lastDump < HS_VPD_GAP) { d.held++; return; }
  if (typeof deskLog !== "function") return;
  d.lastDump = now; d.dumps++;
  deskLog(`[vpd] ${v.sid || "-"} 上跳 ${Math.round(-dy)}px 来源=${from} build=${hsVpdBuild()}`
    + ` 第${d.dumps}次 期间抑制=${d.held} `
    + JSON.stringify(Object.assign({ prev: Math.round(prev) }, geo, hsVpdState(v))));
  // 只倒尾部:触发行永远是最新那条,前面几十条足够看清序列;跟随环每帧一条 write,再多是重复
  for (const r of d.ring.slice(-HS_VPD_DUMP)) deskLog("[vpd]  " + JSON.stringify(r));
  d.held = 0;
}

// 每页一个控制器：两种意图（跟随 / 阅读）、一个可取消的帧队列、一个正文位置写入点。
// 业务代码只提交意图或布局事务，不持有跨帧 scrollTop。新意图使旧恢复、旧跟随全部失效。
function hsViewport(v) {
  return v.viewport || (v.viewport = { epoch: 0, frame: 0, anchor: null,
    remaining: 0, depth: 0, until: 0 });
}

function hsViewportCancel(v) {
  const s = hsViewport(v);
  s.epoch++;
  if (s.frame) cancelAnimationFrame(s.frame);
  s.frame = 0; s.anchor = null; s.remaining = 0; s.until = 0;
  if (v.restickTimer) clearTimeout(v.restickTimer);
  v.restickTimer = 0; v.restickNear = false;
  v.restoringScroll = false;
  if (v.body) v.body.classList.remove("hs-restoring");
}

function hsViewportWrite(v, top, why) {
  if (!v.body || !v.body.isConnected || !v.body.clientHeight) return;
  const sh = v.body.scrollHeight, ch = v.body.clientHeight;
  const bottom = Math.max(0, sh - ch);
  const want = top;
  top = Math.max(0, Math.min(bottom, top));
  const before = v.body.scrollTop;
  // _hsVpdOwn 挂在元素上而不是顶层变量:控制器的真实源码会被测试按名抽进 node 跑,顶层变量抽不到
  if (Math.abs(before - top) > 0.5) { v.body._hsVpdOwn = true; try { v.body.scrollTop = top; } finally { v.body._hsVpdOwn = false; } }
  v.lastTop = v.body.scrollTop;
  // 取证:这几个几何值本函数本来就读了,记下来不多付一次布局。
  globalThis.hsVpdWrite?.(v, why, before, want, sh, ch);
  hsTocSchedule(v);
}

// 用户定位只滚正文，不让 scrollIntoView 连带滚动外层，也不留下浏览器自己的平滑动画。
function hsViewportReveal(v, target, align = "nearest") {
  hsUnstick(v);
  const r = target.getBoundingClientRect(), b = v.body.getBoundingClientRect();
  if (!r.height && !r.width) return;
  const delta = align === "start" ? r.top - b.top
    : align === "center" ? r.top - b.top - (b.height - r.height) / 2
    : align === "third" ? r.top - b.top - b.height / 3
    : r.top < b.top ? r.top - b.top : r.bottom > b.bottom ? r.bottom - b.bottom : 0;
  hsViewportWrite(v, v.body.scrollTop + delta, "reveal");
  hsHoldCapture(v);
}

function hsViewportSchedule(v) {
  const s = hsViewport(v);
  if (s.frame || s.depth || !v.body || !v.body.isConnected || !v.body.clientHeight) return;
  const epoch = s.epoch;
  s.frame = requestAnimationFrame(() => {
    if (s.epoch !== epoch) return;
    s.frame = 0;
    if (!v.body.isConnected || !v.body.clientHeight) return;
    hsViewportCommit(v, v.stick && !!v.busy);
    if (s.anchor && --s.remaining <= 0) {
      s.anchor = null; v.restoringScroll = false;
      v.body.classList.remove("hs-restoring");
      hsHoldCapture(v);
    }
    if (s.anchor || (v.stick && (v.busy || performance.now() < s.until))) hsViewportSchedule(v);
  });
}

function hsViewportCommit(v, smooth = false) {
  const s = hsViewport(v);
  if (s.depth || !v.body || !v.body.isConnected || !v.body.clientHeight) return false;
  if (v.stick) hsViewportWrite(v, smooth ? hsFollowTarget(v.body)
    : v.body.scrollHeight - v.body.clientHeight, smooth ? "follow" : "bottom");
  else if (s.anchor) hsViewportApplyAnchor(v, s.anchor);
  else hsHoldAdjust(v);
  return true;
}

// DOM 替换、补历史和裁剪都经过同一个同步布局事务。先完成折叠和投影，再恢复位置；
// 延迟图片等后续变化由同一个控制器接管，而不是再发起一条独立的补偿循环。
function hsViewportChange(v, change, replace = false, why = "") {
  const s = hsViewport(v);
  if (s.depth) return change();
  const epoch = s.epoch, anchor = hsViewportAnchor(v);
  globalThis.hsVpdNote?.(v, "tx", { why: why || (replace ? "replace" : "change"), ph: 0 }, true);
  s.depth++;
  try { return change(); }
  finally {
    try { hsMeasureBubbles(v); }
    finally {
      s.depth--;
      globalThis.hsVpdNote?.(v, "tx", { why: why || (replace ? "replace" : "change"), ph: 1, same: epoch === s.epoch ? 1 : 0 });
      if (epoch === s.epoch && !v.historyRevealing) {
        if (v.stick) hsScrollCommit(v, true);
        else if (anchor) hsRestoreViewportAnchor(v, anchor, replace);
        else hsViewportCommit(v);
      }
    }
  }
}

function hsSetStick(v, on) {
  if (!v) return false;
  const next = !!on, was = !!v.stick;
  globalThis.hsVpdNote?.(v, "stick", { on: next ? 1 : 0, was: was ? 1 : 0 });
  hsViewportCancel(v);
  v.stick = next;
  if (next) v.bottomIntent = false;
  if (v.body) v.body.classList.toggle("hs-follow", next);
  if (v.down) v.down.classList.toggle("on", !next);
  if (next) {
    // 手机上向上分页会临时挂回更多历史；回到底部立刻恢复硬预算，而不是等下一轮才裁。
    if (!was && typeof hsTrimDom === "function") hsTrimDom(v);
    hsScrollCommit(v, true);
    if (v.busy) hsViewportSchedule(v);
  }
  return next;
}

// iOS 到底后的弹性回弹会把最后一帧记成“向上”，而惯性滚动也可能在脱钩静默窗内结束。
// 每条 scroll 都重置计时，停稳后只看真实位置；停在上文时 gap 自然过不了，不会被拉走。
function hsRestickAfterScroll(v, towardBottom) {
  if (!v) return;
  const epoch = hsViewport(v).epoch;
  // 一次惯性滚动的最后几帧可能回弹成向上；只要这段滚动此前明确向下过，就保留 80px
  // 的“已到底”区间直到停稳。没有向下意图的被动 scroll 仍只认 4px，内容增长不会抓人。
  if (towardBottom) v.restickNear = true;
  if (v.restickTimer) clearTimeout(v.restickTimer);
  const quietLeft = 300 - (performance.now() - (v.unstickAt || 0));
  v.restickTimer = setTimeout(() => {
    if (epoch !== hsViewport(v).epoch) return;
    v.restickTimer = 0;
    const near = v.restickNear;
    v.restickNear = false;
    v.bottomIntent = false;
    if (!v.body || v.stick || v.restoringScroll || !v.body.isConnected) return;
    const gap = v.body.scrollHeight - v.body.scrollTop - v.body.clientHeight;
    const limit = near ? 80 : 4;
    if (gap >= limit) return;
    hsSetStick(v, true);
    hsScrollCommit(v);
  }, Math.max(90, quietLeft + 16));
}

function hsScrollCommit(v, instant = false) {
  if (!v || !v.stick) return false;
  // 流式布局通知只唤醒唯一帧队列，不在 RO 和 rAF 中各推进一次缓动。
  if (v.busy && !instant) { hsViewportSchedule(v); return true; }
  return hsViewportCommit(v);
}

// 会话中所有会改变纵向几何的区域都在这里登记：正文覆盖流式文字、工具、图片与过程区；
// composer 覆盖目标、任务、系统提示、提问、附件与输入框；body 覆盖窗口和左右栏改宽后的
// 重新换行。ResizeObserver 的通知位于 layout 之后、paint 之前，同步写滚动位置不会再露出
// 一帧“内容先长高、视口下一帧才追上”的中间态。
// 刻度列(.hs-toc)钉在「标题栏下沿到输入框上沿」的正中。它的定位基准是整个会话页,
// 输入区也在里面,所以 CSS 要把输入区那一截减掉(见 style.css 的 .hs-toc)。这里给的是
// 「输入框为空、任务清单收起时」的输入区高度:打字或展开任务都会把输入区顶高,按当前高度
// 算的话刻度会跟着移动,而它标的是正文的位置,不该被这两种临时展开牵动。
// 空态行高按 line-height 与 min-height 取大者:两者随字号变(手机 16px 那档行高就超过
// 24px 的下限),写死一个数在别的字号下会偏。任务展开增量直接量列表与新增行；折叠标题行
// 仍属于输入区的稳定边界，不扣除。
function hsTocAnchor(v) {
  const inp = v && v.inp;
  const composer = v && v.el && v.el.querySelector(".hs-composer");
  if (!inp || !composer || !v.el.isConnected) return;
  const cs = getComputedStyle(inp);
  const line = Math.max(parseFloat(cs.lineHeight) || 0, parseFloat(cs.minHeight) || 0);
  const grow = Math.max(0, inp.getBoundingClientRect().height - line);
  const tasks = composer.querySelector(".hs-tasks.open");
  const taskGrow = tasks
    ? [...tasks.querySelectorAll(".hs-tasks-list, .hs-tasks-add")]
        .reduce((height, el) => height + el.getBoundingClientRect().height, 0)
    : 0;
  const empty = Math.round(composer.getBoundingClientRect().height - grow - taskGrow);
  if (v.tocCompEmpty === empty) return;   // 每敲一个字都会触发观察,值没变就别碰样式
  v.tocCompEmpty = empty;
  v.el.style.setProperty("--hs-comp-empty", empty + "px");
}

function hsObserveLayout(v) {
  if (!v || v.layoutObserver || v.layoutResize) return;
  const composer = v.el && v.el.querySelector(".hs-composer");
  if (typeof ResizeObserver === "function") {
    v.layoutObserver = new ResizeObserver((entries) => {
      if (!v.el || !v.el.isConnected) return;
      globalThis.hsVpdRo?.(v, entries);   // 取证:哪个容器变了、变成多高。保持单行,回调段有测试按文本截取
      if (v.stick) hsScrollCommit(v);
      else { hsViewportCommit(v); if (hsViewport(v).anchor) hsViewportSchedule(v); }
      // 主区宽度跨过信息栏容器断点时，同步页头按钮的启用态。只在整页尺寸变化时做，
      // 流式正文和输入框增高不重复刷新页头。
      if (entries.some((entry) => entry.target === v.el)) {
        if (typeof syncOverlayCornerAction === "function") syncOverlayCornerAction();
        if (typeof apprRehost === "function") apprRehost();
      }
      // 与上面的 hsVpdRo 同一处理:回调段被测试单独截出来 eval,同文件的函数在那个上下文里
      // 并不存在。经 globalThis 可选调用,浏览器里照常命中,截段测试里安静跳过。
      globalThis.hsTocAnchor?.(v);
    });
    [v.stream, composer, v.body, v.el].filter(Boolean).forEach((el) => v.layoutObserver.observe(el));
    return;
  }
  // 极旧 WebView 没有 ResizeObserver 时保留窗口级兜底。常规输出仍由 hsScroll 的 rAF
  // 合并；现代 Desktop 与手机端不会走到这一支。
  v.layoutResize = () => { if (v.stick) hsPin(v, 300); };
  addEventListener("resize", v.layoutResize, { passive: true });
}

// 整页对账会 replaceChildren 整段正文。替换过程中滚动高度会短暂归零，浏览器随即把
// scrollTop 夹到 0；只记绝对 scrollTop 也不够，重建后的段落高度可能因为 markdown、
// 图片和过程折叠发生变化。用「屏幕上缘遇到的第一回合 + 它相对视口的偏移」作锚点，
// 刻度列也记住当前那根的相对位置，重建后按同一语义位置复原。
function hsTurnAnchor(t, index) {
  if (!t) return null;
  return { uid: String(t.uid || ""), seq: Number(t.seq || 0), uat: Number(t.uat || 0),
           msgId: String(t.msgId || ""), user: String(t.user || ""), index };
}

function hsTurnAtAnchor(v, a) {
  if (!v || !a) return null;
  let t = null;
  if (a.uid) t = v.turns.find((x) => String(x.uid || "") === a.uid);
  if (!t && a.seq) t = v.turns.find((x) => Number(x.seq || 0) === a.seq);
  if (!t && a.uat) t = v.turns.find((x) => Number(x.uat || 0) === a.uat && String(x.user || "") === a.user);
  if (!t && a.msgId) t = v.turns.find((x) => String(x.msgId || "") === a.msgId);
  return t || v.turns[Math.max(0, Math.min(v.turns.length - 1, Number(a.index || 0)))] || null;
}

// 回合内部的定位。锚只钉回合顶边是不够的:一个回合可以有几千像素高,而重建前后它的
// 内部高度会变(工具卡数量不同、markdown 重排)。把回合顶边按原偏移放回去,回合内部的
// 内容就整体位移了那个高度差——视口上缘原本在这一回合内部 2430px 处,重建后这一回合
// 只剩 437px,于是整段被推走(2026-09-07 探针实测,位移 917px)。
// 记一条从回合根往下的子节点序号路径,复位时走同一条路径找到同一个块。
// 与 hsHoldCapture 的下钻同一套语义,那边靠元素引用,这边靠序号——重建之后节点全是新的。
function hsBlockPath(root, bodyTop) {
  const path = [];
  let el = root, depth = 0;
  while (el && depth++ < 6) {
    let at = -1, next = null;
    const kids = el.children || [];
    for (let i = 0; i < kids.length; i++) {
      if (kids[i].getBoundingClientRect().bottom > bodyTop + 1) { at = i; next = kids[i]; break; }
    }
    if (!next || next === el) break;
    path.push(at);
    el = next;
  }
  return { path, el };
}

function hsBlockAt(root, path) {
  let el = root;
  for (const i of path || []) {
    const kids = el.children || [];
    if (i < 0 || i >= kids.length) return null;
    el = kids[i];
  }
  return el === root ? null : el;
}

function hsViewportAnchor(v) {
  if (!v || !v.body || v.stick) return null;
  const br = v.body.getBoundingClientRect();
  const mounted = v.turns.filter((t) => t.el && t.el.isConnected);
  const turn = mounted.find((t) => t.el.getBoundingClientRect().bottom > br.top + 1)
            || mounted[mounted.length - 1];
  const index = turn ? v.turns.indexOf(turn) : 0;
  const a = { turn: hsTurnAnchor(turn, index), bodyTop: v.body.scrollTop, bodyOffset: 0,
              blockPath: null, blockOffset: 0,
              tocTop: v.toc ? v.toc.scrollTop : 0, tocTurn: null, tocOffset: 0 };
  if (turn) {
    a.bodyOffset = turn.el.getBoundingClientRect().top - br.top;
    const got = hsBlockPath(turn.el, br.top);
    if (got.el && got.el !== turn.el) {
      a.blockPath = got.path;
      a.blockOffset = got.el.getBoundingClientRect().top - br.top;
    }
  }
  const tick = v.tocAt && v.tocAt._turn ? v.tocAt : null;
  if (tick && v.toc) {
    a.tocTurn = hsTurnAnchor(tick._turn, v.turns.indexOf(tick._turn));
    a.tocOffset = tick.getBoundingClientRect().top - v.toc.getBoundingClientRect().top;
  }
  return a;
}

function hsRestoreViewportAnchor(v, a, settle = true) {
  if (!v || !a || !v.body || v.stick) return;
  hsViewportCancel(v);
  const s = hsViewport(v);
  s.anchor = a; s.remaining = 2;
  v.restoringScroll = true;
  v.body.classList.add("hs-restoring");
  hsViewportCommit(v);
  if (settle) hsViewportSchedule(v);
  else {
    s.anchor = null; s.remaining = 0; v.restoringScroll = false;
    v.body.classList.remove("hs-restoring");
    hsHoldCapture(v);
  }
}

// 只计算当前阅读锚点的位置，由主写入器提交。没有独立定时器，也不改变阅读意图。
function hsViewportApplyAnchor(v, a) {
  const turn = hsTurnAtAnchor(v, a.turn);
  if (turn && turn.el && turn.el.isConnected) {
    // 先按回合内部那个块复位。找不到同一条路径时(重建后结构真的变了)才退回回合顶边——
    // 那一档至少不会比原来更差。
    const block = a.blockPath ? hsBlockAt(turn.el, a.blockPath) : null;
    const base = v.body.getBoundingClientRect().top;
    const delta = block
      ? block.getBoundingClientRect().top - base - a.blockOffset
      : turn.el.getBoundingClientRect().top - base - a.bodyOffset;
    hsViewportWrite(v, v.body.scrollTop + delta, block ? "anchor-block" : "anchor-turn");
  } else {
    hsViewportWrite(v, a.bodyTop, "anchor-abs");
  }
  if (v.toc) {
    const tocTurn = hsTurnAtAnchor(v, a.tocTurn);
    const tick = tocTurn && [...v.toc.querySelectorAll(".hs-tk")].find((x) => x._turn === tocTurn);
    if (tick) {
      const delta = tick.getBoundingClientRect().top - v.toc.getBoundingClientRect().top - a.tocOffset;
      if (Math.abs(delta) > 0.5) v.toc.scrollTop += delta;
    } else v.toc.scrollTop = a.tocTop;
  }
  hsTocSchedule(v);
}

// ---- 非贴底时的自持锚点 ----------------------------------------------------
// 脱钩阅读的位置保持不再依赖浏览器原生锚定,由 Desk 自己补偿。原生锚定在 WebKit 上
// 两头都靠不住:锚选进流式重绘的节点,节点被摘时滚动位直接重置到 0(2026-09-01 实证
// scrollTop 1885→0,无任何 JS 调用);把整个打字块排除出候选之后,视口落在流式块内部
// 的读者又完全失去保护,块定型(列表、代码栅栏收口)的高度变化把内容从手底下推走
// (2026-09-01 用户实见:靠近底部但不在底部,偶发跳动)。自持锚点记「视口上缘的元素 +
// 它相对视口的偏移」,尺寸变化后按同一语义位置补回;引用的元素被换掉时保持原位不动,
// 最坏情况等于没补,永远不会重置。body 常关 overflow-anchor,滚动位只有一条控制链。
function hsHoldCapture(v) {
  if (!v || !v.body) return;
  if (v.stick || v.restoringScroll || !v.body.isConnected || !v.body.clientHeight) {
    v.holdAnchor = null;
    return;
  }
  const br = v.body.getBoundingClientRect();
  const mounted = v.turns.filter((t) => t.el && t.el.isConnected);
  let el = null;
  // 从末尾往回找视口上缘所在的回合:脱钩的人多半在尾部附近,几步就到
  for (let i = mounted.length - 1; i >= 0; i--) {
    const r = mounted[i].el.getBoundingClientRect();
    if (r.top <= br.top + 1) {
      el = r.bottom > br.top + 1 ? mounted[i].el
         : (mounted[i + 1] && mounted[i + 1].el) || mounted[i].el;
      break;
    }
  }
  if (!el && mounted.length) el = mounted[0].el;
  // 巨大的回合(长输出整段在一个回合里)按层下钻,锚到视口上缘那一个块:
  // 回合级的锚看不见回合内部的高度变化,块定型的推挤就补不回来。
  let depth = 0;
  while (el && el.getBoundingClientRect().height > v.body.clientHeight && depth++ < 6) {
    let next = null;
    for (const c of el.children) {
      const r = c.getBoundingClientRect();
      if (r.bottom > br.top + 1) { next = c; break; }
    }
    if (!next || next === el) break;
    el = next;
  }
  v.holdAnchor = el ? { el, off: el.getBoundingClientRect().top - br.top } : null;
}

function hsHoldAdjust(v) {
  if (!v || !v.body || v.stick || v.restoringScroll
      || !v.body.isConnected || !v.body.clientHeight) return;
  const a = v.holdAnchor;
  if (a && a.el && a.el.isConnected) {
    const br = v.body.getBoundingClientRect();
    const delta = a.el.getBoundingClientRect().top - br.top - a.off;
    hsViewportWrite(v, v.body.scrollTop + delta, "hold");
  }
  hsHoldCapture(v);
}

function hsScroll(v) {
  if (!v || !v.stick) return;
  hsViewportSchedule(v);
}

// 用户主动往上走(点刻度、滚轮向上)时立刻脱钩。原来只靠滚动位置判定 stick:流式期间
// 每帧都有一次钉底,向上的平滑滚动和滚轮在挪出 80px 阈值之前就被砸回去,表现为
// 「点了没反应、页面拖不动」,直到吐字出现间隙才跳过去。整段提交(而非逐字流)的引擎
// 锁死的窗口最长。
function hsUnstick(v) {
  v.unstickAt = performance.now();   // 位置回贴的静默窗从这一刻起算(见 scroll 监听)
  v.bottomIntent = false;
  hsSetStick(v, false);
  hsRemount(v);   // 贴底时摘掉的那几轮挂回来,人往上翻要看得到(见 hsTrimDom)
  hsHoldCapture(v);   // 一脱钩就建自持锚点,下一次尺寸变化立刻有得补
}

function hsBusy(v, on) {
  if (!on) {
    v.stallSec = 0; v.retry = null; v.providerWait = null; v.stepLog = ""; v.logBuf = "";
    if (v.busyVerifyTimer) { clearTimeout(v.busyVerifyTimer); v.busyVerifyTimer = 0; }
    if (v.compactT0 || v.compactEl || v.el.classList.contains("hs-compacting")) {
      hsCompactOff(v);
      v.cmpLive = false;
    }
  }
  globalThis.hsVpdNote?.(v, "busy", { on: on ? 1 : 0 });
  v.busy = on;
  hsViewport(v).until = on ? 0 : performance.now() + 600;
  if (v.stick) hsViewportSchedule(v);
  v.el.classList.toggle("hs-busy", !!on);
}
