"""会话完成未读铃铛：主侧栏打开清除，下一回合可重新提示。"""
import unittest
import pytest
import test_frontend_single_sidebar as sidebar


class BellAssetContract(unittest.TestCase):
    def test_unread_marker_uses_packaged_bell(self):
        asset = sidebar.FRONT / 'icons' / 'bell.png'
        data = asset.read_bytes()
        self.assertEqual(data[:8], b'\x89PNG\r\n\x1a\n')
        self.assertEqual((int.from_bytes(data[16:20], 'big'), int.from_bytes(data[20:24], 'big')), (512, 512))
        css = (sidebar.FRONT / 'style.css').read_text(encoding='utf-8')
        rule = css.split('.term-item .ti-completed-unread {', 1)[1].split('}', 1)[0]
        self.assertIn('width: 10px', rule)
        self.assertIn('height: 10px', rule)
        self.assertIn('flex: 0 0 10px', rule)
        self.assertIn('transform: translateY(.5px)', rule)
        self.assertIn('background: var(--ink-82)', rule)
        self.assertIn('url("/icons/bell.png")', rule)
        self.assertNotIn('border-radius', rule)


@unittest.skipUnless(sidebar.sync_playwright, '需要 Playwright 和 WebKit')
@pytest.mark.desktop
class SessionCompletion(unittest.TestCase):
    def setUp(self):
        sidebar.SingleSidebarWebKit.setUp(self)
        self.page.add_script_tag(content='const _termReadPending = new Map();\n' + '\n'.join(
            sidebar.function(sidebar.TERMS, name) for name in (
                'termCompletionKeys', 'termCompletionRead', 'termCompletionUnread', 'termState', 'termStatTip',
                'paintTermCompletion', 'clearTermCompletion', 'openTermCtxMenu')))
        self.page.add_script_tag(content='''
          const RP_MOVE_ICO='', RP_TRASH_ICO='';
          function termInfoNode() { return document.createElement('div'); }
          function openCtxMenu(x,y,items) { window.menuItems=items; }
          const sample = _termsList.find(t=>t.id==='codex-0');
          sample.unread=true; sample.busy=false; sample.alive=false; sample.completion_generation=100;
          document.getElementById('panel-list').appendChild(termRow(sample,'wf'));
          function repaint() {
            document.querySelectorAll('.term-item[data-sid="codex-0"]').forEach(row=>bindTermRowIdentity(row,sample));
          }
        ''')

    def tearDown(self):
        sidebar.SingleSidebarWebKit.tearDown(self)

    def test_bell_and_click_clear_sidebar(self):
        marker = self.page.locator('.ti-completed-unread')
        self.assertEqual(marker.count(), 1)
        style = marker.evaluate("el => { const s=getComputedStyle(el); return {width:s.width,height:s.height,mask:s.webkitMaskImage,transform:s.transform}; }")
        self.assertEqual((style['width'], style['height']), ('10px', '10px'))
        self.assertEqual(style['transform'], 'matrix(1, 0, 0, 1, 0, 0.5)')
        self.assertIn('/icons/bell.png', style['mask'])
        self.page.locator('#panel-list .term-item[data-sid="codex-0"]').click()
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)
        self.page.evaluate('repaint()')  # 尚未回写的旧 unread 响应
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)
        self.assertEqual(self.page.evaluate('clicked'), {'id':'codex-0','engine':'codex'})
        self.assertEqual(self.errors, [])

    def test_read_ack_and_next_completion(self):
        self.page.locator('#panel-list .term-item').click()
        self.page.evaluate('sample.unread=false; repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)
        self.page.evaluate('sample.busy=true; sample.completion_generation=101; repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)
        self.page.evaluate('sample.busy=false; sample.unread=true; repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)

    def test_old_busy_and_idle_snapshots_never_relight_read_turn(self):
        self.page.locator('#panel-list .term-item').click()
        for busy, unread in ((True, False), (False, False), (False, True)):
            self.page.evaluate('(s) => { Object.assign(sample,s); repaint(); }',
                               dict(busy=busy, unread=unread))
            self.assertEqual(self.page.locator('.ti-completed-unread,.term-item.unread,.st-working,.st-unread').count(), 0)
        self.page.evaluate('sample.completion_generation=101; repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)
        self.assertEqual(self.page.locator('.st-unread').count(), 1)

    def test_preparing_next_turn_does_not_relight_previous_completion(self):
        self.page.locator('#panel-list .term-item').click()
        self.page.evaluate('sample.completion_generation=101; sample.completed_generation=100; repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)
        self.page.evaluate('sample.busy=true; repaint()')
        self.assertEqual(self.page.locator('.st-working').count(), 1)
        self.page.evaluate('sample.busy=false; sample.completed_generation=101; repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)

    def install_view(self, hidden=False):
        source = (sidebar.FRONT / 'app-chat-page.js').read_text(encoding='utf-8')
        self.page.add_script_tag(content='\n'.join(sidebar.function(source, name)
                                                  for name in ('hsHidden', 'hsSeen', 'hsFollow')))
        self.page.evaluate('''hidden => {
          window.seenCalls=[];
          window.api = (url, opts) => {
            const body=JSON.parse(opts.body); seenCalls.push(body);
            return Promise.resolve({seen_generation:body.generation});
          };
          window.termById = () => sample;
          const el=document.createElement('div'); el.style.display=hidden?'none':'block';
          document.body.appendChild(el);
          window.testView={sid:'adp_sample',engine:'codex',el,turnGeneration:100,busy:false};
        }''', hidden)

    def test_visible_settlement_clears_without_click(self):
        self.install_view()
        source = (sidebar.FRONT / 'app-chat-core.js').read_text(encoding='utf-8')
        self.page.add_script_tag(content='\n'.join(sidebar.function(source, name)
                                                  for name in ('hsStateGeneration', 'hsTurnSettled')))
        self.page.evaluate('''() => {
          for (const name of ['hsAskClose','hsFlush','hsFinalize','hsStatus','hsIndex','hsReconcile',
                              'hsGoalSync','hsAgentsSync','hsTasksSync','hsEngineSync']) window[name]=()=>{};
          window.hsBusy=(v,busy)=>v.busy=busy;
          testView.busy=true; hsTurnSettled(testView,{generation:100});
        }''')
        self.assertEqual(self.page.locator('.ti-completed-unread,.term-item.unread,.st-unread').count(), 0)
        self.assertEqual(self.page.evaluate('seenCalls'), [{'generation': 100}])
        self.assertEqual(self.errors, [])

    def test_return_from_background_acknowledges_once(self):
        self.install_view()
        self.page.evaluate('''() => {
          window.testHidden=true;
          Object.defineProperty(document,'hidden',{get:()=>testHidden, configurable:true});
          window.hsFollowTick=()=>{};
          hsFollow(testView); hsSeen(testView);
        }''')
        self.assertEqual(self.page.evaluate('seenCalls'), [])
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)
        self.page.evaluate("testHidden=false; document.dispatchEvent(new Event('visibilitychange'))")
        self.page.evaluate("window.dispatchEvent(new Event('focus'))")
        self.assertEqual(self.page.evaluate('seenCalls'), [{'generation': 100}])
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)

    def test_late_seen_response_does_not_ack_next_generation(self):
        self.install_view()
        self.page.evaluate('''() => {
          window.api=()=>new Promise(resolve=>window.resolveSeen=resolve);
          hsSeen(testView);
          sample.completion_generation=101; repaint();
          resolveSeen({seen_generation:100});
        }''')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)

    def test_failed_ack_retries_when_visible_again(self):
        self.install_view()
        self.page.evaluate('''() => {
          window.api=()=> { seenCalls.push('failed'); return Promise.reject(new Error('offline')); };
          hsSeen(testView);
        }''')
        self.page.evaluate('''() => {
          window.api=()=> { seenCalls.push('ok'); return Promise.resolve({seen_generation:100}); };
          hsSeen(testView);
        }''')
        self.page.evaluate('hsSeen(testView)')
        self.assertEqual(self.page.evaluate('seenCalls'), ['failed', 'ok'])
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)

    def test_first_list_preserves_ack_but_backend_switch_clears_it(self):
        self.page.add_script_tag(content='''
          let _termsScopeKey=null, _termsSource='', _termsVerified=false, _termsAt=0, _termsFetch=null;
          let scope='self'; function _termsScope() { return scope; }
        ''' + sidebar.function(sidebar.TERMS, '_termsEnsureScope'))
        self.page.locator('#panel-list .term-item').click()
        self.page.evaluate('_termsEnsureScope(); repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)
        self.page.evaluate("scope='remote'; _termsEnsureScope(); repaint()")
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)

    def test_busy_open_does_not_suppress_later_background_completion(self):
        self.install_view()
        self.page.evaluate('''() => {
          sample.busy=true; testView.busy=true; hsSeen(testView);
          testView.el.style.display='none'; sample.busy=false; testView.busy=false;
          hsSeen(testView); repaint();
        }''')
        self.assertEqual(self.page.evaluate('seenCalls'), [{'generation': 99}])
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)
        self.page.evaluate('repaint(); repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)

    def test_only_finished_unread_sessions_show_dot(self):
        for field in ('busy', 'agents', 'list_stale'):
            self.page.evaluate(f'sample.{field}=true; repaint()')
            self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)
            self.page.evaluate(f'sample.{field}=false; repaint()')
        self.page.evaluate('sample.unread=false; repaint()')
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)

    def test_star_menu_removed_and_legacy_state_ignored(self):
        self.page.evaluate('wbState.ui={\'sessionStar:["codex","codex-0"]\':true}; sample.unread=false; repaint()')
        self.page.locator('#panel-list .term-item').click(button='right')
        labels=self.page.evaluate('menuItems.map(x=>x.label).filter(Boolean)')
        self.assertFalse(any('星标' in label for label in labels))
        self.assertEqual(self.page.locator('.ti-star,.ti-completed-unread').count(), 0)
        self.assertNotIn('function toggleTermStar', sidebar.TERMS)
        self.assertEqual(self.errors, [])

    def test_hidden_completed_view_does_not_clear_unread(self):
        source = (sidebar.FRONT / 'app-chat-page.js').read_text(encoding='utf-8')
        self.page.add_script_tag(content='\n'.join(sidebar.function(source, name) for name in ('hsHidden', 'hsSeen')))
        self.page.evaluate('''() => {
          window.seenCalls=[];
          window.api = url => { seenCalls.push(url); return Promise.resolve(); };
          window.termById = () => sample;
          const el=document.createElement('div');
          el.style.display='none'; document.body.appendChild(el);
          window.testView={sid:'adp_sample',el};
          hsSeen(testView);
        }''')
        self.assertEqual(self.page.evaluate('seenCalls'), [])
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 1)
        self.page.evaluate("testView.el.style.display='block'; hsSeen(testView)")
        self.assertEqual(self.page.evaluate('seenCalls'), ['/session/adp_sample/seen'])
        self.assertEqual(self.page.locator('.ti-completed-unread').count(), 0)

    def test_cached_view_reopening_reports_seen(self):
        source = (sidebar.FRONT / 'app-chat-page.js').read_text(encoding='utf-8')
        self.page.add_script_tag(content=sidebar.function(source, 'hsShown'))
        self.page.evaluate('''() => {
          window.seenCalls=[];
          window.hsSeen=v=>seenCalls.push(v.sid);
          window.hsSituationSync=()=>{}; window.hsViewportChange=()=>{}; window.hsEvictColdSoon=()=>{};
          hsShown({sid:'cached-session',needLoad:false});
        }''')
        self.assertEqual(self.page.evaluate('seenCalls'), ['cached-session'])


if __name__ == '__main__':
    unittest.main()
