// 三个管理入口的 WebKit 交互回归；接口使用固定数据，不连接 Desk 服务。
// 需安装 Playwright 和 WebKit；可用 DESK_PLAYWRIGHT_MODULE / DESK_WEBKIT_PATH 指向已有安装。
// node scripts/dev/check-resource-dialog.cjs；截图默认写入系统临时目录。
const {webkit}=require(process.env.DESK_PLAYWRIGHT_MODULE || 'playwright');
const fs=require('fs'), assert=require('assert');
const path = require('path');
const root = path.resolve(__dirname, '../../frontend') + '/';
const shots = process.env.DESK_DIALOG_SCREENSHOTS || require('os').tmpdir();
const read=f=>fs.readFileSync(root+f,'utf8');
(async()=>{
 const browser=await webkit.launch({headless:true, ...(process.env.DESK_WEBKIT_PATH ? {executablePath:process.env.DESK_WEBKIT_PATH} : {})});
 const page=await browser.newPage({viewport:{width:1360,height:900}});
 const errors=[];page.on('pageerror',e=>{errors.push(e.message);console.error('pageerror:',e.message)});
 await page.route('http://desk.test/**',r=>r.fulfill({contentType:'text/html; charset=utf-8',body:'<html data-theme="cyan"><head><meta charset="utf-8"></head><body><button id="entry">安排</button><div id="asset-body">原会话内容</div><div id="settings-modal" hidden></div><span id="arrangement-cnt"></span></body></html>'}));
 await page.goto('http://desk.test/');
 await page.addStyleTag({content:read('style.css')});
 await page.addScriptTag({content:`
 const $=s=>document.querySelector(s), escapeHtml=s=>String(s??'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
 let nav={type:'domain',id:'demo'}, domains=[],desktopApps=[];const DESKTOP='desktop';
 const ICONS=new Proxy({}, {get:()=>'<svg viewBox="0 0 24 24"></svg>'});
 const api=async path=> {if(path==='/workbench/arrangements')return {items:[...Array.from({length:30},(_,i)=>({id:'schedule-'+i,name:'安排 '+i,enabled:false,backend:'desktop',trigger:{kind:'manual'}}))],backend:'desktop'}; if(path.includes('/assets')) return {skills_installed:[]}; return {items:[],rows:[],peers:[],backend:'desktop',status:[]};};
 const placePop=(pop,r)=>{pop.style.left=r.left+'px';pop.style.top=r.bottom+'px'};
 `});
 await page.addScriptTag({content:read('app-rail-nav.js').split('(function initSidebar()')[0]});
 const top=read('app-rail-topnav.js');
 await page.addScriptTag({content:top.slice(top.indexOf('const WS_TAB_ICONS'))});
 for(const file of ['app-rail-agents.js','app-rail-bots.js','app-panel-arrangements.js','app-assets-apps.js','app-assets-kb.js','app-assets-workshop.js','app-assets-create.js']) await page.addScriptTag({content:read(file)});
 await page.addScriptTag({content:read('app-panel-sidebar.js').split('// 菜单里的')[0]});
 await page.evaluate(()=>{document.querySelector('#entry').onclick=()=>openArrangementsPage()});
 await page.focus('#entry'); await page.keyboard.press('Enter');
 await page.waitForSelector('.resource-view-head');
 assert.equal(await page.locator('#asset-body').innerText(),'原会话内容');
 assert.deepEqual(await page.evaluate(()=>nav),{type:'domain',id:'demo'});
 assert.equal(await page.locator('.resource-panel').evaluate(el=>el.classList.contains('sm-panel')),true);
 assert.equal(await page.locator('.ws-tabs, .ap-chips').count(),0);
 await page.locator('[data-category="system"]').click();
 assert.equal(await page.locator('.wf-h1').innerText(),'系统安排');
 assert.equal(await page.locator('.arr-row').count(),0);
 assert.equal(await page.locator('[data-new]').count(),0);
 assert.deepEqual(await page.locator('.resource-nav-items .ai-title').allTextContents(),['本地','系统安排']);
 await page.fill('#resource-search','安排 1');
 await page.locator('[data-category="system"]').click();
 assert.equal(await page.inputValue('#resource-search'),'');
 await page.locator('[data-category="local"]').click();
 assert.equal(await page.locator('.arr-row').count(),30);
 await page.click('[data-new]');
 await page.waitForSelector('.resume-pop');
 assert(await page.evaluate(()=>getComputedStyle(document.querySelector('.resume-pop')).zIndex>getComputedStyle(document.querySelector('#resource-modal')).zIndex));
 await page.locator('.resume-pop .rp-row').nth(1).click();
 await page.waitForSelector('.arr-modal');
 await page.keyboard.press('Escape');
 assert.equal(await page.locator('.arr-modal').count(),0);
 assert.equal(await page.locator('#resource-modal').isVisible(),true);
 await page.screenshot({animations:'disabled',path:path.join(shots,'desk-resource-arrangements.png')});
 await page.evaluate(()=>{_resourceScroll('arrangements').scrollTop=400});
 await page.keyboard.press('Escape');
 await page.evaluate(()=>openArrangementsPage());
 await page.waitForTimeout(50);
 assert.equal(await page.evaluate(()=>_resourceScroll('arrangements').scrollTop),400);
 await page.evaluate(()=>{document.querySelector('.resource-close').focus()});
 await page.keyboard.press('Shift+Tab');
 assert.equal(await page.evaluate(()=>document.querySelector('#resource-modal').contains(document.activeElement)),true);
 
 await page.keyboard.press('Escape');
 assert.equal(await page.evaluate(()=>document.activeElement.id),'entry');
 await page.evaluate(()=>openStore('app'));
 await page.keyboard.press('Meta+k');
 assert.equal(await page.evaluate(()=>document.activeElement.id),'resource-search');
 await page.fill('#resource-search','保留筛选');
 await page.click('.resource-nav-items [data-category="local"]');
 await page.keyboard.press('Escape');
 await page.evaluate(()=>openStore('app'));
 assert.equal(await page.inputValue('#resource-search'),'保留筛选');
 assert.equal(await page.locator('.resource-nav-items .active').getAttribute('data-category'),'local');
 await page.evaluate(()=>{desktopApps=[{label:'保留筛选 应用',id:'demo'}];refreshResourceApps();});
 assert.equal(await page.inputValue('#resource-search'),'保留筛选');
 assert.equal(await page.locator('.ap-row').count(),1);
 await page.screenshot({animations:'disabled',path:path.join(shots,'desk-resource-apps.png')});
 await page.evaluate(()=>{
   window._appRecentPush=()=>{}; window._dockAppKey=a=>a.id;
   window.openBrowserApp=()=>{window.launched=true};
   _openDomainApp({id:'browser',page:'browser'},'desktop');
 });
 assert.equal(await page.locator('#resource-modal').isVisible(),false);
 assert.equal(await page.evaluate(()=>window.launched),true);
 
 await page.evaluate(()=>openBotsPage());
 await page.waitForSelector('.resource-nav-items');
 await page.fill('#resource-search','保留 Bot');
 await page.locator('.resource-nav-items [data-category="assets"]').click();
 await page.waitForSelector('.skills-page');
 assert.equal(await page.locator('.resource-nav-items .active').getAttribute('data-category'),'assets');
 await page.screenshot({animations:'disabled',path:path.join(shots,'desk-resource-skills.png')});
 await page.locator('.resource-nav-items [data-category="kb"]').click();
 await page.waitForSelector('.kb-page');
 assert.equal(await page.locator('.resource-nav-items .active').getAttribute('data-category'),'kb');
 await page.screenshot({animations:'disabled',path:path.join(shots,'desk-resource-kb.png')});
 await page.locator('.resource-nav-items [data-category="bots"]').click();
 assert.equal(await page.inputValue('#resource-search'),'保留 Bot');
 assert.equal(await page.locator('#asset-panel-host').count(),0);
 await page.screenshot({animations:'disabled',path:path.join(shots,'desk-resource-bots.png')});
 // 技能首次请求尚未返回就切走：重开必须补画，而不能缓存一张“加载中”。
 await page.evaluate(async()=>{
   const fetchAssets = _fetchAssets;
   _assetsCache = null; _resourcePages.delete('assets');
   let release;
   _fetchAssets = () => new Promise(resolve => { release = resolve; });
   const loading = openAssetListPage('assets');
   openBotsPage();
   release({skills_installed:[]});
   await loading;
   _fetchAssets = fetchAssets;
   await openAssetListPage('assets');
 });
 await page.waitForSelector('.skills-page');
 await page.setViewportSize({width:390,height:844});
 for(const name of ['arrangements','apps','bots','skills','kb']){
  await page.evaluate(name=>{if(name==='arrangements')return openArrangementsPage();else if(name==='apps')return openStore('app');else return openWorkshopPage(name)},name);
  const geom=await page.locator('.resource-panel').boundingBox();
  assert(geom.x>=0 && geom.width<=390 && geom.height<=844);
  const heading=await page.locator('.wf-h1').boundingBox();
  assert(heading.x>=geom.x && heading.x+heading.width<=geom.x+geom.width);
  assert.equal(await page.evaluate(()=>document.querySelector('.resource-scroll').scrollWidth<=390),true);
  await page.screenshot({animations:'disabled',path:path.join(shots,'desk-resource-'+name+'-mobile.png')});
 }
 assert.deepEqual(errors,[]);
 console.log('Browser checks passed: three dialogs, nested form/menu, Escape/focus, filters, catalog refresh, tabs, desktop/mobile geometry.');
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
