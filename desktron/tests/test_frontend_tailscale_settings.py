"""Phone setup controls with synthetic fetches only; no real Tailscale mutations."""
from pathlib import Path

import pytest
from frontend_i18n_js import BROWSER_SCRIPT

ROOT = Path(__file__).resolve().parents[2]
SETTINGS = (ROOT / 'frontend/app-settings-core.js').read_text(encoding='utf-8')
SOURCE = SETTINGS[SETTINGS.index('let _phoneQrPromise ='):SETTINGS.index('function _paintSystemArrangementsSettings')]


def test_setup_uses_local_revisioned_api_and_no_restart_or_cli_execution():
    assert "fetch('/workbench/tailscale'" in SOURCE
    assert 'expected_revision: config.revision' in SOURCE
    assert '/workbench/restart' not in SOURCE
    assert '配置 Serve' in SOURCE and 'Funnel' in SOURCE
    assert '_phoneTailscaleControls(card, state.tailscale_config, section, state.tailscale_config_error)' in SOURCE
    assert 'confirm' in SOURCE and '保存结果未知' in SOURCE


@pytest.mark.desktop
def test_tailscale_controls_save_conflict_unknown_and_remote_privacy():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.webkit.launch()
        try:
            for width in (320, 1280):
                page = browser.new_page(viewport={'width': width, 'height': 900})
                page.route('http://phone.test/**', lambda route: route.fulfill(content_type='text/html', body='''
                    <div id="settings-modal"><div class="settings-center-live" style="padding:12px">
                    <section class="sm-phone-section"><div class="sm-phone-cards"></div></section></div></div>'''))
                page.goto('http://phone.test/')
                page.add_style_tag(content=(ROOT / 'frontend/style.css').read_text(encoding='utf-8'))
                page.add_script_tag(content=BROWSER_SCRIPT)
                page.add_script_tag(content=(ROOT / 'frontend/vendor/qrcode.js').read_text(encoding='utf-8'))
                page.add_script_tag(content=SOURCE + '''
window.section=document.querySelector('.sm-phone-section'); window.calls=[]; window.fail='';
window.state={local:true,enabled:false,tailscale_config:{enabled:false,port:7766,revision:'new',managed:true,active_port:0,restart_required:false}};
window.fetch=async(path,options={})=>{
  calls.push({path,body:options.body});
  if(options.method==='PUT') {
    if(fail==='network') throw new TypeError('synthetic disconnect');
    if(fail==='conflict') return {ok:false,status:409,json:async()=>({error:'stale: refresh settings'})};
    const body=JSON.parse(options.body);
    state.tailscale_config={...state.tailscale_config,...body.config,revision:fail==='server'?'committed':'saved',restart_required:true};
    if(fail==='server') return {ok:false,status:503,json:async()=>({state:'unknown',error:'synthetic failure after commit'})};
    return {ok:true,status:200,json:async()=>state.tailscale_config};
  }
  return {ok:true,status:200,json:async()=>state};
};
''')
                for theme in ('dark', 'light'):
                    page.evaluate('theme=>document.documentElement.dataset.theme=theme', theme)
                    page.evaluate('renderPhoneConnectionSettings(section)')
                    assert page.locator('.sm-phone-ts-settings').count() == 1
                    assert not page.locator('.sm-phone-ts-enabled').is_checked()
                    assert page.locator('.sm-phone-ts-port').input_value() == '7766'
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                page.locator('.sm-phone-ts-enabled').check()
                page.locator('.sm-phone-ts-port').fill('80')
                page.locator('.sm-phone-ts-save').click()
                assert '1024' in page.locator('.sm-phone-ts-feedback').inner_text()
                assert page.evaluate("calls.filter(c=>c.body).length") == 0
                page.locator('.sm-phone-ts-port').fill('7780')
                page.evaluate("fail='conflict'")
                page.locator('.sm-phone-ts-save').click()
                assert 'stale' in page.locator('.sm-phone-ts-feedback').inner_text()
                assert page.locator('.sm-phone-ts-enabled').is_checked()
                assert page.locator('.sm-phone-ts-port').input_value() == '7780'
                page.evaluate("fail='network'")
                page.locator('.sm-phone-ts-save').click()
                assert '结果未知' in page.locator('.sm-phone-ts-feedback').inner_text()
                assert page.locator('.sm-phone-ts-save').is_disabled()
                assert page.evaluate('calls.filter(c=>c.body).length') == 2
                page.locator('.sm-phone-ts-refresh').click()
                assert not page.locator('.sm-phone-ts-enabled').is_checked()
                page.evaluate("fail=''")
                page.locator('.sm-phone-ts-enabled').check()
                page.locator('.sm-phone-ts-save').click()
                assert '重启' in page.locator('.sm-phone-ts-feedback').inner_text()
                assert page.evaluate('JSON.parse(calls.at(-1).body).expected_revision') == 'new'
                assert not page.locator('.sm-phone-ts-save').is_disabled()
                page.evaluate('''state.tailscale_config.active_port=7766;state.tailscale_config.restart_required=false;
                  state.tailscale={enabled:true,state:'unconfigured',command:'tailscale serve --bg http://127.0.0.1:7766'}''')
                page.locator('.sm-phone-ts-refresh').click()
                assert 'tailscale serve' in page.locator('.sm-phone-ts-command').inner_text()
                page.evaluate('''state.tailscale={enabled:true,state:'public',command:''};renderPhoneConnectionSettings(section)''')
                assert 'Funnel' in page.locator('.sm-phone-ts-hint').inner_text()
                assert page.locator('.sm-phone-qr').count() == 0
                page.evaluate('''state.tailscale={enabled:true,state:'ready',address:'https://phone.example.ts.net/',url:'https://phone.example.ts.net/?token=synthetic-fixture'};renderPhoneConnectionSettings(section)''')
                assert page.locator('[data-entry="tailscale"] .sm-phone-qr svg').count() == 1
                page.locator('.sm-phone-ts-enabled').uncheck()
                page.locator('.sm-phone-ts-save').click()
                assert page.locator('[data-entry="tailscale"]').count() == 0
                page.evaluate("fail='server'")
                page.locator('.sm-phone-ts-enabled').check()
                page.locator('.sm-phone-ts-save').click()
                assert '结果未知' in page.locator('.sm-phone-ts-feedback').inner_text()
                assert page.locator('.sm-phone-ts-save').is_disabled()
                page.locator('.sm-phone-ts-refresh').click()
                assert page.locator('.sm-phone-ts-enabled').is_checked()
                assert not page.locator('.sm-phone-ts-save').is_disabled()
                assert page.evaluate('state.tailscale_config.revision') == 'committed'
                assert all(c['path'] in ('/workbench/tailscale', '/desk-lan-access/status') for c in page.evaluate('calls'))
                page.evaluate('state.local=false;renderPhoneConnectionSettings(section)')
                assert page.locator('.sm-phone-ts-settings').count() == 0
                assert page.locator('.sm-phone-qr').count() == 0
                assert 'synthetic-fixture' not in page.locator('.sm-phone-cards').inner_text()
                page.close()
        finally:
            browser.close()
