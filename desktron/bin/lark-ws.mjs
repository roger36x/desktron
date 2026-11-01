#!/usr/bin/env node
/**
 * 飞书长连接的收侧(docs/agent-hosting-design.md「架构：Node 收，Python 发」)。
 *
 * 这一层只做一件事：把长连接收到的消息事件 POST 给本机的 Desk 后端。不解析业务、
 * 不判断该不该回、更不发消息——出站全在 Python 那侧，那里才有红线闸。这么划分不是
 * 图省事：Node 这层碰不到出站，也就不存在绕过闸的路径。
 *
 * 为什么收侧要用 Node：飞书长连接是私有协议（protobuf frame + 自有的握手与心跳），
 * 官方只有 Node/Java/Go/Python SDK，自己照着抓包实现不值当。Python SDK 要 pip，
 * 而 Desk 后端一贯零第三方依赖；Node 本就是 Desk 的运行时，装个 npm 包不动
 * Python 那侧的零依赖。
 *
 * 用法：node lark-ws.mjs --app-id <id> --port <desk端口> [--domain feishu|larksuite]
 * app_secret 从环境变量 LARK_APP_SECRET 取，不走命令行——argv 在 ps 里人人可见。
 */
import { createRequire } from 'node:module';
import { writeFileSync } from 'node:fs';

/**
 * SDK 不能用裸 import。ESM 的模块解析是从**脚本文件所在目录**往上找 node_modules，
 * 不看 cwd——而这个脚本住在 payload 里（desktron/bin/），依赖装在
 * ~/.local/share/desktron/larkbot/ 下，两处不相邻，裸 import 一定 ERR_MODULE_NOT_FOUND。
 * 设 cwd 不管用（ESM 不看），NODE_PATH 也不管用（那是 CJS 的机制）。
 *
 * createRequire 指向依赖目录里的 package.json，解析就从那儿起步。SDK 本身是 CJS，
 * require 它拿到的就是完整的导出。
 */
let lastError = '';
/**
 * SDK 的 logger 既不走 console.error 也不走 console.log（两个都劫持过，一条也没抓到），
 * 所以直接接在 stdout/stderr 的 write 上——那是所有输出的必经之路，不管上层用的什么。
 * 抓的是级别标记：`[error]: [ '[ws]', 'invalid appId: …' ]`。
 *
 * **必须排在 SDK 加载之前。** 放在 require 之后一条也抓不到——SDK 的 logger 在模块
 * 初始化那一刻就把写出口绑定好了，之后再换 stdout.write 换的是另一个引用。
 */
for (const st of [process.stdout, process.stderr]) {
  const orig = st.write.bind(st);
  st.write = (chunk, ...rest) => {
    try {
      const t = typeof chunk === 'string' ? chunk : String(chunk);
      if (/\[(error|fatal)\]/.test(t)) lastError = t.trim().slice(0, 300);
    } catch { /* 劫持出错不能带塌真正的输出 */ }
    return orig(chunk, ...rest);
  };
}

const SDK_HOME = process.env.DESKTRON_LARKBOT_HOME
  || `${process.env.HOME}/.local/share/desktron/larkbot`;
let Lark;
try {
  Lark = createRequire(`${SDK_HOME}/package.json`)('@larksuiteoapi/node-sdk');
} catch (e) {
  console.error(`飞书 SDK 未安装或装在别处（找的是 ${SDK_HOME}/node_modules）。`
    + `在 Desk 的设置页 → 飞书 bot 里点「安装」，或在该目录下跑 npm install。`);
  console.error(String(e).slice(0, 200));
  process.exit(3);
}

const argv = process.argv.slice(2);
const arg = (k, d = '') => {
  const i = argv.indexOf('--' + k);
  return i >= 0 && argv[i + 1] ? argv[i + 1] : d;
};

const APP_ID = arg('app-id');
const APP_SECRET = process.env.LARK_APP_SECRET || '';
const PORT = arg('port', '7765');
const DOMAIN = arg('domain', 'feishu');
const TOKEN = process.env.DESKTRON_REMOTE_TOKEN || '';

if (!APP_ID || !APP_SECRET) {
  console.error('缺 --app-id 或环境变量 LARK_APP_SECRET');
  process.exit(2);
}

// 一律给 URL 字符串，不用 Lark.Domain 枚举：SDK 的 formatDomain 对未知字符串走
// `default: return domain`，两者等价。统一成字符串，这份表就能与 Python 侧
// larkapi.DOMAINS 一字不差。
const DOMAINS = {
  feishu: 'https://open.feishu.cn',
  larksuite: 'https://open.larksuite.com',
};

const ENDPOINT = `http://127.0.0.1:${PORT}/workbench/lark-bot/event`;

