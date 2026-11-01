"""Attachment acceptance, public URL ownership, reconnect and browser rendering."""
import base64
from io import BytesIO
import os
from pathlib import Path
import tempfile
import unittest
import pytest
from unittest import mock

from engine_adapters.bus import EventBus
from engine_adapters.shepherd import ShepherdAdapter
from test_peer_message_route import Handler, Adapter, ui_contract
from test_frontend_recall import fn
from test_shepherd_images import PNG


class ImageProjection(unittest.TestCase):
    def test_same_named_images_in_same_millisecond_do_not_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            adapter=ShepherdAdapter(root,root,EventBus())
            images=[{'type':'image','name':'shot.png','data_b64':base64.b64encode(b).decode()}
                    for b in (b'first',b'second')]
            with mock.patch('engine_adapters.cli_turn.time.time',return_value=1):
                saved=adapter._stash('public',images)
            self.assertNotEqual(saved[0][1],saved[1][1])
            self.assertEqual([Path(row[1]).read_bytes() for row in saved],[b'first',b'second'])

    def test_failed_attachment_save_rejects_before_user_record_and_dispatch(self):
        with tempfile.TemporaryDirectory() as root:
            adapter=ShepherdAdapter(root,root,EventBus());events=[];adapter.bus.publish=events.append
            adapter.requests.claim('public','request',{'text':'look'})
            with mock.patch('engine_adapters.cli.CliAdapter._stash',return_value=[]), \
                 mock.patch.object(adapter,'_turn_dispatch') as dispatch:
                with self.assertRaises(ValueError):
                    adapter._run_serial('public','look',atts=[{'type':'image','data_b64':PNG}],
                                        client_request_id='request')
            dispatch.assert_not_called()
            self.assertEqual(adapter.messages('public'),[])
            error=next(e['properties'] for e in events if e['type']=='session.error')
            self.assertEqual(error['requestID'],'request')
            self.assertEqual(error['error']['code'],'prompt_not_started')
            self.assertEqual(error['error']['stage'],'attachment')
            self.assertEqual(adapter.requests.get('public','request')['status'],'rejected')

    def test_persisted_url_and_sse_use_public_shepherd_owner(self):
        with tempfile.TemporaryDirectory() as root:
            adapter=ShepherdAdapter(root,root,EventBus())
            events=[]; adapter.bus.publish=events.append
            # Exercise the real shared upload/message path; stop before any native dispatch.
            with mock.patch.object(adapter,'_touch',side_effect=StopIteration):
                with self.assertRaises(StopIteration):
                    adapter._run('public','look',atts=[{'type':'image','name':'shot.png',
                        'media_type':'image/png','data_b64':PNG}])
            image_events=[e['properties']['part'] for e in events
                          if e['type']=='message.part.updated' and e['properties']['part']['type']=='image']
            self.assertEqual(len(image_events),1)
            part=image_events[0]
            self.assertEqual(part['sessionID'],'public')
            self.assertNotIn('data_b64',part)
            reopened=ShepherdAdapter(root,root,EventBus())
            messages=reopened.messages('public')
            self.assertEqual(messages[0]['parts'][1]['url'],part['url'])
            reopened.index['public']={'id':'public'}
            handler=Handler('public',{}); handler.path=part['url']; handler.wfile=BytesIO()
            handler.send_header=mock.Mock()
            with mock.patch.object(ui_contract,'_shepherd',return_value=reopened), \
                 mock.patch.object(ui_contract,'_adapter',return_value=Adapter()), \
                 mock.patch.object(ui_contract,'_engine_pref',return_value='codex'):
                handler._adapter_route('GET')
            self.assertEqual(handler.code,200)
            self.assertEqual(handler.wfile.getvalue(),base64.b64decode(PNG))
            handler.send_header.assert_any_call('Content-Type','image/png')


@unittest.skipUnless(os.environ.get('DESK_SHEPHERD_IMAGE_UI')=='1','opt-in isolated browser image rendering')
@pytest.mark.desktop
class BrowserImages(unittest.TestCase):
    def test_live_data_and_restored_url_render_on_mobile_viewport(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            engine = os.environ.get('DESK_IMAGE_BROWSER','chrome')
            browser = (p.webkit.launch(timeout=10000) if engine == 'webkit'
                       else p.chromium.launch(channel='chrome',timeout=10000))
            try:
                page=browser.new_page(viewport={'width':390,'height':844},is_mobile=True)
                page.route('http://fixture.test/**',lambda route: route.fulfill(
                    content_type='image/png' if '/asset/' in route.request.url else 'text/html',
                    body=base64.b64decode(PNG) if '/asset/' in route.request.url else
                    '<div id="turn"><div class="hs-u"><div class="hs-bubble"></div></div></div>'))
                page.goto('http://fixture.test/',timeout=5000)
                page.add_script_tag(content='\n'.join(fn(file,name) for file,name in (
                    ('app-chat-core.js','hsImgSrc'),('app-chat-model.js','hsTurnImgs'))))
                for attachment in ({'type':'image','media_type':'image/png','data_b64':PNG},
                                   {'type':'image','url':'/session/public/asset/shot.png'}):
                    page.evaluate('''a=>{
                        document.querySelector('.hs-bubble').replaceChildren();
                        window.turn={el:document.querySelector('#turn'),atts:[a]};
                        hsTurnImgs({},turn);hsTurnImgs({},turn);
                    }''',attachment)
                    page.wait_for_function('document.querySelector("img").naturalWidth===32',timeout=5000)
                    self.assertEqual(page.locator('img').count(),1)
            finally:
                browser.close()
