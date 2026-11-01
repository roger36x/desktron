"""更新按钮：检查、下载、确认安装分别由独立点击触发。"""
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[2]


def test_update_click_stages_and_retries():
    script = r'''
const fs = require('fs'), vm = require('vm'), assert = require('assert/strict');
const source = fs.readFileSync('frontend/app-settings-sections.js', 'utf8');
const i18nSource = ['app-i18n.js', 'app-i18n-zh.js', 'app-i18n-en.js']
  .map(name => fs.readFileSync('frontend/' + name, 'utf8')).join('\n');
const classes = {toggle() {}};
const label = {classList: classes, textContent: '', attrs: {},
  getAttribute(k) {return this.attrs[k] || '';}, setAttribute(k, v) {this.attrs[k] = v;}};
const memory = new Map();
const button = {classList: classes, setAttribute(k,v) {this[k] = v;}};
let state, posts, alerts, confirmations, consent = true, failPost = false, failStatus = false, gate;
const context = vm.createContext({
  document: {getElementById(id) {return id === 'smp-update' ? button : label;}, documentElement: {},
    querySelectorAll(selector) {return selector === '[data-desk-i18n]' ? [label] : [];}},
  localStorage: {getItem: k => memory.get(k), setItem: (k, v) => memory.set(k, v)},
  setTimeout() {return 1;}, clearTimeout() {},
  alert(s) {alerts.push(s);}, confirm(s) {confirmations.push(s); return consent;},
  async api(path, opts) {
    if (path === '/workbench/terminals') return [{busy: true}];
    if (!opts) {if (failStatus) throw Error('断网'); if (gate) await gate; return {...state};}
    posts.push([path, opts]);
    if (failPost) throw Error('请求失败');
    if (path.endsWith('/check')) state = {state: 'available', hasUpdate: true};
    if (path.endsWith('/download')) state = {state: 'downloading', checking: true, progress: 45};
    if (path.endsWith('/apply')) {state = {state: 'applying'}; return {ok: true};}
    return {...state};
  }
});
vm.runInContext(i18nSource, context);
vm.runInContext(source.slice(source.indexOf('let _updateStatusTimer = 0;')), context);
function reset(s) {state = s; posts = []; alerts = []; confirmations = []; consent = true; failPost = false; failStatus = false;}
async function click() {await context.openUpdateTUI();}
(async () => {
  reset({state:'idle', unknown:true});
  await click();
  assert.deepEqual(posts.map(x=>x[0]), ['/workbench/update/check']);
  assert.equal(label.textContent, '下载更新');
  await click();
  assert.equal(posts[1][0], '/workbench/update/download');
  assert.equal(label.textContent, '下载中 45%');
  assert.equal(button.disabled, true);
  await click(); assert.equal(posts.length, 2);
  state = {state:'staged', staged_rev:'abc', hasUpdate:true};
  await context.refreshUpdateStatus();
  assert.equal(label.textContent, '安装并重启');
  assert.equal(posts.length, 2); // 暂存完成不会触发安装
  consent = false; await click(); assert.equal(posts.length, 2);
  consent = true; await click();
  assert.equal(posts[2][0], '/workbench/update/apply');
  assert.deepEqual(JSON.parse(posts[2][1].body), {confirm:'abc'});
  assert.match(confirmations[0], /重启会中断所有会话/);
  assert.equal(label.textContent, '更新中');

  reset({state:'dev', dev:true, hasUpdate:true});
  await click(); assert.equal(posts.length, 0);
  assert.match(label.textContent, /源码同步/); assert.match(alerts[0], /不会下载或安装发行包/);

  reset({state:'error', hasUpdate:true, error:'校验失败'});
  await context.refreshUpdateStatus(); assert.equal(label.textContent, '重试下载');
  assert.match(button.title, /校验失败/);
  await click(); assert.equal(posts[0][0], '/workbench/update/download');

  reset({state:'applying', acceptance_unknown:true, hasUpdate:true, error:'接管状态未知'});
  await context.refreshUpdateStatus(); assert.equal(label.textContent, '更新状态待确认');
  assert.equal(button.disabled, true); await click(); assert.equal(posts.length, 0);

  reset({state:'available', hasUpdate:true}); failPost = true;
  await click(); assert.match(alerts[0], /下载更新失败/); assert.equal(button.disabled, false);
  failPost = false; await click(); assert.equal(posts.length, 2);

  reset({state:'staged', staged_rev:'abc'}); failPost = true;
  await click(); assert.match(alerts[0], /启动更新失败/);
  assert.equal(label.textContent, '安装并重启'); assert.equal(button.disabled, false);

  reset({state:'idle'}); failStatus = true;
  await click(); assert.equal(posts.length, 0); assert.match(label.textContent, /连接中断/);
  assert.equal(button.disabled, false);
  failStatus = false; await click(); assert.equal(posts[0][0], '/workbench/update/check');

  reset({state:'available', hasUpdate:true});
  let release; gate = new Promise(resolve => release = resolve);
  const first = click(); await click(); release(); gate = null; await first;
  assert.equal(posts.length, 1); // 同时双击只请求一次

  // 英文界面：状态文字随语言切换，不必重新请求。
  context.DeskI18n.setLocale('en');
  assert.equal(label.textContent, 'Downloading 45%');
  reset({state:'dev', dev:true}); await context.refreshUpdateStatus();
  assert.equal(label.textContent, 'Development build · Source sync');
})().catch(e => {console.error(e); process.exit(1);});
'''
    result = subprocess.run(['node', '-e', script], cwd=ROOT, text=True,
                            capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