const log = (...a) => console.log(new Date().toISOString().slice(0, 19), ...a);

/**
 * 转发给 Desk 后端。失败只记日志不重试：飞书那边有自己的重推机制，而 Python 那侧
 * 按 event_id 去重（handler._dedupe）。我们再叠一层重试，只会让重复投递变多。
 */
async function forward(payload) {
  const headers = { 'Content-Type': 'application/json' };
  if (TOKEN) headers['Authorization'] = 'Bearer ' + TOKEN;   // 后端绑了 0.0.0.0 时要验票
  try {
    const r = await fetch(ENDPOINT, {
      method: 'POST',
      headers,
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(600_000),   // 一轮引擎可能跑几分钟，别在这里超时
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok || j.ok === false) log('转发未成功', r.status, JSON.stringify(j).slice(0, 200));
    else if (j.skipped) log('已忽略:', j.skipped);
    else log('已处理:', j.agent || '', j.kind || '', j.level || '');
  } catch (e) {
    log('转发失败:', String(e).slice(0, 200));
  }
}

const wsClient = new Lark.WSClient({
  appId: APP_ID,
  appSecret: APP_SECRET,
  domain: DOMAINS[DOMAIN] || DOMAINS.feishu,
  loggerLevel: Lark.LoggerLevel.info,
});

const dispatcher = new Lark.EventDispatcher({}).register({
  'im.message.receive_v1': async (data) => {
    // data 已是 event 体。补上 app_id：一台机器上可能同时跑几个 bot，
    // Python 那侧按它挑 agent（route.pick 的第一步）。
    const payload = { ...data, app_id: APP_ID };
    // 不 await：飞书要求事件处理尽快返回，否则会判超时并重推。真正的处理在 Python 侧，
    // 那边跑一轮引擎可能要几分钟，等它等不起。
    forward(payload);
    return { ok: true };
  },
});

log(`长连接启动 app=${APP_ID} domain=${DOMAIN} → ${ENDPOINT}`);
wsClient.start({ eventDispatcher: dispatcher });

// 把「进程在跑」与「真的连上了」分开写进状态文件。这两件事差别很大：凭证过期、
// 权限没开、网络被中间设备拦掉时，进程照样活着（SDK 会一直重连），但一条消息也收不到。
// 只看 pid 的话，面板会显示「在线」而人在群里 @ 它没有任何反应，无从下手。
const STATE = `${SDK_HOME}/ws-${APP_ID}.state.json`;
const putState = (o) => {
  try {
    writeFileSync(STATE, JSON.stringify({ at: Math.floor(Date.now() / 1000), app_id: APP_ID, ...o }));
  } catch { /* 状态文件写不了不该影响收消息 */ }
};

/**
 * 记下 SDK 打出的最后一条错误。SDK 的日志走 console，不抛异常也不给回调，凭证错、
 * 权限不足这些只体现在那几行文字里；进程随后可能直接退出（实测 `invalid appId` 就是
 * 起来三秒后退掉，不重连）。不留下这条，面板上只剩一个「未启动」，人第二次点启动
 * 还是同样的结果，看不出原因在凭证上。
 */
putState({ status: 'starting' });
let lastState = 'starting';
setInterval(() => {
  let st = 'unknown';
  try {
    // SDK 的 getConnectionStatus 返回 {state, lastConnectTime, ...} 对象(types/index.d.ts:
    // WSConnectionStatus),不是字符串。拿对象去 String() 会写出 "[object Object]",
    // 面板据此把在线的 bot 判成「重连中」(2026-08-27 实测)。取 .state;老版 SDK 若
    // 直接给字符串也照收。
    const raw = wsClient.getConnectionStatus?.();
    st = String((raw && typeof raw === 'object' ? raw.state : raw) ?? 'unknown');
  } catch { st = 'error'; }
  if (st !== lastState) { log('连接状态:', st); lastState = st; }
  putState({ status: st, error: lastError });
}, 5000).unref?.();

// 退出时把原因留下。SDK 用 process.exit() 强退时 'exit' 仍会触发，同步写文件来得及。
// 退出码本身说明不了问题：SDK 拿到 `invalid appId` 之后是以 0 退出的。所以判定看
// lastError 而不是 code——有过 error 就是 failed，让面板能把「配错了」与「被人停掉了」分开。
process.on('exit', (code) => putState({
  status: lastError ? 'failed' : 'exited', code, error: lastError,
}));

// SDK 自己会重连。这里只保证进程不因为一个未捕获的 rejection 就退出——
// 退了就再也收不到消息，而且外面不一定有人守着重启。
process.on('unhandledRejection', (e) => log('未处理的 rejection:', String(e).slice(0, 200)));
process.on('SIGTERM', () => { log('收到 SIGTERM，退出'); process.exit(0); });
