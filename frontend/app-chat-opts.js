// 由 app-chat.js 分拆(2026-08-27,纯搬移;specs/2026-08-27-repo-partition-2.md),第 9/13 片
// ---- 模式与模型 ------------------------------------------------------------
// Claude 暂沿用引擎级选项。Codex 的
// Mode / Sandbox / Model / Effort 来自 app-server，并写入当前 GUI 会话；
// 不同会话可以维持不同设置。

// 档位名一律用 claude 自己的说法(取自二进制里的模式表与 shift+tab 提示语),不另起中文译名:
// 这四个字样在 claude 的状态行、/permissions、文档里都是同一套,换个叫法只会多一层对照。
// default→manual mode、plan→plan mode、acceptEdits→auto-accept edit mode、
// bypassPermissions→bypass permissions mode。注意 claude 另有一个真的叫 auto mode 的档
//(--permission-mode auto),和 acceptEdits 不是一回事,别拿 auto 去指 acceptEdits。
const HS_MODES = [
  { id: "default", label: "manual mode" },
  { id: "acceptEdits", label: "auto-accept edit mode" },
  { id: "auto", label: "auto mode" },
  { id: "bypassPermissions", label: "bypass permissions mode" },
  { id: "plan", label: "plan mode" },
];
// --model 支持族名别名(opus/sonnet/haiku)和完整模型名,界面上显示到版本号,
// 否则「Opus」这一档在几个大版本之间是分不出来的。
// 别名而不是完整模型名:别名跟着上游滚到最新一版,写死 claude-opus-5 这类全名的话,
// 下一次换代就得改这里。CLI 帮助里列出的别名是 fable / opus / sonnet,haiku 同样认。
// 2026-08-09 实测 fable 在本机可用,落到 claude-fable-5;2026-09-02 复测已滚到
// claude-fable-5-1(别名机制按预期跟上,不必改传参)。这里的标签只是「点开菜单时看到什么」,
// 跑起来之后档位钮显示的是引擎自报的真实版本(见下面 hsOptsPaint 里 realModel 那一行),
// 所以标签落后一版不会误导正在跑的会话——但菜单本身仍要写准,否则选之前判断不了。
const HS_MODELS = [
  { id: "", label: "默认" }, { id: "fable", label: "Fable 5.1" },
  { id: "opus", label: "Opus 5.5" },
  // 固定旧版本必须用完整 id，不能复用会随上游升级的 opus 别名。
  { id: "claude-opus-4-6", label: "Opus 4.6" },
  { id: "sonnet", label: "Sonnet 5.5" }, { id: "haiku", label: "Haiku 4.5" },
];
// 引擎报回来的真实模型 id → 好看的名字。选「默认」时显示的就是它实际跑在哪个模型上。
function hsPrettyModel(id) {
  const m = String(id || "").match(/(opus|sonnet|haiku|fable)[-_]?(\d+(?:[-.]\d+)?)/i);
  if (!m) return "";
  return m[1].charAt(0).toUpperCase() + m[1].slice(1) + " " + m[2].replace("-", ".");
}
// --effort 的取值来自 claude --help:low / medium / high / xhigh / max
const HS_EFFORTS = [
  { id: "low", label: "Low" }, { id: "medium", label: "Medium" },
  { id: "high", label: "High" }, { id: "xhigh", label: "XHigh" }, { id: "max", label: "Max" },
];
// Claude/Codex 使用人能读懂的窗口档位。Pi 不使用这组设置：它按当前模型的
// contextWindow 与原生 reserveTokens 自动压缩，切换模型后阈值随模型变化。
const HS_AUTOCOMPACTS = [
  { id: "", label: "自动" }, { id: "200000", label: "20 万" },
  { id: "400000", label: "40 万" }, { id: "600000", label: "60 万" },
];

// 各引擎的档位不是同一件事,不能共用一张表:claude 的模式是权限档,codex 的是沙箱档。
// 名字一律用各引擎自己的说法,不另起统一译名——换个叫法只会让人在界面与引擎文档
// 之间多一层对照。
//
// codex 的模型不在这里切:它的模型名由用户自己那份配置定(~/.codex/config.toml),
// 工作台不写那份配置,界面上列一批猜来的名字,选中即是一次硬失败。
// 默认模型经 /config 显示,只读。
// codex 的模式、沙箱三档、「模型与 effort 由引擎自报」集中在一个常量里。
const HS_UI_CODEX = {
  modes: [{ id: "default", label: "Default" }, { id: "plan", label: "Plan" }],
  modeDefault: "default", modeKey: "collaboration_mode", models: null, efforts: null,
  autocompacts: HS_AUTOCOMPACTS,
  sandboxes: [{ id: "read-only", label: "Read only" },
              { id: "workspace-write", label: "Workspace write" },
              { id: "danger-full-access", label: "Full access" }],
};
const HS_ENGINE_UI = {
  claude: {
    modes: HS_MODES, modeDefault: "default",
    models: HS_MODELS, efforts: HS_EFFORTS, effortDefault: "high",
    autocompacts: HS_AUTOCOMPACTS, ultracode: true,
  },
  codex: HS_UI_CODEX,
  // Pi 冷启动目录尚未回来时的回退；模型与 thinking 档仍以 RPC 自报为准。
  pi: {
    modes: [{ id: "default", label: "manual mode" },
            { id: "acceptEdits", label: "auto-accept edit mode" },
            { id: "bypassPermissions", label: "bypass permissions mode" }],
    modeDefault: "default", models: null, efforts: null,
    autocompacts: null,
  },
};

// 表里没有的引擎不回落到某一个具体引擎:回落 claude 的后果就是 deepseek 会话上列着
// Fable 5 / Opus 5 与 Effort、ultracode,那是另一个引擎的档位,选中即是一次硬失败。
// 什么都不给,界面各处自会把对应的档位收起来——不显示比显示一组假的诚实。
const HS_UI_NONE = { modes: null, modeDefault: "", models: null, efforts: null };

function hsUI(v) {
  return HS_ENGINE_UI[v && v.engine] || HS_UI_NONE;
}

