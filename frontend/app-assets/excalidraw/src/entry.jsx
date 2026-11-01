// desktron「Excalidraw」应用入口:原版全屏画布 + 多画布管理。
// 画布数据落盘在工作台 .workbench/canvases/(一张一个 .excalidraw 文件),经 /workbench/canvases
// 读写,不出网。保存时顺带导出一份同名 .svg,备忘、文档可直接引用这张静态图。
import React, { useCallback, useEffect, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import { Excalidraw, MainMenu, Sidebar, WelcomeScreen, exportToSvg } from "@excalidraw/excalidraw";

const SIDEBAR = "canvases";
const LAST_KEY = "desk_excalidraw_last";
const API = "/workbench/canvases";

/* ---------------- 服务端存储:一张画布一个文件 ---------------- */
async function dbAll() {
  try {
    const r = await fetch(API);
    const j = await r.json();
    return Array.isArray(j.items) ? j.items : [];
  } catch { return []; }
}
async function dbGet(id) {
  try {
    const r = await fetch(`${API}/${encodeURIComponent(id)}`);
    return r.ok ? await r.json() : null;
  } catch { return null; }
}
async function dbPut(rec, svg) {
  try {
    const r = await fetch(`${API}/${encodeURIComponent(rec.id)}`, {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ name: rec.name, created: rec.created,
                             elements: rec.elements || [], appState: rec.appState || {},
                             files: rec.files || {}, svg: svg || undefined }) });
    return r.ok;
  } catch { return false; }
}
async function dbDel(id) {
  try { await fetch(`${API}/${encodeURIComponent(id)}`, { method: "DELETE" }); } catch {}
}
// 当前场景导出成 SVG 字符串;导不出(空画布/异常)返回空串,后端就不动 .svg
async function sceneSvg(elements, appState, files) {
  try {
    const el = (elements || []).filter((e) => !e.isDeleted);
    if (!el.length) return "";
    const svg = await exportToSvg({
      elements: el, files: files || {},
      appState: { exportBackground: true, exportWithDarkMode: appState?.theme === "dark",
                  viewBackgroundColor: appState?.viewBackgroundColor || "#ffffff" },
    });
    return new XMLSerializer().serializeToString(svg);
  } catch { return ""; }
}
// 一次性迁移:老版本把画布存在浏览器 IndexedDB 里,盘上还没有就整体搬过去(不删 IndexedDB,留退路)
async function migrateFromIDB() {
  const olds = await new Promise((resolve) => {
    let done = false;
    const fin = (v) => { if (!done) { done = true; resolve(v); } };
    try {
      const req = indexedDB.open("desk-excalidraw", 1);
      req.onupgradeneeded = () => { try { req.transaction.abort(); } catch {} fin([]); };   // 库本来就不存在
      req.onerror = () => fin([]);
      req.onsuccess = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains("canvases")) return fin([]);
        const q = db.transaction("canvases", "readonly").objectStore("canvases").getAll();
        q.onsuccess = () => fin(q.result || []);
        q.onerror = () => fin([]);
      };
    } catch { fin([]); }
  });
  for (const c of olds) if (c && c.id) await dbPut(c);
  return olds.length;
}

const newId = () => "c-" + Date.now().toString(36) + "-" + Math.random().toString(36).slice(2, 7);
const blankCanvas = (name) => ({
  id: newId(), name: name || "未命名画布", created: Date.now(), updated: Date.now(),
  elements: [], files: {}, appState: {},
});
const fmtTime = (ts) => {
  const d = new Date(ts), p = (n) => String(n).padStart(2, "0");
  const today = new Date(); today.setHours(0, 0, 0, 0);
  if (ts >= today.getTime()) return `${p(d.getHours())}:${p(d.getMinutes())}`;
  return `${d.getMonth() + 1}月${d.getDate()}日`;
};

