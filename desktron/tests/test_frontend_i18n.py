"""Device-local bilingual UI: explicit owned nodes only; no setup side effects."""
import json
from pathlib import Path
import subprocess
import unittest
import pytest

ROOT = Path(__file__).resolve().parents[2]
from frontend_i18n_js import PACK_FILES, SOURCE


def node_check(body):
    harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const nodes = [];
class Node {
  constructor() { this.attrs = {}; this.children = []; this.textContent = ''; this.value = ''; nodes.push(this); }
  getAttribute(key) { return this.attrs[key] || ''; }
  setAttribute(key, value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  append(...items) { this.children.push(...items); }
  appendChild(item) { this.children.push(item); }
}
const memory = new Map();
const document = {documentElement: {}, createElement: () => new Node(),
  querySelectorAll: selector => nodes.filter(node => Object.hasOwn(node.attrs, selector.slice(1,-1)))};
const localStorage = {getItem: key => memory.get(key), setItem: (key, value) => memory.set(key, value)};
const context = vm.createContext({document, localStorage});
vm.runInContext(SOURCE, context);
const i18n = vm.runInContext('DeskI18n', context);
'''
    result = subprocess.run(['node', '-'], input='const SOURCE=' + json.dumps(SOURCE) + ';\n' + harness + body,
                            text=True, capture_output=True, timeout=10, encoding='utf-8')
    if result.returncode:
        raise AssertionError(result.stderr)


class BilingualUI(unittest.TestCase):
    def test_every_pack_matches_the_chinese_reference(self):
        # Each pack has exactly the reference keys, and every form keeps the same placeholders.
        node_check(r'''
const forms = value => typeof value === 'object' ? Object.values(value) : [value];
const slots = text => (String(text).match(/\{\w+\}/g) || []).sort();
const base = i18n.packs.zh.messages;
assert.ok(Object.keys(base).length >= 70);
for (const [code, pack] of Object.entries(i18n.packs)) {
  assert.ok(pack.label && pack.tag, code);
  assert.deepEqual(Object.keys(pack.messages).sort(), Object.keys(base).sort(), code);
  for (const [key, value] of Object.entries(pack.messages)) {
    for (const text of forms(value)) assert.deepEqual(slots(text), slots(forms(base[key])[0]), code + ' ' + key);
    if (typeof value === 'object') assert.ok('other' in value, code + ' ' + key);
  }
}
''')

    def test_new_language_needs_only_a_pack(self):
        # A third pack registers, appears in the picker, falls back per key and keeps zh text.
        node_check(r'''
i18n.register('ja', {label: '日本語', tag: 'ja-JP', fallback: 'en', messages: {'nav.apps': 'アプリ'}});
const select = i18n.picker().children[1];
assert.deepEqual(select.children.map(o => o.value), ['zh', 'en', 'ja']);
i18n.setLocale('ja');
assert.equal(i18n.t('nav.apps'), 'アプリ');
assert.equal(i18n.t('nav.chat'), 'Chat');                    // declared fallback
assert.equal(i18n.t('nav.chat', {}, '聊天'), 'Chat');
assert.equal(document.documentElement.lang, 'ja-JP'); assert.equal(i18n.tag, 'ja-JP');
i18n.setLocale('xx'); assert.equal(i18n.locale, 'zh');      // unknown code returns to Chinese
''')

    def test_plural_forms_follow_the_pack_language(self):
        node_check(r'''
i18n.setLocale('en');
assert.equal(i18n.t('setup.remaining', {count: 1}), '1 step remaining');
assert.equal(i18n.t('setup.remaining', {count: 3}), '3 steps remaining');
i18n.setLocale('zh');
assert.equal(i18n.t('setup.remaining', {count: 1}), '还需完成 1 项');
''')

    def test_switch_preserves_user_content_children_and_state(self):
        node_check(r'''
const label = new Node(); i18n.bind(label, 'nav.apps');
const title = new Node(); i18n.bind(title, 'nav.search', {}, 'title');
const draft = new Node(); draft.value = '工作流 / unsent draft'; draft.textContent = '安排';
const parent = new Node(); const icon = new Node(); parent.append(icon, label);
const callback = () => {}; parent.onclick = callback;
i18n.setLocale('en');
assert.equal(label.textContent, 'Apps'); assert.equal(title.attrs.title, 'Search conversations');
assert.equal(draft.value, '工作流 / unsent draft'); assert.equal(draft.textContent, '安排');
assert.equal(parent.children[0], icon); assert.equal(parent.onclick, callback);
assert.equal(document.documentElement.lang, 'en'); assert.equal(memory.get('cd_locale'), 'en');
assert.equal(i18n.t('setup.remaining', {count: 2}), '2 steps remaining');
i18n.setLocale('zh'); assert.equal(label.textContent, '应用');
''')

    def test_preference_persists_and_storage_failure_is_safe(self):
        node_check(r'''
i18n.setLocale('en');
const restored = vm.createContext({document, localStorage}); vm.runInContext(SOURCE, restored);
assert.equal(vm.runInContext('DeskI18n.locale', restored), 'en');
const blocked = vm.createContext({document, localStorage: {getItem() {throw Error();}, setItem() {throw Error();}}});
vm.runInContext(SOURCE, blocked); vm.runInContext("DeskI18n.setLocale('en')", blocked);
assert.equal(vm.runInContext('DeskI18n.locale', blocked), 'en');
i18n.setLocale('invalid'); assert.equal(i18n.locale, 'zh');
''')

    def test_switch_has_no_network_command_or_rerender_dependencies(self):
        for forbidden in ('fetch(', 'api(', 'openSetupGuide(', '_setupPaint(', 'location.reload', 'MutationObserver', 'innerHTML'):
            self.assertNotIn(forbidden, SOURCE)
        setup = (ROOT / 'frontend/app-settings-setup.js').read_text(encoding='utf-8')
        settings = (ROOT / 'frontend/app-settings-core.js').read_text(encoding='utf-8')
        self.assertIn('DeskI18n.picker()', setup)
        self.assertIn('DeskI18n.picker()', settings)
        self.assertIn("DeskI18n.t('settings.' + item.id)", settings)

    def test_chinese_keeps_caller_generated_text(self):
        node_check(r'''
const caption = new Node();
i18n.bind(caption, 'setup.runtimeMissing', {}, 'text', '缺少 Git 2.40+');
assert.equal(caption.textContent, '缺少 Git 2.40+');
i18n.setLocale('en'); assert.equal(caption.textContent, 'Missing or outdated dependencies');
i18n.setLocale('zh'); assert.equal(caption.textContent, '缺少 Git 2.40+');
''')

    def test_no_in_progress_scope_notice_in_ui(self):
        self.assertNotIn("'scope'", SOURCE)
        for name in ('app-settings-core.js', 'app-settings-setup.js'):
            text = (ROOT / 'frontend' / name).read_text(encoding='utf-8')
            self.assertNotIn("'scope'", text)
            self.assertNotIn('仍在迁移', text)

    def test_setup_bindings_pass_generated_chinese(self):
        setup = (ROOT / 'frontend/app-settings-setup.js').read_text(encoding='utf-8')
        paint = setup[setup.index('function _setupPaint('):]
        paint = paint[:paint.index('\n}\n')]
        for line in paint.splitlines():
            if 'DeskI18n.bind(' in line:
                self.assertIn("'text',", line, line.strip())

    @pytest.mark.desktop
    def test_settings_language_label_has_a_visible_gap_in_webkit(self):
        from playwright.sync_api import sync_playwright

        settings = (ROOT / 'frontend/app-settings-core.js').read_text(encoding='utf-8')
        start = settings.index('  const apLanguage = DeskI18n.picker();')
        mount = settings[start:settings.index('  ap.appendChild(apChoices);', start)]
        css = (ROOT / 'frontend/style.css').read_text(encoding='utf-8')
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            try:
                for width in (320, 390, 1280):
                    context = browser.new_context(viewport={'width': width, 'height': 800},
                                                  has_touch=width < 821, is_mobile=width < 821)
                    page = context.new_page()
                    page.set_content('<div class="settings-center-live" style="margin:10px;max-width:600px">'
                                     '<div class="sm-card"></div></div>')
                    page.add_style_tag(content=css)
                    page.add_script_tag(content=SOURCE + '\nconst ap = document.querySelector(".sm-card");\n' + mount)
                    for theme in ('dark', 'light'):
                        page.evaluate('theme => document.documentElement.dataset.theme = theme', theme)
                        for locale in ('zh', 'en'):
                            page.evaluate('locale => DeskI18n.setLocale(locale)', locale)
                            title = page.locator('.sm-language-row > span').bounding_box()
                            select = page.locator('.sm-language-row > select').bounding_box()
                            card = page.locator('.sm-card').bounding_box()
                            self.assertAlmostEqual(select['x'] - title['x'] - title['width'], 16, delta=0.5,
                                                   msg=(width, theme, locale))
                            self.assertAlmostEqual(title['y'] + title['height'] / 2,
                                                   select['y'] + select['height'] / 2, delta=0.5)
                            self.assertLessEqual(select['x'] + select['width'], card['x'] + card['width'])
                    # Still the original language control: switching remains device-local and live.
                    page.locator('[data-desk-language]').select_option('zh')
                    self.assertEqual(page.locator('.sm-language-row > span').inner_text(), '界面语言')
                    page.locator('[data-desk-language]').select_option('en')
                    self.assertEqual(page.locator('.sm-language-row > span').inner_text(), 'Interface language')
                    context.close()
            finally:
                browser.close()

    def test_picker_syncs_without_replacing_other_nodes(self):
        node_check(r'''
const first = i18n.picker(); const second = i18n.picker();
first.children[1].value = 'en'; first.children[1].onchange();
assert.equal(second.children[1].value, 'en');
assert.equal(first.children[0].textContent, 'Interface language');
''')


def _messages():
    out = subprocess.run(['node', '-'], input='const SOURCE=' + json.dumps(SOURCE) + r''';
const vm = require('node:vm');
const context = vm.createContext({document: {documentElement: {}, querySelectorAll: () => []},
  localStorage: {getItem() {}, setItem() {}}});
vm.runInContext(SOURCE, context);
const packs = vm.runInContext('DeskI18n.packs', context);
process.stdout.write(JSON.stringify(Object.fromEntries(Object.keys(packs.zh.messages)
  .map(key => [key, [packs.zh.messages[key], packs.en.messages[key]]]))));''',
                         text=True, capture_output=True, timeout=10, encoding='utf-8', check=True)
    return json.loads(out.stdout)


class TranslatedSurfaces(unittest.TestCase):
    """Surfaces reported untranslated in English mode stay bound to existing keys."""

    @classmethod
    def setUpClass(cls):
        cls.messages = _messages()
        cls.files = {name: (ROOT / name).read_text(encoding='utf-8') for name in (
            'frontend/app-settings-core.js', 'frontend/app-settings-sections.js', 'frontend/app-rail-nav.js',
            'frontend/app-assets-apps.js', 'frontend/app-chat-situation.js', 'frontend/app-approval.js',
            'frontend/app-panel-sidebar.js', 'frontend/app-init.js', 'frontend/pages/file-browser.html',
            'frontend/app-events.js', 'frontend/app-chat-opts.js', 'frontend/app-chat-page.js',
            'frontend/app-chat-tasksbar.js', 'frontend/app-panel-terms.js', 'frontend/app-panel-task-memo.js',
            'frontend/app-tasks.js', 'frontend/app-panel-arrangements.js')}

    def test_every_literal_key_exists(self):
        import re
        pattern = re.compile(r'''(?:DeskI18n\.(?:bind\([^,]+,|t\()|\btr\()\s*["']([a-z][\w.]+)["']''')
        # Prefixes such as "apps.title." + pool are covered by test_composed_keys_exist.
        used = {key for text in self.files.values() for key in pattern.findall(text) if not key.endswith('.')}
        self.assertGreater(len(used), 40)
        self.assertEqual(sorted(used - set(self.messages)), [])

    def test_composed_keys_exist(self):
        for key in ('resource.all', 'resource.official', 'resource.local', 'apps.title.all',
                    'apps.title.official', 'apps.title.local', 'apps.empty.all', 'apps.empty.official',
                    'apps.empty.local', 'apps.vibeHint', 'apps.gitHint', 'nav.apps', 'nav.arrangements'):
            self.assertIn(key, self.messages)
        situation = self.files['frontend/app-chat-situation.js']
        kinds = situation[situation.index('HS_SIT_ARTIFACT_KINDS = {'):].split('}', 1)[0]
        for kind, zh in __import__('re').findall(r"(\w+): '([^']+)'", kinds):
            self.assertEqual(self.messages['sit.kind.' + kind][0], zh)

    def test_engine_status_maps_keep_the_generated_chinese(self):
        core = self.files['frontend/app-settings-core.js']
        maps = core[core.index('const _SM_ENGINE_SUMMARY_KEYS'):core.index('function _smBindKnown')]
        pairs = __import__('re').findall(r'"([^"]+)": "([\w.]+)"', maps)
        self.assertGreaterEqual(len(pairs), 14)
        for zh, key in pairs:
            self.assertEqual(self.messages[key][0], zh, key)
            self.assertIn(f'"{zh}"', core.replace(maps, ''), zh)   # still produced by the settings code

    def test_sidebar_groups_are_found_by_marker_not_by_translated_text(self):
        init = self.files['frontend/app-init.js']
        body = init[init.index('function looseHead('):init.index('function apply(next)')]
        self.assertIn('[data-group=', body)
        self.assertNotIn('textContent', body)
        self.assertIn('lh.dataset.group = "chat"', self.files['frontend/app-panel-sidebar.js'])

    def test_user_text_drops_earlier_translation_binding(self):
        self.assertIn("if (recapText) DeskI18n.plain(recapEl, recapText);", self.files['frontend/app-chat-situation.js'])
        node_check(r'''
const node = new Node(); i18n.bind(node, 'sit.noRecap'); i18n.bind(node, 'composer.send', {}, 'title');
i18n.plain(node, '用户写的回顾');
i18n.setLocale('en');
assert.equal(node.textContent, '用户写的回顾');      // user text survives a language switch
assert.equal(node.attrs.title, 'Send');               // other bindings on the same node stay
i18n.plain(node, 'x', 'title'); i18n.setLocale('zh');
assert.equal(node.attrs.title, 'x'); assert.equal(node.attrs['data-desk-i18n'], undefined);
''')

    def test_composer_attributes_can_be_bound(self):
        node_check(r'''
const node = new Node(); i18n.setLocale('en');
i18n.bind(node, 'composer.placeholder', {}, 'data-ph'); i18n.bind(node, 'composer.attach', {}, 'data-tip');
assert.equal(node.attrs['data-ph'], 'Write something…'); assert.equal(node.attrs['data-tip'], 'Attach');
''')

    def test_file_browser_reads_host_dictionary(self):
        node_check(r'''assert.equal(vm.runInContext('globalThis.DeskI18n', context), i18n);''')
        page = self.files['frontend/pages/file-browser.html']
        self.assertIn('parent.DeskI18n.t(key, params, zh)', page)
        self.assertIn('attributeFilter: ["lang"]', page)

    def test_stored_chinese_labels_have_reference_entries(self):
        # Option and status labels stay Chinese in state; DeskI18n.label finds them by prefix.
        opts = self.files['frontend/app-chat-opts.js']
        for zh in __import__('re').findall(r'label: "([^"]*[\u4e00-\u9fff][^"]*)"', opts[:opts.index('const HS_UI_CODEX')]):
            self.assertIn(zh, {v[0] for k, v in self.messages.items() if k.startswith('model.')}, zh)
        node_check(r'''
i18n.setLocale('en');
assert.equal(i18n.label('处理中', 'status.'), 'Working');
assert.equal(i18n.label('默认', 'model.'), 'Default');
assert.equal(i18n.label('用户写的标题', 'status.'), '用户写的标题');
i18n.setLocale('zh'); assert.equal(i18n.label('处理中', 'status.'), '处理中');
''')

    def test_engine_descriptions_match_dictionary(self):
        events = self.files['frontend/app-events.js']
        block = events[events.index('const ENGINE_DESC = {'):].split('};', 1)[0]
        for engine, zh in __import__('re').findall(r'(\w+): "([^"]+)"', block):
            self.assertEqual(self.messages['engineMenu.desc.' + engine][0], zh)


class SourceTextPhrases(unittest.TestCase):
    """Screens translated by Chinese source text (DeskI18n.tx / txh) stay complete in every pack."""

    def test_every_source_literal_has_a_translation_with_matching_placeholders(self):
        import re
        out = subprocess.run(['node', '-'], input='const SOURCE=' + json.dumps(SOURCE) + r''';
const vm = require('node:vm');
const context = vm.createContext({document: {documentElement: {}, querySelectorAll: () => []},
  localStorage: {getItem() {}, setItem() {}}});
vm.runInContext(SOURCE, context);
const packs = vm.runInContext('DeskI18n.packs', context);
process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(packs).map(([c, p]) => [c, p.phrases]))));''',
                             text=True, capture_output=True, timeout=10, encoding='utf-8', check=True)
        phrases = json.loads(out.stdout)
        literal = re.compile(r'''DeskI18n\.txh?\(\s*(["'])((?:(?!\1)[^\\\n]|\\.)*)\1''')
        used = set()
        for path in (ROOT / 'frontend').glob('*.js'):
            if not path.name.startswith('app-i18n'):
                used |= {m.group(2) for m in literal.finditer(path.read_text(encoding='utf-8'))
                         if re.search(r'[一-鿿]', m.group(2))}
        self.assertGreater(len(used), 200)
        slots = lambda text: sorted(re.findall(r'\{\w+\}', text))
        for code, table in phrases.items():
            if code == 'zh':
                self.assertEqual(table, {})
                continue
            self.assertEqual(sorted(used - set(table)), [], code)
            for zh, value in table.items():
                for form in (value.values() if isinstance(value, dict) else [value]):
                    self.assertEqual(slots(form), slots(zh), (code, zh))

    def test_tx_escaping_plural_and_localize_round_trip(self):
        node_check(r'''
i18n.register('en', {label: 'English', tag: 'en', fallback: 'zh', messages: {},
  phrases: {'保存': 'Save', '共 {count} 项': {one: '{count} item', other: '{count} items'}, '<危险>': '<b>x</b>'}});
assert.equal(i18n.tx('保存'), '保存');                       // Chinese returns the source
i18n.setLocale('en');
assert.equal(i18n.tx('保存'), 'Save'); assert.equal(i18n.tx('未收录'), '未收录');
assert.equal(i18n.tx('共 {count} 项', {count: 1}), '1 item'); assert.equal(i18n.tx('共 {count} 项', {count: 2}), '2 items');
assert.equal(i18n.txh('<危险>'), '&lt;b&gt;x&lt;/b&gt;');
const node = new Node(); i18n.bindText(node, '保存', {}, 'title'); assert.equal(node.attrs.title, 'Save');
i18n.setLocale('zh'); assert.equal(node.attrs.title, '保存');
''')

    @pytest.mark.desktop
    def test_localize_switches_template_text_both_ways_and_skips_translate_no(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.webkit.launch()
            try:
                page = browser.new_page()
                page.set_content('<div id="t"><b>保存</b> <span title="关闭">x</span><i>用户的保存</i>'
                                 '<pre translate="no">保存</pre></div>')
                page.add_script_tag(content=SOURCE)
                result = page.evaluate('''() => {
                  const root = document.getElementById('t'), read = () => [root.querySelector('b').textContent,
                    root.querySelector('span').title, root.querySelector('i').textContent, root.querySelector('pre').textContent];
                  const seen = []; addEventListener('desk-locale', (e) => seen.push(e.detail.locale));
                  DeskI18n.setLocale('en'); DeskI18n.localize(root); const en = read();
                  DeskI18n.setLocale('zh'); DeskI18n.localize(root); const zh = read();
                  return {en, zh, seen};
                }''')
                self.assertEqual(result['en'], ['Save', 'Close', '用户的保存', '保存'])
                self.assertEqual(result['zh'], ['保存', '关闭', '用户的保存', '保存'])
                self.assertEqual(result['seen'], ['en', 'zh'])
            finally:
                browser.close()