// 「模式」这一档在各引擎里叫的键不同:claude 的权限档存 mode,codex 家族的协作档存
// collaboration_mode。原来这里按引擎名判(hsIsCodexLike),接一家新引擎就得回来改三处。
// 现在键名与默认档由后端 options_catalog().ui 自报,前端只读声明,不认引擎名。
// 静态表那一路是 catalog 还没到时的回落(与 modes / modeDefault 同一个口径):后端还没
// 重启完就打开一段 codex 会话时,少了它就会按 mode 去读写,切档写不到引擎认的那个键。
function hsModeKey(v) {
  return ((v.catalog || {}).ui || {}).modeKey || hsUI(v).modeKey || "mode";
}
function hsModeDefault(v) {
  const ui = (v.catalog || {}).ui || {};
  return ui.modeDefault || hsUI(v).modeDefault || "";
}
// 这一段会话当前落在哪一档
function hsModeId(v) {
  return (v.opts || {})[hsModeKey(v)] || hsModeDefault(v);
}

// 模式清单与模型清单同一个判据:引擎自报的那份优先,没有才用前端这张静态表。
function hsModes(v) {
  const live = (v.catalog || {}).modes || [];
  return live.length ? live : (hsUI(v).modes || []);
}
// 模型清单:引擎自报的那份优先,没有才用前端这张静态表。
// 原来按「是不是 codex 家族」分流,于是 2026-08-17 接 pi 时它明明自报了清单也用不上——
// 档位菜单一项都没有,点开只弹「模型由它自己的配置决定」。判据换成「有没有自报」之后,
// claude 不受影响(它的 catalog 里没有 models),再来一家自报清单的也不用改这里。
function hsModels(v) {
  const live = (v.catalog || {}).models || [];
  return live.length ? live : (hsUI(v).models || []);
}
function hsModel(v) {
  const list = hsModels(v), configured = (v.opts || {}).model || "";
  const actual = String(v.realModel || "");
  const find = (id) => list.find((x) => x.id === id
    || (x.variants || []).some((route) => route.id === id));
  // /model 会直接改变 Pi 的真实路由而不改 Desk opts；能在目录中精确识别时以引擎自报为准。
  // 菜单点选会先清 realModel，因此不会被上一个回合的旧自报值盖住。
  return (actual && find(actual)) || find(configured)
    || list.find((x) => x.default) || list[0] || null;
}
function hsModelRoute(v, model) {
  const m = model || hsModel(v);
  if (!m) return null;
  const routes = m.variants || [];
  if (!routes.length) return m;
  const actual = String(v.realModel || "");
  const opts = v.opts || {};
  const id = opts.model || "";
  // Pi 把规格编码进精确 model id；目录也可用 variantOption 声明单独的规格键，
  // 前端仍渲染同一套“规格”菜单，不按引擎名分叉。
  const variant = m.variantOption ? String(opts[m.variantOption] || "") : "";
  return (actual && routes.find((route) => route.id === actual))
    || routes.find((route) => route.id === id)
    || (variant && routes.find((route) => route.id === variant || route.key === variant))
    || routes.find((route) => route.key === m.defaultVariant)
    || routes.find((route) => route.key === "standard") || routes[0] || m;
}
function hsModelRoutePatch(model, route) {
  if (!model || !route) return {};
  return model.variantOption
    ? { model: model.id, [model.variantOption]: route.id }
    : { model: route.id };
}
function hsVariants(v) {
  return (hsModel(v) || {}).variants || [];
}
function hsRuntimeModelLabel(v, value) {
  const id = String(value || "");
  for (const model of hsModels(v)) {
    if (model.id === id || (model.variants || []).some((route) => route.id === id)) return model.label;
  }
  return id;
}
// 选项标签存的是中文（HS_MODELS、HS_AUTOCOMPACTS），只在显示处随界面语言变化。
function hsOptLabel(label) {
  return DeskI18n.label(label, "model.");
}
function hsCollapsedModelLabel(value) {
  // 折叠钮只回答“当前是什么模型”。负载、额度与快照新鲜度属于选择时的比较信息，
  // 继续保留在模型菜单里，避免常驻状态把模型名挤成省略号。
  return String(value || "").split(" · ").filter((part) =>
    !/^(?:负载|额度)(?:\s|$)/.test(part) && part !== "状态待刷新" && part !== DeskI18n.t("model.stale")
  ).join(" · ");
}
function hsEngineLabel(v, engine) {
  return ((((v || {}).engineSnap || {}).engines || {})[engine] || {}).label || engine;
}

// 引擎切换会依次收到本地乐观值、PUT 回包、session.settings 与 /config 回读。
// 这些帧多数内容相同；对 textContent / hidden 做空写仍会让 WebKit 重建文本节点、
// 触发输入区与长会话正文重新布局。统一只在值真的变化时碰 DOM。
function hsUiText(el, value) {
  if (!el) return false;
  const text = String(value == null ? "" : value);
  if (el.textContent === text) return false;
  el.textContent = text;
  return true;
}
function hsUiHidden(el, hidden) {
  if (!el) return false;
  const value = !!hidden;
  if (el.hidden === value) return false;
  el.hidden = value;
  return true;
}
function hsShepherdEngines(v) {
  const snap = (v || {}).engineSnap || {};
  return Object.entries(snap.engines || {}).map(([id, meta]) => ({
    id, label: meta.label || id, available: !snap.available || snap.available[id] !== false,
  }));
}
function hsEfforts(v) {
  // effort 挂在模型上还是引擎上,由引擎自报的那份说了算:codex 家族每个模型能开哪几档不同,
  // pi 的七档对所有模型一样(仍照这个形状挂在模型上)。模型上没写才回落到引擎级常量表(claude)。
  const own = (hsModelRoute(v) || {}).efforts;
  if (own && own.length) return own;
  return hsUI(v).efforts || [];
}
function hsAutocompacts(v) {
  // 档位可以挂在模型或规格上（Shepherd Pi 只给长上下文模型），与 effort 同一取法：
  // 当前路由声明了就以它为准，没声明才看引擎级声明。
  const own = (hsModelRoute(v) || {}).autocompacts;
  if (Array.isArray(own)) return own;
  if (((v.catalog || {}).models || []).some((m) => Array.isArray(m.autocompacts)
      || (m.variants || []).some((route) => Array.isArray(route.autocompacts)))) return [];
  // 能力由后端声明；静态表只服务后端还没重启、/config 暂时取不到的过渡窗口。
  const live = ((v.catalog || {}).ui || {}).autocompacts;
  return Array.isArray(live) ? live : (hsUI(v).autocompacts || []);
}
function hsEffortDefault(v) {
  // 模式自带默认 effort 的,以它为先(codex 家族的协作档就带)。判据是这一档有没有写,
  // 不是引擎叫什么——没写的引擎自然落到下面按模型取的那一路。
  const mode = hsModes(v).find((x) => x.id === hsModeId(v));
  if (mode && mode.defaultEffort) return mode.defaultEffort;
  const model = hsModelRoute(v);
  return (model && model.defaultEffort) || hsUI(v).effortDefault || "";
}