/* ---------------- 画布列表(挂进 Excalidraw 原生 Sidebar) ---------------- */
function CanvasList({ list, currentId, onPick, onNew, onRename, onDelete, onDuplicate }) {
  const [editing, setEditing] = useState(null);
  const [draft, setDraft] = useState("");
  const commit = () => {
    if (editing) onRename(editing, draft.trim() || "未命名画布");
    setEditing(null);
  };
  return (
    <div className="cv-wrap">
      <button className="cv-new" onClick={onNew} title="新建画布">
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
             strokeWidth="2" strokeLinecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
        新建画布
      </button>
      <div className="cv-list">
        {list.map((c) => (
          <div key={c.id} className={"cv-item" + (c.id === currentId ? " on" : "")}
               onClick={() => c.id !== editing && onPick(c.id)}>
            <div className="cv-main">
              {editing === c.id ? (
                <input className="cv-input" autoFocus value={draft}
                       onChange={(e) => setDraft(e.target.value)} onBlur={commit}
                       onKeyDown={(e) => { if (e.key === "Enter") commit(); if (e.key === "Escape") setEditing(null); }}/>
              ) : (
                <div className="cv-name" onDoubleClick={(e) => { e.stopPropagation(); setEditing(c.id); setDraft(c.name); }}>
                  {c.name}
                </div>
              )}
              <div className="cv-meta">{c.count} 个元素 · {fmtTime(c.updated)}</div>
            </div>
            <div className="cv-ops" onClick={(e) => e.stopPropagation()}>
              <button title="重命名" onClick={() => { setEditing(c.id); setDraft(c.name); }}>
                <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2"
                     strokeLinecap="round" strokeLinejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/></svg>
              </button>
              <button title="复制" onClick={() => onDuplicate(c.id)}>
                <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2"
                     strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/></svg>
              </button>
              <button title="删除" onClick={() => onDelete(c.id)}>
                <svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" strokeWidth="2"
                     strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/></svg>
              </button>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ---------------- 应用主体 ---------------- */
function App() {
  const [api, setApi] = useState(null);
  const [list, setList] = useState([]);          // [{id,name,updated,count}]
  const [current, setCurrent] = useState(null);  // 完整记录
  const [ready, setReady] = useState(false);
  const [docked, setDocked] = useState(true);
  const currentIdRef = useRef(null);
  const saveT = useRef(null);
  const switching = useRef(false);

  // 画布列表在主区可停靠，但右侧紧凑宿主和手机必须把画布本身放在第一位。
  // Excalidraw 会随 iframe 宽度切移动工具条，这里只补它不会自动改变的 docked 状态。
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 820px)");
    const sync = () => { if (mq.matches) setDocked(false); };
    sync();
    mq.addEventListener?.("change", sync);
    return () => mq.removeEventListener?.("change", sync);
  }, []);

  const refreshList = useCallback(async () => {
    setList(await dbAll());   // 服务端已按更新时间倒序,且只回清单字段
  }, []);

  // 起手:先把老 IndexedDB 数据搬上盘;盘上一张也没有就建一张空的;否则打开上次那张
  useEffect(() => {
    (async () => {
      let all = await dbAll();
      if (!all.length) {
        if (await migrateFromIDB()) all = await dbAll();
      }
      if (!all.length) {
        const c = blankCanvas("画布 1");
        await dbPut(c);
        all = await dbAll();
      }
      const last = localStorage.getItem(LAST_KEY);
      const pickId = (all.find((c) => c.id === last) || all[0]).id;
      const rec = (await dbGet(pickId)) || blankCanvas("画布 1");
      currentIdRef.current = rec.id;
      setCurrent(rec);
      setList(all);
      setReady(true);
    })();
  }, [refreshList]);

  // 存盘:onChange 防抖 800ms 落库(切画布过程中不落,免得把新画布写成旧内容)
  const persist = useCallback(async (elements, appState, files) => {
    const id = currentIdRef.current;
    if (!id || switching.current) return;
    const rec = (await dbGet(id)) || blankCanvas();
    rec.id = id;
    rec.elements = elements.filter((e) => !e.isDeleted);
    rec.files = files || {};
    rec.appState = { viewBackgroundColor: appState.viewBackgroundColor, scrollX: appState.scrollX,
                     scrollY: appState.scrollY, zoom: appState.zoom, theme: appState.theme };
    rec.updated = Date.now();
    await dbPut(rec, await sceneSvg(rec.elements, appState, rec.files));   // 落盘同时刷新导出图
    refreshList();
  }, [refreshList]);

  const onChange = useCallback((elements, appState, files) => {
    clearTimeout(saveT.current);
    saveT.current = setTimeout(() => persist(elements, appState, files), 800);
  }, [persist]);

  const openCanvas = useCallback(async (id) => {
    if (!api || id === currentIdRef.current) return;
    clearTimeout(saveT.current);
    // 先把当前这张落盘,再切
    const els = api.getSceneElements(), st = api.getAppState(), fs = api.getFiles();
    await persist(els, st, fs);
    switching.current = true;
    const rec = await dbGet(id);
    if (!rec) { switching.current = false; return; }
    currentIdRef.current = id;
    localStorage.setItem(LAST_KEY, id);
    api.updateScene({ elements: rec.elements || [], appState: { ...(rec.appState || {}) } });
    api.addFiles(Object.values(rec.files || {}));
    if ((rec.elements || []).length) api.scrollToContent(rec.elements, { fitToContent: false });
    setCurrent(rec);
    switching.current = false;
    refreshList();
  }, [api, persist, refreshList]);

  const newCanvas = useCallback(async () => {
    const used = list.map((x) => { const m = /^画布 (\d+)$/.exec(x.name); return m ? +m[1] : 0; });
    const c = blankCanvas(`画布 ${Math.max(0, ...used) + 1}`);
    await dbPut(c);
    await refreshList();
    if (api) {
      clearTimeout(saveT.current);
      const els = api.getSceneElements(), st = api.getAppState(), fs = api.getFiles();
      await persist(els, st, fs);
      switching.current = true;
      currentIdRef.current = c.id;
      localStorage.setItem(LAST_KEY, c.id);
      api.updateScene({ elements: [], appState: { scrollX: 0, scrollY: 0, zoom: { value: 1 } } });
      setCurrent(c);
      switching.current = false;
      refreshList();
    }
  }, [api, list.length, persist, refreshList]);

  const renameCanvas = useCallback(async (id, name) => {
    const rec = await dbGet(id);
    if (!rec) return;
    rec.name = name;
    await dbPut(rec);
    if (id === currentIdRef.current) setCurrent({ ...rec });
    refreshList();
  }, [refreshList]);

  const duplicateCanvas = useCallback(async (id) => {
    const rec = await dbGet(id);
    if (!rec) return;
    const copy = { ...rec, id: newId(), name: rec.name + " 副本", created: Date.now(), updated: Date.now() };
    await dbPut(copy, await sceneSvg(copy.elements, copy.appState, copy.files));
    refreshList();
  }, [refreshList]);

  const deleteCanvas = useCallback(async (id) => {
    const all = await dbAll();
    if (all.length <= 1) return;   // 至少留一张,不留空态
    if (!window.confirm("删除这张画布?此操作不可撤销。")) return;
    await dbDel(id);
    await refreshList();
    if (id === currentIdRef.current) {
      const rest = (await dbAll()).sort((a, b) => b.updated - a.updated);
      currentIdRef.current = null;              // 让 openCanvas 认为是切换
      await openCanvas(rest[0].id);
    }
  }, [openCanvas, refreshList]);

  if (!ready) return null;

  return (
    <Excalidraw
      excalidrawAPI={(a) => { window.__excalidrawAPI = a; setApi(a); }}
      initialData={{
        elements: current?.elements || [],
        appState: { theme: "dark", ...(current?.appState || {}) },
        files: current?.files || {},
        scrollToContent: true,
      }}
      onChange={onChange}
      langCode="zh-CN"
      renderTopRightUI={() => (
        <Sidebar.Trigger name={SIDEBAR} className="cv-trigger" title="画布列表"
          icon={
            <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor"
                 strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="4" width="18" height="7" rx="1.6"/><rect x="3" y="14" width="11" height="6" rx="1.6"/>
            </svg>
          }>
          <span className="cv-trigger-t">{current?.name || "画布"}</span>
        </Sidebar.Trigger>
      )}
    >
      <WelcomeScreen />
      <MainMenu>
        <MainMenu.Item onSelect={newCanvas}>新建画布</MainMenu.Item>
        <MainMenu.Item onSelect={() => api && api.toggleSidebar({ name: SIDEBAR, force: true })}>画布列表</MainMenu.Item>
        <MainMenu.Separator />
        <MainMenu.DefaultItems.LoadScene />
        <MainMenu.DefaultItems.SaveAsImage />
        <MainMenu.DefaultItems.Export />
        <MainMenu.Separator />
        <MainMenu.DefaultItems.ToggleTheme />
        <MainMenu.DefaultItems.ChangeCanvasBackground />
      </MainMenu>
      <Sidebar name={SIDEBAR} docked={docked} onDock={setDocked} className="cv-sidebar">
        <Sidebar.Header>画布</Sidebar.Header>
        <CanvasList list={list} currentId={current?.id} onPick={openCanvas} onNew={newCanvas}
                    onRename={renameCanvas} onDelete={deleteCanvas} onDuplicate={duplicateCanvas} />
      </Sidebar>
    </Excalidraw>
  );
}

createRoot(document.getElementById("app")).render(<App />);
