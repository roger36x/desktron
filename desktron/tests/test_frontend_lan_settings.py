"""LAN controls and QR visibility, with synthetic HTTP responses only."""
import pytest
from test_frontend_tailscale_settings import ROOT, SOURCE, SETTINGS, BROWSER_SCRIPT


def test_lan_controls_use_local_revisioned_api_and_never_restart_or_open_firewall():
    assert "fetch('/workbench/lan'" in SOURCE
    assert '_phoneLanControls(card, state.lan_config, section, state.lan_config_error)' in SOURCE
    assert 'expected_revision: config.revision' in SOURCE
    assert '停用也需重启才能撤销监听' in SOURCE
    assert '/workbench/restart' not in SOURCE
    assert 'New-NetFirewallRule' not in SOURCE
    assert 'section._phoneLanSaveUnknown' in SOURCE


@pytest.mark.desktop
def test_disabling_entry_during_delayed_qr_does_not_destroy_settings():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            for key, control in (('lan', 'lan'), ('tailscale', 'ts')):
                page = browser.new_page()
                page.set_content('<div id="settings-modal"><section class="sm-phone-section"><div class="sm-phone-cards"></div></section></div>')
                page.add_script_tag(content=BROWSER_SCRIPT)
                page.add_script_tag(content=(ROOT / 'frontend/vendor/qrcode.js').read_text(encoding='utf-8'))
                page.add_script_tag(content=SOURCE + '''
window.section=document.querySelector('.sm-phone-section');
window.state={local:true,enabled:true,address:'http://192.168.50.10:7765/',url:'http://192.168.50.10:7765/?token=synthetic',
 tailscale:{enabled:true,state:'ready',address:'https://phone.example.ts.net/',url:'https://phone.example.ts.net/?token=synthetic'},
 lan_config:{enabled:true,interface:'192.168.50.10/24',revision:'lan',managed:true,candidates:[]},
 tailscale_config:{enabled:true,port:7766,revision:'ts',managed:true}};
window.fetch=async(path,options={})=>({ok:true,status:200,json:async()=>options.method==='PUT'
 ? {...(path.endsWith('/lan')?state.lan_config:state.tailscale_config),...JSON.parse(options.body).config,revision:'saved',restart_required:true}:state});
_phoneQrLibrary=()=>new Promise(resolve=>{window.finishQr=resolve});
void renderPhoneConnectionSettings(section);
''')
                page.locator('.sm-phone-' + control + '-enabled').uncheck()
                page.locator('.sm-phone-' + control + '-save').click()
                assert page.locator('[data-entry="' + key + '"]').count() == 0
                page.evaluate('finishQr(window.qrcode)')
                page.wait_for_function('!section._phoneLoading')
                assert page.locator('.sm-phone-lan-settings').count() == 1
                assert page.locator('.sm-phone-ts-settings').count() == 1
                assert page.locator('.sm-phone-qr svg').count() == 1
                assert page.get_by_role('button', name='重试', exact=True).count() == 0
                page.close()
        finally:
            browser.close()


@pytest.mark.desktop
def test_navigation_during_qr_loading_rebuilds_connection_controls():
    from playwright.sync_api import sync_playwright
    navigation = SETTINGS[SETTINGS.index('function _activateSettingsView('):SETTINGS.index('let _phoneQrPromise =')]
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            for outcome in ('loaded', 'failed'):
                page = browser.new_page()
                page.set_content('''<div id="settings-modal"><div id="sm-host">
                  <section class="sm-phone-section" data-settings-view="phone"><div class="sm-phone-cards"></div></section>
                  <section data-settings-view="engines"></section></div></div>''')
                page.add_script_tag(content=BROWSER_SCRIPT)
                page.add_script_tag(content=(ROOT / 'frontend/vendor/qrcode.js').read_text(encoding='utf-8'))
                page.add_script_tag(content=SOURCE + '''
let _smActiveView='phone'; const _SM_VIEW_COPY={}; function _paintSettingsNav(){}
window.section=document.querySelector('.sm-phone-section'); window.qrCalls=0; window.reads=0;
window.fetch=async()=>{reads++;return {ok:true,json:async()=>({local:true,enabled:true,
 address:'http://phone.test/',url:'http://phone.test/?token=synthetic',
 lan_config:{enabled:true,interface:'',revision:'lan',managed:true,candidates:[]},
 tailscale_config:{enabled:false,port:7766,revision:'ts',managed:true}})}};
_phoneQrLibrary=()=>++qrCalls===1?new Promise((resolve,reject)=>{window.finishQr=resolve;window.failQr=reject}):Promise.resolve(window.qrcode);
''' + navigation + "\n_activateSettingsView('phone');")
                page.wait_for_function('qrCalls===1')
                page.evaluate("_activateSettingsView('engines');_activateSettingsView('phone')")
                assert page.locator('.sm-phone-cards').inner_text() == ''
                if outcome == 'loaded':
                    page.evaluate('finishQr(window.qrcode)')
                else:
                    page.evaluate("failQr(new Error('synthetic QR failure'))")
                page.wait_for_function('!section._phoneLoading && reads===2')
                assert page.locator('.sm-phone-lan-settings').count() == 1
                assert page.locator('.sm-phone-ts-settings').count() == 1
                assert page.locator('.sm-phone-qr svg').count() == 1
                assert page.locator('.sm-phone-copy-link').count() == 1
                assert page.locator('.sm-phone-lan-refresh').is_enabled()
                page.close()
        finally:
            browser.close()