function hsNewSessionEngine(wantShepherd) {
  const saved = typeof engineInfo !== "undefined" && engineInfo && engineInfo.shepherd_engine;
  return wantShepherd && ["claude", "codex", "pi"].includes(saved) ? saved : curEngine;
}

async function hsEngineSync(v) {
  const shepherdSyncEpoch = v.shepherdPickEpoch || 0;
  // 引擎那份开机时已经取过(applyEnginePick 存进 engineInfo,而开机数据现在直接内联在
  // 首页里)。这里再打一趟是白花一个往返——经隧道时就是一两秒。切引擎会整页重载,
  // 所以 engineInfo 不会陈旧;拿不到才现取。
  let st = (typeof engineInfo !== "undefined" && engineInfo) || null;
  if (!st) {
    try { st = await api("/workbench/engine"); } catch {}
  }
  // 引擎能力位:回退只有 claude 有,分支 codex 没有,按位显隐入口。
  // engine 也从这里取——档位表、opts 存哪个键、切终端传哪个引擎都按它分流,
  // 前端不再假定「GUI 就是 claude」。
  try {
    const configPath = v.ephemeral
      ? "/config?engine=" + encodeURIComponent(hsNewSessionEngine(
          typeof shepherdMode !== "undefined" && shepherdMode))
      : "/config?session=" + encodeURIComponent(v.sid);
    const cf = (await api(configPath)) || {};
    if (v.shepherdMode && shepherdSyncEpoch !== (v.shepherdPickEpoch || 0)) return;
    v.engine = cf.engine || "claude";
    v.shepherdMode = v.ephemeral
      ? (typeof shepherdMode !== "undefined" && !!shepherdMode) : !!cf.shepherd_mode;
    v.shepherdTrial = !!cf.shepherd_trial;
    v.caps = cf.caps || {};
    v.execution = cf.execution || null;
    v.producerEngine = (v.execution || {}).producer_engine || v.engine;
    v.slashEng = cf.slash || [];
    v.commandManifest = cf.command_manifest || null;
    // 后端没报过时只回引擎名(没有「· 」),档位钮退回人选的标签;否则后端一旦带模型尾,
    // 就可能盖掉刚换的人选项——比如人选某个第三方模型时 entry["model"] 被清了,
    // 如果 cf.model 还写着旧模型(像 GPT-5.4),「·」拆分出来会把人选盖掉(2026-09-03 用户实见)。
    v.realModel = "";
    if (cf.model) {
      const pretty = hsPrettyModel(cf.model);
      const splitTail = String(cf.model).includes(" · ") ? String(cf.model).split(" · ").pop() : "";
      const slashTail = String(cf.model).includes("/") ? String(cf.model).split("/").pop() : "";
      const base = pretty || splitTail || slashTail || "";
      if (base && base !== cf.model) v.realModel = base;
    }
    v.catalog = cf.options || {};
    hsCatalogRemember(v.engine, v.catalog);
    v.sessionOpts = Object.prototype.hasOwnProperty.call(cf, "opts") ? (cf.opts || {}) : null;
    const reconciliation = (v.execution || {}).reconciliation;
    if (reconciliation && reconciliation.required) {
      const unsupportedShape = reconciliation.reason === "source_shape_unsupported";
      hsEnvironment(v, { state: "warning", stage: "recovery",
        label: unsupportedShape ? "当前 Desktron 无法识别 Claude 的新记录格式" : "Shepherd 上轮原生终态未确认",
        details: [reconciliation.detail || (unsupportedShape
          ? "请升级 Desktron 后重新打开会话；私有执行证据仍完整保留"
          : "系统无法安全验证本轮内容；私有执行证据仍完整保留")],
        action: { kind: "shepherd-reconcile", label: "隔离无法验证内容并继续",
                  attemptID: reconciliation.attempt_id } });
    } else {
      // A recovery banner is tied to an active reconciliation requirement.  Once
      // /config no longer reports one, do not let an older page-side warning
      // survive across a successful continuation or engine switch.
      if (v.envStage === "recovery") {
        v.envCurrent = null; v.envSig = ""; hsEnvironmentHide(v, true);
      }
      hsEnvironmentSnapshot(v, cf.environment, cf.environment_revision);
      if (!cf.environment) hsUpdateNotice(v, cf.update);
    }
    v.bgtasks = cf.bgtasks || { running: 0 };
  } catch {
    v.caps = {}; v.slashEng = [];
    v.commandManifest = null;
    // /config 没回来时别把引擎一起丢掉。engine 决定用哪张档位表(hsUI),丢了就落到
    // HS_UI_NONE——模式整组消失、模型点开只弹「由引擎配置决定」,两个一起坏,
    // 看着像控件坏了,其实只是这一次请求没回来。引擎名有现成的退路:
    // 上面那次 /workbench/engine,再不行用当前选中的那个。有了它,静态表就能顶上。
    v.engine = v.engine || (st && st.engine) || curEngine || "claude";
    // 档位也要有个明确的「没拿到」。下面那句判的是 `!== null`,而这里不赋值的话
    // v.sessionOpts 是 undefined——undefined !== null 为真,于是走「用会话档位」那一支,
    // 把 v.opts 设成了 undefined,人看到的是所有档位一起回默认:选好的 auto mode 变 manual、
    // Opus 5 变「默认」,像是设置被清了(2026-08-25 用户实见)。写成 null 才会落到
    // 引擎级那一份,至少是一组真档位。
    if (v.sessionOpts === undefined) v.sessionOpts = null;
    // 而且要能再来一次。原来失败之后这一页就一直残着,除非人把它关掉重开——
    // 2026-08-24 用户实见:手机经隧道时每个请求要一两秒,开机那一阵请求挤在一起,
    // 恢复出来的会话页一出生就是残的,按了右上角 × 再打开才好。
    // 只重试一次:再不行也还有上面那张静态表兜着,不必没完没了地打。
    if (!v.cfgRetried) {
      v.cfgRetried = true;
      setTimeout(() => { if (v.el && v.el.isConnected) hsEngineSync(v); }, 4000);
    }
  }
  v.engineSnap = st || v.engineSnap || null;   // 引擎级那份留一份:下面重绘时拿不到 st 也要有退路
  hsCapsPaint(v);
  hsBashPaint(v);
  hsOptsPaint(v);
  hsContextStatusSync(v);
}

