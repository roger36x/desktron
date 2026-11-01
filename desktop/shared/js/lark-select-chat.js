(() => {
  const targetChat = "__DESKTRON_TARGET_CHAT__";
  const targetTag = "__DESKTRON_TARGET_TAG__";
  const clickResult = __DESKTRON_CLICK_RESULT__;
  if (!targetChat) return;
  const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const compact = (s) => norm(s).replace(/[.。…·\-_/\\\s]/g, "").toLowerCase();
  const elementText = (el) => {
    if (!el) return "";
    return [
      el.innerText,
      el.textContent,
      el.getAttribute && el.getAttribute("aria-label"),
      el.getAttribute && el.getAttribute("title"),
      el.getAttribute && el.getAttribute("data-title"),
      el.getAttribute && el.getAttribute("data-tooltip")
    ].map(norm).filter(Boolean).join(" ");
  };
  const textMatches = (haystack, needle) => {
    const h = norm(haystack);
    const n = norm(needle);
    if (!h || !n) return false;
    if (h.includes(n) || n.includes(h)) return true;
    const ch = compact(h);
    const cn = compact(n);
    return ch.length >= 3 && cn.length >= 3 && (ch.includes(cn) || cn.includes(ch) || cn.startsWith(ch.slice(0, Math.min(8, ch.length))));
  };
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  const rectOk = (r) => r && r.width > 0 && r.height > 0;
  const visible = (el) => {
    if (!el || !(el instanceof Element)) return false;
    const r = el.getBoundingClientRect();
    const st = getComputedStyle(el);
    return rectOk(r) && st.visibility !== "hidden" && st.display !== "none" && r.bottom > 0 && r.top < innerHeight;
  };
  const score = (el, text, preferFeed) => {
    const r = el.getBoundingClientRect();
    let s = Math.abs(elementText(el).length - text.length);
    if (preferFeed && el.closest("[data-feed-id]")) s -= 1000;
    if (r.left > innerWidth * 0.65) s += 500;
    if (r.width > innerWidth * 0.75) s += 300;
    return s + Math.max(0, r.left);
  };
  const clickable = (el) => {
    if (!el) return null;
    const direct = el.closest("[data-feed-id],button,[role='button'],[role='tab'],[role='treeitem'],[role='listitem'],li,a");
    if (direct) return direct;
    let best = el;
    for (let cur = el.parentElement, depth = 0; cur && cur !== document.body && depth < 5; depth++, cur = cur.parentElement) {
      const r = cur.getBoundingClientRect();
      if (rectOk(r) && r.left < innerWidth * 0.45 && r.width >= 80 && r.width <= 420 && r.height >= 20 && r.height <= 72) best = cur;
    }
    return best;
  };
  const byText = (text, preferFeed = false) => {
    const needle = norm(text);
    if (!needle) return null;
    const nodes = Array.from(document.querySelectorAll("[data-feed-id],button,[role='button'],[role='tab'],[role='treeitem'],a,span,div"))
      .filter(visible)
      .filter((el) => textMatches(elementText(el), needle));
    nodes.sort((a, b) => score(a, needle, preferFeed) - score(b, needle, preferFeed));
    return nodes.length ? clickable(nodes[0]) : null;
  };
  const fire = (el) => {
    if (!el) return false;
    el.scrollIntoView({block: "center", inline: "nearest"});
    const r = el.getBoundingClientRect();
    const init = {bubbles: true, cancelable: true, view: window, clientX: r.left + Math.min(24, r.width / 2), clientY: r.top + Math.min(18, r.height / 2)};
    for (const type of ["pointerover", "pointerdown", "pointerup"]) {
      try { el.dispatchEvent(new PointerEvent(type, init)); } catch {}
    }
    for (const type of ["mouseover", "mousedown", "mouseup", "click"]) el.dispatchEvent(new MouseEvent(type, init));
    try { el.click(); } catch {}
    return true;
  };
  const dismissDesktopNotificationBanner = () => {
    const bannerRe = /desktop notifications|Enable desktop notifications|开启桌面通知|桌面通知/i;
    const banners = Array.from(document.querySelectorAll("div,section,aside"))
      .filter(visible)
      .filter((el) => {
        const r = el.getBoundingClientRect();
        if (r.top > Math.max(140, innerHeight * 0.16) || r.height > 140 || r.width < innerWidth * 0.45) return false;
        return bannerRe.test(elementText(el));
      })
      .sort((a, b) => {
        const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
        return (ar.top - br.top) || ((br.width * br.height) - (ar.width * ar.height));
      });
    const banner = banners[0];
    if (!banner) return false;
    let style = document.getElementById("__desktron_lark_banner_css");
    if (!style) {
      style = document.createElement("style");
      style.id = "__desktron_lark_banner_css";
      document.head.appendChild(style);
    }
    style.textContent = ".desktron-hidden-notification-banner{display:none!important;visibility:hidden!important;pointer-events:none!important;}";
    const br = banner.getBoundingClientRect();
    const closeTargets = Array.from(banner.querySelectorAll("button,[role='button'],[aria-label],svg,path,span,div"))
      .filter(visible)
      .map((el) => ({el, r: el.getBoundingClientRect(), text: elementText(el)}))
      .filter(({r, text}) => r.left > br.left + br.width * 0.72 && r.top >= br.top && r.bottom <= br.bottom + 2 && (r.width <= 80 || /close|关闭|dismiss/i.test(text)))
      .sort((a, b) => b.r.left - a.r.left);
    const target = closeTargets[0] && closeTargets[0].el;
    if (target) fire(target);
    else {
      const maybe = document.elementFromPoint(br.right - 28, br.top + Math.min(48, br.height / 2));
      if (maybe) fire(maybe);
    }
    banner.classList.add("desktron-hidden-notification-banner");
    return true;
  };
  const leftScrollers = () => Array.from(document.querySelectorAll("div,aside,section"))
    .filter((el) => {
      if (!visible(el)) return false;
      const r = el.getBoundingClientRect();
      return r.left < innerWidth * 0.65 && r.width >= 160 && r.width <= 720 && el.scrollHeight > el.clientHeight + 80;
    })
    .sort((a, b) => (b.scrollHeight - b.clientHeight) - (a.scrollHeight - a.clientHeight));
  const findWithScroll = async (text, preferFeed) => {
    let el = byText(text, preferFeed);
    if (el) return el;
    for (const scroller of leftScrollers().slice(0, 4)) {
      const max = scroller.scrollHeight - scroller.clientHeight;
      const step = Math.max(180, Math.floor(scroller.clientHeight * 0.75));
      for (let y = 0; y <= max; y += step) {
        scroller.scrollTop = y;
        await sleep(120);
        el = byText(text, preferFeed);
        if (el) return el;
      }
    }
    return null;
  };
  const byTextInRoot = (root, text, preferFeed = false) => {
    const needle = norm(text);
    if (!root || !needle) return null;
    const nodes = Array.from(root.querySelectorAll("[data-feed-id],button,[role='button'],[role='listitem'],li,a,span,div"))
      .filter(visible)
      .filter((el) => textMatches(elementText(el), needle));
    nodes.sort((a, b) => score(a, needle, preferFeed) - score(b, needle, preferFeed));
    return nodes.length ? clickable(nodes[0]) : null;
  };
  const feedListContainers = () => Array.from(document.body.querySelectorAll("section,aside,div"))
    .filter(visible)
    .filter((el) => {
      const r = el.getBoundingClientRect();
      if (r.left < 280 || r.left > innerWidth * 0.62 || r.width < 180 || r.width > 620 || r.height < innerHeight * 0.45) return false;
      const txt = elementText(el);
      const feedCount = el.querySelectorAll("[data-feed-id],[role='listitem'],li").length;
      return feedCount >= 2 || /消息|Chats|Yesterday|AM|PM|\d{1,2}:\d{2}/.test(txt);
    })
    .sort((a, b) => {
      const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
      const as = a.querySelectorAll("[data-feed-id],[role='listitem'],li").length;
      const bs = b.querySelectorAll("[data-feed-id],[role='listitem'],li").length;
      return (bs * 10000 + br.height) - (as * 10000 + ar.height);
    });
  const findChatInFeed = async (text) => {
    for (let attempt = 0; attempt < 24; attempt++) {
      const containers = feedListContainers().slice(0, 3);
      for (const root of containers) {
        let el = byTextInRoot(root, text, true);
        if (el) return el;
        const max = Math.max(0, root.scrollHeight - root.clientHeight);
        const step = Math.max(180, Math.floor(root.clientHeight * 0.75));
        for (let y = 0; y <= max; y += step) {
          root.scrollTop = y;
          await sleep(120);
          el = byTextInRoot(root, text, true);
          if (el) return el;
        }
      }
      await sleep(500);
    }
    return null;
  };
  const textInputs = (root = document) => Array.from(root.querySelectorAll("input,textarea,[contenteditable='true'],[role='textbox']"))
    .filter(visible)
    .sort((a, b) => {
      const ar = a.getBoundingClientRect(), br = b.getBoundingClientRect();
      return (br.width - ar.width) || (ar.top - br.top);
    });
  const globalSearchInputs = () => {
    const active = document.activeElement;
    const activeList = active && visible(active) ? [active] : [];
    return activeList.concat(textInputs())
      .filter((el, idx, arr) => arr.indexOf(el) === idx)
      .filter((el) => {
        const r = el.getBoundingClientRect();
        return r.top < Math.max(180, innerHeight * 0.28) && r.width > Math.min(360, innerWidth * 0.28);
      });
  };
  const inputValue = (el, value) => {
    if (!el) return false;
    el.focus();
    try {
      if ("value" in el) {
        const proto = Object.getPrototypeOf(el);
        const desc = Object.getOwnPropertyDescriptor(proto, "value");
        if (desc && desc.set) desc.set.call(el, value);
        else el.value = value;
      } else {
        el.textContent = value;
      }
      el.dispatchEvent(new InputEvent("input", {bubbles: true, inputType: "insertText", data: value}));
    } catch {
      try { el.value = value; el.dispatchEvent(new Event("input", {bubbles: true})); } catch {}
    }
    el.dispatchEvent(new Event("change", {bubbles: true}));
    return true;
  };
  const pressEnterGlobalSearch = (input) => {
    const init = {key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true};
    for (const target of [document.activeElement, input, document.body, document, window].filter(Boolean)) {
      try { target.dispatchEvent(new KeyboardEvent("keydown", init)); } catch {}
      try { target.dispatchEvent(new KeyboardEvent("keypress", init)); } catch {}
      try { target.dispatchEvent(new KeyboardEvent("keyup", init)); } catch {}
    }
    return true;
  };
  const openGlobalSearch = async () => {
    const keyInit = {key: "k", code: "KeyK", metaKey: true, bubbles: true, cancelable: true};
    for (const target of [document.activeElement, document.body, document, window].filter(Boolean)) {
      try { target.dispatchEvent(new KeyboardEvent("keydown", keyInit)); } catch {}
    }
    await sleep(500);
    let input = globalSearchInputs()[0];
    if (input) return input;
    const searchButton = byText("Search", false) || byText("搜索", false);
    if (searchButton) { fire(searchButton); await sleep(500); }
    input = globalSearchInputs()[0];
    return input || null;
  };
  const searchDialogRoot = (input) => {
    if (!input) return document.body;
    const roots = [];
    for (let cur = input.parentElement; cur && cur !== document.body; cur = cur.parentElement) {
      const r = cur.getBoundingClientRect();
      if (!rectOk(r)) continue;
      if (r.width > innerWidth * 0.35 && r.height > innerHeight * 0.25) roots.push({el: cur, r, text: elementText(cur)});
    }
    roots.sort((a, b) => {
      const am = /群组|群聊|Groups|Chats|云文档|Docs|筛选|Filter/.test(a.text) ? 1 : 0;
      const bm = /群组|群聊|Groups|Chats|云文档|Docs|筛选|Filter/.test(b.text) ? 1 : 0;
      return (bm - am) || ((b.r.width * b.r.height) - (a.r.width * a.r.height));
    });
    return roots.length ? roots[0].el : document.body;
  };
  const clickGroupSearchTab = async (root) => {
    for (const label of ["群组", "群聊", "Groups", "Group Chats"]) {
      const tab = byTextInRoot(root, label, false);
      if (tab) { fire(tab); await sleep(350); return true; }
    }
    return false;
  };
  const findSearchResult = (root, text) => {
    const needle = norm(text);
    const searchResultClickable = (el) => {
      if (!el) return null;
      const direct = el.closest("[role='option'],[role='listitem'],li,a,button");
      if (direct) return direct;
      let best = el;
      for (let cur = el.parentElement, depth = 0; cur && cur !== document.body && depth < 8; depth++, cur = cur.parentElement) {
        const r = cur.getBoundingClientRect();
        if (!rectOk(r)) continue;
        if (r.width >= 180 && r.height >= 36 && r.height <= 120 && r.left < innerWidth * 0.92) best = cur;
      }
      return best;
    };
    const nodes = Array.from(root.querySelectorAll("[role='option'],[role='listitem'],li,a,button,div,span"))
      .filter(visible)
      .filter((el) => {
        if (el.matches && el.matches("input,textarea,[contenteditable='true'],[role='textbox']")) return false;
        const r = el.getBoundingClientRect();
        if (r.top < 110 || r.height < 22 || r.width < 80) return false;
        return textMatches(elementText(el), needle);
      });
    nodes.sort((a, b) => score(a, needle, false) - score(b, needle, false));
    return nodes.length ? searchResultClickable(nodes[0]) : null;
  };
  const searchDialogOpen = (text) => {
    return globalSearchInputs().some((input) => {
      const value = "value" in input ? input.value : input.textContent;
      if (!textMatches(value, text)) return false;
      const root = searchDialogRoot(input);
      return /Search results|搜索结果|to select|筛选|群组|群聊|Groups/.test(elementText(root));
    });
  };
  const waitForSearchDialogClosed = async (text) => {
    for (let i = 0; i < 40; i++) {
      if (!searchDialogOpen(text)) return true;
      await sleep(250);
    }
    return false;
  };
  const findChatPane = (text) => {
    const candidates = Array.from(document.querySelectorAll("main,section,article,div"))
      .filter(visible)
      .map((el) => ({el, r: el.getBoundingClientRect(), text: elementText(el)}))
      .filter(({r, text: paneText}) => {
        if (r.left < 120 || r.right < innerWidth * 0.72) return false;
        if (r.width < innerWidth * 0.28 || r.height < innerHeight * 0.55) return false;
        if (/Search results|搜索结果|to select|筛选/.test(paneText)) return false;
        return textMatches(paneText, text) || /Aa|Send|发送|Reply|回复|Have a great day|Set off/.test(paneText);
      });
    candidates.sort((a, b) => {
      const ah = textMatches(a.text, text) ? 1 : 0;
      const bh = textMatches(b.text, text) ? 1 : 0;
      return (bh - ah) || (a.r.left - b.r.left) || ((b.r.width * b.r.height) - (a.r.width * a.r.height));
    });
    return candidates.length ? candidates[0].el : null;
  };
  const applyChatFocus = (pane) => {
    if (!pane) return false;
    let style = document.getElementById("__desktron_lark_focus_css");
    if (!style) {
      style = document.createElement("style");
      style.id = "__desktron_lark_focus_css";
      document.head.appendChild(style);
    }
    style.textContent = `
      .desktron-hidden-left-panel{display:none!important;visibility:hidden!important;pointer-events:none!important;}
      .desktron-focused-chat-pane{flex:1 1 auto!important;width:100%!important;max-width:none!important;min-width:0!important;}
    `;
    const paneRect = pane.getBoundingClientRect();
    const cutoff = Math.max(120, Math.min(innerWidth * 0.72, paneRect.left + 8));
    const panels = Array.from(document.querySelectorAll("aside,nav,section,div"))
      .filter((el) => el !== pane && !el.contains(pane) && visible(el))
      .filter((el) => {
        const r = el.getBoundingClientRect();
        if (r.left < -8 || r.right > cutoff) return false;
        if (r.width < 32 || r.width > Math.max(760, cutoff + 16)) return false;
        return r.height > innerHeight * 0.42 && r.top < innerHeight * 0.38;
      });
    panels.forEach((el) => el.classList.add("desktron-hidden-left-panel"));
    pane.classList.add("desktron-focused-chat-pane");
    for (let cur = pane.parentElement; cur && cur !== document.body; cur = cur.parentElement) {
      const r = cur.getBoundingClientRect();
      if (r.right > innerWidth * 0.72 && r.width > innerWidth * 0.32) {
        cur.style.minWidth = "0";
        cur.style.maxWidth = "none";
        cur.style.flex = "1 1 auto";
        cur.style.width = "100%";
      }
    }
    window.__desktronLarkSelectChat.status = "focused_chat_window";
    return true;
  };
  const focusMessengerChat = async (text) => {
    window.__desktronLarkSelectChat.status = "waiting_chat_window";
    await waitForSearchDialogClosed(text);
    dismissDesktopNotificationBanner();
    for (let i = 0; i < 24; i++) {
      const pane = findChatPane(text);
      if (pane && applyChatFocus(pane)) {
        if (window.__desktronLarkFocusTimer) clearInterval(window.__desktronLarkFocusTimer);
        let ticks = 0;
        window.__desktronLarkFocusTimer = setInterval(() => {
          ticks += 1;
          dismissDesktopNotificationBanner();
          const livePane = findChatPane(text);
          if (livePane) applyChatFocus(livePane);
          if (ticks >= 24) clearInterval(window.__desktronLarkFocusTimer);
        }, 500);
        return true;
      }
      await sleep(250);
    }
    return false;
  };
  const selectChatByGlobalSearch = async (text) => {
    window.__desktronLarkSelectChat.status = "opening_global_search";
    dismissDesktopNotificationBanner();
    const input = await openGlobalSearch();
    if (!input) { window.__desktronLarkSelectChat.status = "search_input_not_found"; return false; }
    const root = searchDialogRoot(input);
    await clickGroupSearchTab(root);
    inputValue(input, text);
    await sleep(900);
    if (!clickResult) {
      window.__desktronLarkSelectChat.status = "search_results_ready";
      return true;
    }
    for (let i = 0; i < 60; i++) {
      window.__desktronLarkSelectChat.status = "polling_search_results";
      const liveRoot = searchDialogRoot(globalSearchInputs()[0] || input);
      const result = findSearchResult(liveRoot, text) || findSearchResult(root, text) || findSearchResult(document.body, text);
      if (result) {
        fire(result);
        window.__desktronLarkSelectChat.status = "clicked_global_search";
        await sleep(120);
        await pressEnterGlobalSearch(input);
        window.__desktronLarkSelectChat.status = "selected_global_search_result";
        await focusMessengerChat(text);
        return true;
      }
      await sleep(500);
    }
    try {
      pressEnterGlobalSearch(input);
      window.__desktronLarkSelectChat.status = "pressed_enter_global_search";
      await focusMessengerChat(text);
      return true;
    } catch {}
    window.__desktronLarkSelectChat.status = "global_search_failed_fallback_tag";
    try { document.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", code: "Escape", bubbles: true, cancelable: true})); } catch {}
    return false;
  };
  const run = async () => {
    window.__desktronLarkSelectChat = {targetChat, targetTag, startedAt: Date.now(), status: "running"};
    for (let i = 0; i < 80; i++) {
      if (norm(document.body && document.body.innerText).length > 80) break;
      await sleep(250);
    }
    dismissDesktopNotificationBanner();
    let selected = await selectChatByGlobalSearch(targetChat);
    if (!clickResult) return;
    if (!selected) {
      if (targetTag) {
        const tag = await findWithScroll(targetTag, false);
        if (tag) { fire(tag); await sleep(1200); }
        else { window.__desktronLarkSelectChat.status = "tag_not_found"; return; }
      }
      const chat = await findChatInFeed(targetChat);
      if (chat) {
        fire(chat);
        selected = true;
        window.__desktronLarkSelectChat.status = "clicked_tag_feed";
      }
    }
    if (selected) {
      const focused = await focusMessengerChat(targetChat);
      if (!focused) window.__desktronLarkSelectChat.status = "selected_no_collapse";
    } else {
      window.__desktronLarkSelectChat.status = "chat_not_found";
    }
  };
  run().catch((err) => { window.__desktronLarkSelectChat = {targetChat, targetTag, status: "error", error: String(err && err.message || err)}; });
})();