@pytest.mark.desktop
def test_lan_save_errors_actual_listener_qr_and_remote_privacy():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            for width in (320, 1280):
                page = browser.new_page(viewport={'width': width, 'height': 1000})
                page.route('http://phone.test/**', lambda route: route.fulfill(content_type='text/html', body='''
                  <div id="settings-modal"><div class="settings-center-live" style="padding:12px">
                  <section class="sm-phone-section"><div class="sm-phone-cards"></div></section></div></div>'''))
                page.goto('http://phone.test/')
                page.add_style_tag(content=(ROOT / 'frontend/style.css').read_text(encoding='utf-8'))
                page.add_script_tag(content=BROWSER_SCRIPT)
                page.add_script_tag(content=(ROOT / 'frontend/vendor/qrcode.js').read_text(encoding='utf-8'))
                page.add_script_tag(content=SOURCE + '''
window.section=document.querySelector('.sm-phone-section'); window.calls=[]; window.fail='';
window.state={local:true,enabled:false,lan_config:{enabled:false,interface:'',revision:'new',managed:true,
 active_interface:'',restart_required:false,candidates:['192.168.50.10/24'],detection_error:''}};
window.fetch=async(path,options={})=>{
 calls.push({path,body:options.body});
 if(options.method==='PUT') {
  if(fail==='network') throw new TypeError('synthetic disconnect');
  if(fail==='conflict') return {ok:false,status:409,json:async()=>({error:'stale: refresh settings'})};
  const body=JSON.parse(options.body);
  state.lan_config={...state.lan_config,...body.config,revision:'saved',restart_required:true};
  if(fail==='server') return {ok:false,status:503,json:async()=>({state:'unknown',error:'synthetic postcommit failure'})};
  return {ok:true,status:200,json:async()=>state.lan_config};
 }
 return {ok:true,status:200,json:async()=>state};
};
''')
                for theme in ('dark', 'light'):
                    page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
                    page.evaluate('renderPhoneConnectionSettings(section)')
                    assert page.locator('.sm-phone-lan-settings').count() == 1
                    assert page.locator('.sm-phone-lan-interface').input_value() == '192.168.50.10/24'
                    assert page.locator('.sm-phone-qr').count() == 0
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.sm-phone-lan-enabled').check()
                page.locator('.sm-phone-lan-interface').fill('')
                page.locator('.sm-phone-lan-save').click()
                assert 'IPv4' in page.locator('.sm-phone-lan-feedback').inner_text()
                assert page.evaluate('calls.filter(c=>c.body).length') == 0
                page.locator('.sm-phone-lan-interface').fill('192.168.50.10/24')
                page.evaluate("fail='conflict'")
                page.locator('.sm-phone-lan-save').click()
                assert 'stale' in page.locator('.sm-phone-lan-feedback').inner_text()
                assert not page.locator('.sm-phone-lan-save').is_disabled()
                page.evaluate("fail='network'")
                page.locator('.sm-phone-lan-save').click()
                assert '结果未知' in page.locator('.sm-phone-lan-feedback').inner_text()
                assert page.locator('.sm-phone-lan-save').is_disabled()
                page.locator('.sm-phone-lan-refresh').click()
                assert not page.locator('.sm-phone-lan-enabled').is_checked()
                page.evaluate("fail=''")
                page.locator('.sm-phone-lan-enabled').check()
                page.locator('.sm-phone-lan-save').click()
                assert '重启' in page.locator('.sm-phone-lan-feedback').inner_text()
                assert page.evaluate('JSON.parse(calls.at(-1).body).expected_revision') == 'new'
                assert page.locator('.sm-phone-qr').count() == 0  # Saved is not active.
                page.evaluate('''state.enabled=true;state.address='http://192.168.50.10:7765/';
                  state.url=state.address+'?token=synthetic-fixture';
                  state.lan_config.active_interface='192.168.50.10/24';state.lan_config.restart_required=false;
                  renderPhoneConnectionSettings(section)''')
                assert page.locator('[data-entry="lan"] .sm-phone-qr svg').count() == 1
                assert 'synthetic-fixture' not in page.locator('.sm-phone-cards').inner_text()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.sm-phone-lan-enabled').uncheck()
                page.locator('.sm-phone-lan-save').click()
                assert page.locator('[data-entry="lan"]').count() == 0
                page.evaluate("fail='server'")
                page.locator('.sm-phone-lan-enabled').check()
                page.locator('.sm-phone-lan-save').click()
                assert '结果未知' in page.locator('.sm-phone-lan-feedback').inner_text()
                assert page.locator('.sm-phone-lan-save').is_disabled()
                page.locator('.sm-phone-lan-refresh').click()
                assert page.locator('.sm-phone-lan-enabled').is_checked()
                assert not page.locator('.sm-phone-lan-save').is_disabled()
                page.evaluate('state.lan_config.managed=false;renderPhoneConnectionSettings(section)')
                assert page.locator('.sm-phone-lan-save').is_disabled()
                assert 'DESKTRON_REMOTE_BIND' in page.locator('.sm-phone-lan-feedback').inner_text()
                page.evaluate('state.local=false;renderPhoneConnectionSettings(section)')
                assert page.locator('.sm-phone-lan-settings').count() == 0
                assert page.locator('.sm-phone-qr').count() == 0
                assert 'synthetic-fixture' not in page.locator('.sm-phone-cards').inner_text()
                assert all(c['path'] in ('/workbench/lan','/desk-lan-access/status') for c in page.evaluate('calls'))
                page.close()
        finally:
            browser.close()