// 只重画档位那一行,不取数。抽出来是因为有两个调用方:一是 hsEngineSync 取完 /config,
// 二是 SSE 收到 session.settings——别处改了档位要就地跟上,而那条路上没有 /config 可取。
function hsOptsPaint(v) {
  if (!v || !v.el) return;
  const st = v.engineSnap;
  const ui = hsUI(v);
  // 档位一律是会话级的:/config 带回的就是这一段会话自己的那份(后端已按
  // session_options(sid, engine_opts.<引擎>) 合过——没存过的会话拿到的是引擎级默认值)。
  // 只有连会话 id 都没有时才退回引擎级那一份。
  const o = (v.sessionOpts !== null && v.sessionOpts !== undefined)
    ? v.sessionOpts
    : (((st || {}).opts || {})[v.engine] || {});
  v.opts = o || {};
  if (v.shepherdMode) {
    v.shepherdEngineState = v.shepherdEngineState || {};
    v.shepherdEngineState[v.engine] = Object.assign({},
      v.shepherdEngineState[v.engine] || {}, { opts: v.opts, catalog: v.catalog || {},
        realModel: v.realModel || "" });
  }
  const mo = v.el.querySelector(".hs-mode");
  if (mo) {
    // 同一个位置可能表示执行权限或协作模式；只有 plan/default 协作档使用交叉箭头。
    const icon = mo.querySelector(".hs-composer-icon");
    const iconClass = "hs-composer-icon " + (hsModeKey(v) === "collaboration_mode" ? "is-mode" : "is-lock");
    if (icon && icon.className !== iconClass) icon.className = iconClass;
    hsUiText(mo.querySelector(".hs-mode-t"), hsModeLabel(v));
    // 不给切档位的引擎那一枚不出现:留着它只会显示一个空字样,点开又是一句
    // 「当前引擎没有返回可用模式」。
    hsUiHidden(mo, !hsModes(v).length);
  }
  const m1 = v.el.querySelector(".hs-m1"), m2 = v.el.querySelector(".hs-m2");
  const md = hsModel(v);
  // 显示以「引擎实际在跑哪个」为准,不是「Desk 让它跑哪个」。两者会分开:
  //   · 选「默认」时 opts 里没有 model,本来就要靠 realModel 才说得出话;
  //   · 用 /model 换模型时改的是引擎自己的会话状态,Desk 的 opts 一个字没动——
  //     只认 opts 的话档位钮永远停在旧值,而人明明已经换过了(2026-09-02 用户指出);
  //   · 别名跟着上游滚版本(fable → claude-fable-5-1),静态标签追不上版本号。
  // realModel 来自引擎自报(见 last_model),拿不到才退回静态标签。
  // 有一种情形要让位给人:刚在菜单里选完、这一档还没跑过。此时引擎自报的仍是上一个模型
  // (session_model 的 docstring 说的「刚换过还没跑」那一档,实测拿到的是陈旧值),直接
  // 显示它就盖掉了人刚做的选择——菜单里圆点在第三方模型上,钮上却写着 GPT-5.4
  // (2026-09-02 用户实见)。所以选的那一刻记下当时的自报值,在它变化之前以选择为准;
  // 一旦引擎报出新的,说明这一档真跑起来了,标记作废,回到「以实际为准」。
  // 选完模型、引擎还没自报新值的这段间隙，钮上以人选为准。引擎一旦真报到新模型
  // （realModel 有值且与选中时的基准不同），就回真实值；如果 realModel 被清空，
  // 说明后端明确承认「刚换过、还没跑」，此时继续显示人选，不用旧的「默认」回退把它盖掉。
  if (v.modelPick && v.realModel && v.realModel !== v.modelPickBase) {
    v.modelPick = ""; v.modelPickBase = "";
  }
  const optsModel = (o || {}).model || "";
  if (!v.modelPick && optsModel && md && !v.realModel) v.modelPick = md.label;
  if (m1) {
    const modelText = v.modelPick || hsRuntimeModelLabel(v, v.realModel) || (md ? md.label : "") || "";
    const collapsedModelText = hsCollapsedModelLabel(hsOptLabel(modelText));
    const currentText = hsEngineLabel(v, v.engine) + (collapsedModelText ? " · " + collapsedModelText : "");
    hsUiText(m1, v.shepherdMode ? currentText : collapsedModelText);
  }
  if (m2) {                       // 模型与 Effort 是两件事,分开显示也分开切
    const efforts = hsEfforts(v);
    const ef = efforts.find((x) => x.id === (o.effort || hsEffortDefault(v)));
    // ultracode 开着时直接显示它:此刻决定行为的是它,而不是那个被钉住的 effort 值。
    hsUiText(m2, o.ultracode ? "ultracode" : (ef ? ef.label : ""));
    hsUiHidden(m2, !efforts.length);
    if (m2.classList.contains("on") !== !!o.ultracode) m2.classList.toggle("on", !!o.ultracode);
  }
  const sb = v.el.querySelector(".hs-sandbox");
  if (sb) {
    const list = (v.catalog || {}).sandboxes || ui.sandboxes || [];
    const cur = o.sandbox || (list.length ? "workspace-write" : "");
    const row = list.find((x) => x.id === cur);
    hsUiText(sb.querySelector(".hs-sandbox-t"), row ? row.label : cur);
    // 按「这个引擎有没有权限预设」显隐,不按引擎名:codex 之外的引擎也可能有那三档,
    // 写死 codex 家族会让它明明能切却看不见入口。
    hsUiHidden(sb, !list.length);
  }
}

