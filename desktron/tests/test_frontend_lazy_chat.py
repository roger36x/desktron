"""固定聊天入口应先开前端空页，首条真实消息才创建会话。"""
import os
import shutil
import subprocess
import unittest

import _srcmap


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FRONTEND = os.path.join(ROOT, "frontend")
CHAT = _srcmap.chat_src()
PANEL = _srcmap.panel_src()
RAIL = _srcmap.rail_src()
INIT = open(os.path.join(FRONTEND, "app-init.js"), encoding="utf-8").read()
PAGES = open(os.path.join(FRONTEND, "app-pages.js"), encoding="utf-8").read()


class LazyChat(unittest.TestCase):
    def test_固定聊天入口与聊天页头都开临时页(self):
        self.assertIn('fxNew.onclick = () => openDraftChat();', RAIL)
        self.assertIn('addBtn.onclick = () => openDraftChat();', INIT)

    def test_工作流菜单新建仍立即创建(self):
        self.assertIn('{ ico: WF_MENU_ICONS.conversation, label: DeskI18n.t("wf.newConversation"), fn: () => newTerminal(curEngine, p.id) }', PANEL)

    def test_临时页不使用真实会话地址也不恢复草稿(self):
        self.assertIn('const HS_DRAFT_PAGE = "chat-draft";', CHAT)
        self.assertIn('el._hsBuildOpts = { ephemeral: true, domain: DESKTOP };', CHAT)
        self.assertRegex(CHAT, r'if \(!v\.ephemeral\) \{\s+hsDraftLoad\(v\);')
        self.assertIn('if (v.ephemeral) return;', CHAT)
        self.assertIn('if (v.ephemeral) continue;', CHAT)

    def test_首条发送先创建再投递(self):
        send = CHAT[CHAT.index('async function hsSend(v) {'):CHAT.index('async function hsDispatch(v, text, atts) {')]
        make = 'if (v.ephemeral && !(await hsMaterializeDraft(v))) return;'
        self.assertIn(make, send)
        self.assertLess(send.index(make), send.index('v.inp.textContent = "";', send.index(make)))

    def test_创建后把同一视图转成真实会话(self):
        start = CHAT.index('async function hsMaterializeDraft(v) {')
        end = CHAT.index('function hsDiscardDraft(p) {', start)
        body = CHAT[start:end]
        self.assertIn('s = await api("/session"', body)
        self.assertIn('v.sid = sid;', body)
        self.assertIn('v.ephemeral = false;', body)
        self.assertIn('p.url = HS_PAGE(sid);', body)
        self.assertIn('persistPages();', body)
        self.assertIn('refreshTerms({ maxAge: 0 })', body)
        self.assertIn('renderLooseSessions(host)', body)

    @unittest.skipUnless(shutil.which("node"), "需要 node")
    def test_临时聊天按当前模式创建(self):
        start = CHAT.index('async function hsMaterializeDraft(v) {')
        end = CHAT.index('function hsDiscardDraft(p) {', start)
        body = CHAT[start:end]
        helper = CHAT[CHAT.index('function hsNewSessionEngine('):CHAT.index('async function hsEngineSync(')]
        script = r'''
const assert=require('node:assert/strict');
let curEngine='pi',shepherdMode=false,shepherdTrial=false,DESKTOP='_desktop';
let engineInfo={shepherd_engine:'claude'};
let calls=[],pages=[],pActive=null,activeTermId='',lastChatPage=null;
function HS_PAGE(sid){return 'chat:'+sid;}
async function api(path,opts){calls.push([path,opts]);return {id:'adp_'+calls.length};}
function hsErrorNotice(){}
const _hsViews=new Map();
const document={querySelectorAll:()=>[],querySelector:()=>null};
const localStorage={setItem(){}};
function setOverlayTitle(){}
function termRowMatchesId(){return false;}
function persistPages(){}
async function hsEngineSync(){}
function refreshTerms(){return Promise.resolve([]);}
function renderLooseSessions(){}
FN
async function create(mode,trial){
 shepherdMode=mode;shepherdTrial=trial;
 const page={el:{},url:'chat-draft'};pages=[page];pActive=page;
 const v={sid:'draft',el:page.el,ephemeral:true,draftDomain:'_desktop'};
 _hsViews.clear();_hsViews.set('draft',v);
 assert.equal(await hsMaterializeDraft(v),true);
 return JSON.parse(calls.at(-1)[1].body);
}
(async()=>{
 assert.deepEqual(await create(false,false),{domain:'_desktop',engine:'pi'});
 assert.deepEqual(await create(true,false),
   {domain:'_desktop',engine:'claude',shepherd_mode:true});
 assert.deepEqual(await create(true,true),
   {domain:'_desktop',engine:'claude',shepherd_mode:true,shepherd_trial:true});
})().catch(e=>{console.error(e);process.exit(1)});
'''.replace('FN', helper + body)
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_临时页关闭时清理视图监听(self):
        self.assertIn('if (typeof hsDiscardDraft === "function") hsDiscardDraft(p);', PAGES)
        self.assertIn('_hsViews.delete(HS_DRAFT_SID);', CHAT)


if __name__ == "__main__":
    unittest.main()