// 模式那一枚的字样:清单与键名都按引擎自报的那份取。
function hsModeLabel(v) {
  const cur = hsModeId(v);
  const m = hsModes(v).find((x) => x.id === cur);
  return m ? m.label : (cur || "");
}

async function hsSetOpt(v, patch) {
  v.opts = Object.assign({}, v.opts || {}, patch);
  // 重绘以 sessionOpts 为准。只改 opts 会被 Shepherd 的本地重绘还原，
  // 下一次改 effort/压缩时又把旧模型写回新会话默认值。
  v.sessionOpts = v.opts;
  // 模型选择先让本地精确路由成为权威；否则两次持久化请求返回前快速重开菜单，
  // hsModel 会被上一个回合的 realModel 拉回旧家族/规格。
  if (patch && Object.prototype.hasOwnProperty.call(patch, "model")) v.realModel = "";
  const eng = v.engine || "claude";
  const selectedOpts = Object.assign({}, v.opts);
  // 两处都写,不分引擎:
  //   会话级 —— 这一段会话按你在它里面选的档位跑,改一段不影响另一段;
  //   引擎级 —— 同一份写进 engine_opts.<引擎>,作为「下一次新建会话」的起点,
  //             也是 TUI 那侧 resume / 新建时的同源默认值。
  // 后端发消息取的是 session_options(sid, engine_opts.<引擎>):会话自己存过就用自己的,
  // 没存过回落到引擎级那一份——「新会话按最后一次设置起」由这条回落天然成立。
  // 原来只有 codex 家族走这条,claude 只写引擎级,于是每段 claude 会话读的都是同一个值:
  // 在一段里把 effort 调成 max,全部 claude 会话跟着变(2026-08-16 用户实见)。
  // 按引擎名写键,不写死某一个:各引擎的模型清单、登录各是一套,混进同一个键
  // 会让在一个引擎选的模型名被另一个引擎拿去启动,那是一次硬失败。
  // 两处分别写,各自成败。原来串在同一个 try 里:会话级那条一失败,引擎级那条 await
  // 根本执行不到,catch{} 又把错吞了——两级都没存上,界面却留着上面那行乐观值,
  // 人看不出这次设置没生效。手机经隧道时一个往返就要一两秒,开机拥塞时打得到
  // api() 的 8 秒上限,失败不是假想(2026-08-25)。
  if (!v.ephemeral) {
    try {
      const out = await api(`/session/${encodeURIComponent(v.sid)}/settings`, {
        method: "POST", headers: { "content-type": "application/json" },
        body: JSON.stringify({ opts: patch, engine: eng }) });
      if (out && out.opts) {
        if (!v.shepherdMode || v.engine === eng) {
          v.sessionOpts = out.opts;
          v.opts = out.opts;
        }
        if (v.shepherdMode) {
          v.shepherdEngineState = v.shepherdEngineState || {};
          const slot = v.shepherdEngineState[eng] || {};
          v.shepherdEngineState[eng] = Object.assign({}, slot, { opts: out.opts });
        }
      }
    } catch (e) {
      deskLog(`[hs] 会话档位没存上 ${v.sid}:${e.message}`);
    }
  }
  try {
    // desk_role 属于会话/工作流,不能跟着模型、模式等选项写成引擎全局默认。
    const engineDefaults = Object.assign({}, selectedOpts);
    for (const key of ["desk_role", "shepherd_mode", "shepherd_trial", "active_engine"])
      delete engineDefaults[key];
    const rememberEngine = v.shepherdMode && v.engine === eng;
    await api("/workbench/engine", { method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({ opts: { [eng]: engineDefaults },
        ...(rememberEngine ? { shepherd_engine: eng } : {}) }) });
    // 当前页面的引擎快照也要更新，切回该引擎或打开临时新会话时不读开机旧值。
    for (const snapshot of [v.engineSnap,
      typeof engineInfo !== "undefined" ? engineInfo : null]) {
      if (snapshot) {
        snapshot.opts = Object.assign({}, snapshot.opts || {}, { [eng]: engineDefaults });
        if (rememberEngine && v.engine === eng) snapshot.shepherd_engine = eng;
      }
    }
    if (typeof engineOpts !== "undefined") engineOpts[eng] = engineDefaults;
  } catch (e) {
    deskLog(`[hs] 引擎级档位没存上 ${eng}:${e.message}`);
  }
  // 普通会话回读服务端确认；Shepherd 按引擎缓存，迟到回包不能覆盖更新后的选择。
  if (v.shepherdMode) {
    if (v.engine === eng) hsOptsPaint(v);
  } else {
    hsEngineSync(v);
  }
}

// 档位菜单(模式/模型/Effort/沙箱/自动压缩)在手机上都从输入框那一行弹出,而那时键盘正开着。
// 两件事要先做:把锚点交给菜单(键盘收起后按它重摆,见 openCtxMenu),以及主动收键盘——
// 让布局的变化发生在菜单出现之前,而不是之后。桌面上 blur 没有副作用:那几枚钮本来就不带焦点。
function hsMenuAnchor(anchor) {
  if (typeof openCtxMenu === "function" && openCtxMenu.anchorNext) openCtxMenu.anchorNext(anchor);
  const a = document.activeElement;
  if (a && a !== document.body && a.blur) a.blur();
}
async function hsPickMode(v, anchor) {
  hsMenuAnchor(anchor);
  const r = anchor.getBoundingClientRect();
  const modes = hsModes(v), key = hsModeKey(v);
  const cur = hsModeId(v);
  if (!modes.length) { alert(DeskI18n.t("model.noModes")); return; }
  openCtxMenu(r.left, r.top - 8 - modes.length * 34,
    modes.map((m) => ({ label: m.label, checked: m.id === cur,
                       fn: () => hsSetOpt(v, { [key]: m.id }) })), "rp-wide");
}

function hsPickSandbox(v, anchor) {
  hsMenuAnchor(anchor);
  const list = (v.catalog || {}).sandboxes || hsUI(v).sandboxes || [];
  if (!list.length) return;
  const cur = (v.opts || {}).sandbox || "workspace-write";
  const r = anchor.getBoundingClientRect();
  openCtxMenu(r.left, r.top - 8 - list.length * 34,
    list.map((x) => ({ label: x.label, checked: x.id === cur,
                      fn: () => hsSetOpt(v, { sandbox: x.id }) })), "rp-wide");
}

function hsSwitchShepherdEngine(v, engine) {
  if (!v.shepherdMode || !engine || engine === v.engine) return;
  v.shepherdEngineState = v.shepherdEngineState || {};
  v.shepherdEngineState[v.engine] = { opts: Object.assign({}, v.opts || {}),
    catalog: v.catalog || {}, realModel: v.realModel || "" };
  const cached = v.shepherdEngineState[engine] || {};
  const defaults = Object.assign({}, ((v.engineSnap || {}).opts || {})[engine] || {});
  for (const key of ["desk_role", "shepherd_mode", "shepherd_trial", "active_engine"]) delete defaults[key];
  v.engine = engine;
  v.sessionOpts = Object.assign(defaults, cached.opts || {});
  // Pi 的共享目录是最近一次过滤结果，明确空目录也必须覆盖本视图的旧非空目录。
  const sharedCatalog = typeof hsCatalogRecall === "function" && hsCatalogRecall(engine);
  v.catalog = engine === "pi" && sharedCatalog ? sharedCatalog
    : ((cached.catalog?.models || []).length ? cached.catalog : (sharedCatalog || cached.catalog || {}));
  v.realModel = cached.realModel || "";
  v.modelPick = ""; v.modelPickBase = "";
  v.shepherdDesiredEngine = engine;
  v.shepherdPickEpoch = (v.shepherdPickEpoch || 0) + 1;
  hsOptsPaint(v);
  hsFlushShepherdEngine(v);
}

async function hsFlushShepherdEngine(v) {
  if (v.shepherdSwitching) return;
  v.shepherdSwitching = true;
  try {
    while (v.shepherdDesiredEngine) {
      const engine = v.shepherdDesiredEngine;
      const epoch = v.shepherdPickEpoch;
      v.shepherdDesiredEngine = "";
      try {
        const result = await api(`/session/${encodeURIComponent(v.sid)}/engine`, {
          method: "PUT", headers: { "content-type": "application/json" },
          body: JSON.stringify({ engine }), timeout: 30000 });
        if (epoch !== v.shepherdPickEpoch || engine !== v.engine) continue;
        v.sessionOpts = result.opts || v.sessionOpts || {};
        const slot = v.shepherdEngineState[engine] || {};
        v.shepherdEngineState[engine] = Object.assign({}, slot, { opts: v.sessionOpts });
        hsOptsPaint(v);
        try {
          await api("/workbench/engine", { method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({ shepherd_engine: engine }) });
          if (epoch !== v.shepherdPickEpoch || engine !== v.engine) continue;
          if (typeof engineInfo !== "undefined" && engineInfo) engineInfo.shepherd_engine = engine;
          if (v.engineSnap) v.engineSnap.shepherd_engine = engine;
        } catch (error) {
          deskLog(`[hs] Shepherd 新会话引擎没存上 ${engine}:${error.message}`);
          if (typeof setStatusMsg === "function") setStatusMsg("新会话引擎偏好未保存，请重新选择引擎");
        }
        // Catalog/readback is deliberately detached from the selection action.
        if (epoch === v.shepherdPickEpoch && engine === v.engine) hsEngineSync(v);
      } catch (error) {
        if (epoch === v.shepherdPickEpoch && typeof setStatusMsg === "function")
          setStatusMsg("Shepherd 引擎准备失败：" + (error?.message || "发送时会再次尝试"));
      }
    }
  } finally {
    v.shepherdSwitching = false;
    if (v.shepherdDesiredEngine) hsFlushShepherdEngine(v);
  }
}

function hsPickShepherdEngine(v, anchor) {
  hsMenuAnchor(anchor);
  const engines = hsShepherdEngines(v);
  if (!engines.length) return;
  const r = anchor.getBoundingClientRect();
  openCtxMenu(r.left - 80, r.top - 8 - engines.length * 34,
    engines.map((engine) => ({ label: engine.label,
      checked: engine.id === v.engine,
      disabled: !engine.available,
      fn: () => hsSwitchShepherdEngine(v, engine.id) })), "rp-wide");
}

// 模型目录按引擎共享并存入 localStorage：切引擎、换会话或刷新页面后先显示上一份目录。
// 只含模型名、档位与负载标签等公开元数据；登录变化由后端下一次目录覆盖。
// v2 不复用旧 Pi 通用 OpenAI 目录；后端现在只返回与 Codex 可见目录的交集。
const HS_CATALOG_STORE = "desk.modelCatalog.v2";
function hsCatalogRemember(engine, catalog) {
  if (!engine || !Array.isArray(catalog?.models)
      || (!catalog.models.length && engine !== "pi")) return;
  try {
    const all = JSON.parse(localStorage.getItem(HS_CATALOG_STORE) || "{}") || {};
    all[engine] = { at: Date.now(), catalog };
    localStorage.setItem(HS_CATALOG_STORE, JSON.stringify(all));
  } catch {}
}
function hsCatalogRecall(engine) {
  try {
    const row = (JSON.parse(localStorage.getItem(HS_CATALOG_STORE) || "{}") || {})[engine];
    return Array.isArray(row?.catalog?.models)
      && (row.catalog.models.length || engine === "pi") ? row.catalog : null;
  } catch { return null; }
}

function hsRefreshDynamicModels(v) {
  if (!v || !["pi"].includes(v.engine)) return Promise.resolve(false);
  const engine = v.engine, sid = v.sid, epoch = v.shepherdPickEpoch;
  const path = v.ephemeral ? "/config?engine=" + encodeURIComponent(engine)
    : "/config?session=" + encodeURIComponent(sid);
  const key = JSON.stringify([engine, path, epoch]);
  if (v.modelCatalogRequest?.key === key) return v.modelCatalogRequest.promise;
  // 连点共享一个请求；切引擎（包括切走再切回）或更换会话后，不应用旧目录。
  const request = { key };
  v.modelCatalogRequest = request;
  request.promise = (async () => {
    try {
      const cf = (await api(path)) || {};
      if (v.modelCatalogRequest !== request || v.engine !== engine || v.sid !== sid
          || v.shepherdPickEpoch !== epoch || (cf.engine && cf.engine !== engine)) return false;
      if (!Array.isArray(cf.options?.models)) return false;
      v.catalog = cf.options;
      hsCatalogRemember(engine, cf.options);
      return true;
    } catch { return false; }
    finally { if (v.modelCatalogRequest === request) v.modelCatalogRequest = null; }
  })();
  return request.promise;
}

async function hsPickModel(v, anchor) {
  const dynamic = v.engine === "pi";
  const sharedCatalog = dynamic && typeof hsCatalogRecall === "function" && hsCatalogRecall(v.engine);
  if (sharedCatalog) v.catalog = sharedCatalog;
  const models = hsModels(v);
  // 不等网络：缓存列表立即可选，首次空目录也立即给出加载状态。
  const menu = hsShowModelMenu(v, anchor, dynamic && !models.length ? DeskI18n.t("model.loading") : "");
  if (!dynamic) return;
  const engine = v.engine, sid = v.sid, epoch = v.shepherdPickEpoch;
  const refreshed = await hsRefreshDynamicModels(v);
  if (!menu?.isOpen() || !anchor?.isConnected || (v.el && hsHidden(v))
      || v.engine !== engine || v.sid !== sid || v.shepherdPickEpoch !== epoch) return;
  const freshModels = hsModels(v);
  if (!models.length || (refreshed
      && JSON.stringify(models.map((m) => m.id)) !== JSON.stringify(freshModels.map((m) => m.id)))) {
    // 新增或撤下模型时立即使用过滤后的清单；不让已撤下条目继续可选。
    hsShowModelMenu(v, anchor, freshModels.length ? "" : DeskI18n.t("model.loadFailed"));
    return;
  }
  // 同一清单只更新文字，保留节点、滚动和已进入的二级菜单。
  const stale = " · " + DeskI18n.t("model.stale");
  menu.setLabels(new Map((refreshed ? freshModels : models).map((m) => [m.id,
    refreshed ? hsOptLabel(m.label) : hsOptLabel(m.label.replace(/ · 状态待刷新$/, "")) + stale])));
}

function hsModelItem(v, m, cur, currentVariant, o) {
  return {
    id: m.id,
    modelRow: true,         // 只有模型与分组标题放进滚动区，运行参数固定在菜单底部
    label: hsOptLabel(m.label),   // 模型与 Load / Quota 沿用同一行、同一字号
    checked: m.id === cur,   // 勾走 openCtxMenu 的行尾位,不拼进 label——拼了会把选中项推右两个字符
    fn: () => {
      const routes = m.variants || [];
      const route = routes.find((x) => x.key === currentVariant)
        || routes.find((x) => x.key === "standard") || routes[0] || m;
      const ok = (route.efforts || []).some((x) => x.id === o.effort);
      // 记下这次选择与当时的自报值:引擎报出新模型之前,钮上显示人选的那个。
      v.modelPick = m.label;
      v.modelPickBase = v.realModel || "";
      hsSetOpt(v, Object.assign(hsModelRoutePatch(m, route),
        ok ? {} : { effort: route.defaultEffort || "" }));
    },
  };
}

// 跨服务商时把同一服务商的模型排在一起；组的先后取首次出现的位置，组内顺序不变。
function hsModelsByProvider(models) {
  const order = [];
  const groups = new Map();
  for (const m of models) {
    const key = m.provider || "";
    if (!groups.has(key)) { groups.set(key, []); order.push(key); }
    groups.get(key).push(m);
  }
  return order.flatMap((key) => groups.get(key));
}

// 模型菜单:上半是当前执行引擎的模型；Shepherd 额外提供执行引擎入口。
// 模型与执行引擎都属于当前 Conversation，不能回到左下全局书桌菜单切换。
function hsShowModelMenu(v, anchor, emptyLabel = "") {
  const ui = hsUI(v), o = v.opts || {};
  const autocompacts = hsAutocompacts(v);
  // 选中项按解析后的那一个算,不按 opts.model 的原值:没选过时它是空的,而输入框上那一枚
  // 显示的是清单里标了 default 的那个。只认原值的话,pi 这种「每个模型都有真 id、默认由
  // 引擎自报」的引擎打开菜单一个圆点都没有,与旁边显示的模型名对不上(2026-08-17 实见)。
  // claude 的清单里「默认」本身就是一条 id 为空的项,两种算法在它上面同解。
  const selected = hsModel(v);
  const cur = selected && selected.id;
  const currentRoute = hsModelRoute(v, selected);
  const currentVariant = currentRoute && currentRoute.key;
  // Pi 可同时登录多个服务商，同名模型可能来自不同服务商。跨服务商时按服务商分组，
  // 组的先后取各自第一个模型在目录里的位置，组内保持后端给出的顺序。
  const models = hsModelsByProvider(hsModels(v));
  const grouped = new Set(models.map((m) => m.provider || "")).size > 1;
  const items = [];
  const folds = v.modelProviderFolds || (v.modelProviderFolds = new Set());
  let lastProvider = null;
  for (const m of models) {
    const provider = m.provider || "";
    if (grouped && provider !== lastProvider) {
      const foldKey = JSON.stringify([v.engine, provider]);
      items.push({ header: m.providerLabel || provider || DeskI18n.t("model.other"), modelRow: true,
        modelGroup: true, collapsed: folds.has(foldKey), sep: lastProvider !== null,
        onCollapse: (collapsed) => collapsed ? folds.add(foldKey) : folds.delete(foldKey) });
      lastProvider = provider;
    }
    items.push(hsModelItem(v, m, cur, currentVariant, o));
  }
  if (!models.length && emptyLabel) {
    items.push({ label: emptyLabel, modelRow: true, disabled: emptyLabel === DeskI18n.t("model.loading"),
                 fn: () => hsPickModel(v, anchor) });
  }
  if (v.shepherdMode) {
    items.push({ label: DeskI18n.t("model.engine"), value: hsEngineLabel(v, v.engine), chev: true,
                 sep: items.length > 0, fn: () => hsPickShepherdEngine(v, anchor) });
  }
  const variants = hsVariants(v);
  if (variants.length > 1) {
    const variant = hsModelRoute(v);
    items.push({ label: DeskI18n.t("model.variant"), value: variant ? variant.label : "Standard", chev: true,
                 sep: items.length > 0, fn: () => hsPickVariant(v, anchor) });
  }
  // Effort 与模型正交,单独一档:上方一条发丝线分开,取值靠右压暗,末尾一个进二级菜单的记号。
  // 原来拼成「Effort · High ›」一整串,读起来像是又一个模型名。
  const efforts = hsEfforts(v);
  const ultra = !!o.ultracode;
  if (efforts.length) {
    const ef = efforts.find((x) => x.id === (o.effort || hsEffortDefault(v))) || efforts[0];
    // 开着 ultracode 时 effort 由它钉死在 xhigh,这一档就不该再显得可选:
    // 值直接显示 xhigh 并注明来由,点它只解释、不改。
    items.push({ label: "Effort", value: ultra ? DeskI18n.t("model.ultraEffort") : ef.label,
                 chev: !ultra, sep: items.length > 0,
                 fn: () => (ultra ? alert(DeskI18n.t("model.ultraAlert"))
                                  : hsPickEffort(v, anchor)) });
  }
  // ultracode 与 effort 不同层:它把 effort 抬到 xhigh,还常驻打开动态工作流编排
  // (引擎自己按需拉起多代理)。摆进 effort 列表会让人以为只是更高一档,
  // 而它真正改变的是成本与行为,得单独一行、说清楚代价。
  if (ui.ultracode) {
    items.push({ label: "ultracode", value: DeskI18n.t(ultra ? "model.on" : "model.off"), checked: ultra,
                 fn: () => hsSetOpt(v, { ultracode: !ultra }) });
  }
  // 压缩窗口跟在 Effort 后面:两者都是与模型正交的运行参数,归在同一组里读起来是一件事。
  if (autocompacts.length) {
    const ac = autocompacts.find((x) => x.id === (o.autocompact || "")) || autocompacts[0];
    items.push({ label: DeskI18n.t("model.autocompact"), value: hsOptLabel(ac.label), chev: true,
                 fn: () => hsPickAutocompact(v, anchor) });
  }
  // 这个引擎既不给切模型也没有 Effort(两者都由它自己那份配置定),
  // 就说清楚模型是什么、去哪儿改,不弹一个空菜单。
  if (!items.length) {
    alert(DeskI18n.t("model.ownConfig", { model: v.realModel || DeskI18n.t("model.default") })); return;
  }
  // 锚点状态只能在实际弹出前绑定，不能留给异步回调或另一个菜单消费。
  hsMenuAnchor(anchor);
  const r = anchor.getBoundingClientRect();
  return openCtxMenu(r.left - 80, r.top - 8 - items.length * 34, items, "rp-wide rp-model");
}

function hsPickVariant(v, anchor) {
  hsMenuAnchor(anchor);
  const variants = hsVariants(v);
  if (variants.length < 2) return;
  const current = hsModelRoute(v);
  const r = anchor.getBoundingClientRect();
  openCtxMenu(r.left - 80, r.top - 8 - variants.length * 34,
    variants.map((route) => ({ label: route.label, checked: current && route.id === current.id,
      fn: () => {
        const effort = (v.opts || {}).effort;
        const ok = (route.efforts || []).some((x) => x.id === effort);
        hsSetOpt(v, Object.assign(hsModelRoutePatch(hsModel(v), route),
          ok ? {} : { effort: route.defaultEffort || "" }));
      } })), "rp-wide");
}

function hsPickEffort(v, anchor) {
  hsMenuAnchor(anchor);
  const efforts = hsEfforts(v);
  if (!efforts.length) return;
  const cur = (v.opts || {}).effort || hsEffortDefault(v);
  const r = anchor.getBoundingClientRect();
  openCtxMenu(r.left - 80, r.top - 8 - efforts.length * 34,
    efforts.map((e) => ({ label: e.label, checked: e.id === cur,
                             fn: () => hsSetOpt(v, { effort: e.id }) })), "rp-wide");
}

function hsPickAutocompact(v, anchor) {
  hsMenuAnchor(anchor);
  const autocompacts = hsAutocompacts(v);
  if (!autocompacts.length) return;
  const saved = (v.opts || {}).autocompact || "";
  // A tier saved on a larger model stays dormant after switching to a smaller
  // route. Show native auto as the effective value without erasing the saved tier;
  // switching back to a compatible model can resume it.
  const cur = autocompacts.some((a) => a.id === saved) ? saved : "";
  const r = anchor.getBoundingClientRect();
  openCtxMenu(r.left - 80, r.top - 8 - autocompacts.length * 34,
    autocompacts.map((a) => ({ label: hsOptLabel(a.label), checked: a.id === cur,
                              fn: () => hsSetOpt(v, { autocompact: a.id }) })), "rp-wide");
}
