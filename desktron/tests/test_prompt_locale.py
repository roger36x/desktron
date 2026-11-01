"""English prompt variants: per-Conversation language, fixed at creation."""
import os
import re
from pathlib import Path

import pytest

import prompt_locale
import task_ledger
from context_delivery import compose_system
from engine_adapters import ClaudeAdapter, CodexAdapter, PiAdapter
from engine_adapters.bus import EventBus
from engine_adapters import pi as pi_module
from engine_adapters.codex import CODEX_DELTA
from grounding import builder
from shepherd import recap as shepherd_recap
from shepherd.claude_codec import BASH_CWD_PROMPT, static_system_prompt
from shepherd.history_prompt import history_prompt

ROOT = Path(__file__).resolve().parents[2]
CJK = re.compile(r'[一-鿿]')

# Every fixed Python prompt string that has an English translation.
REGISTERED = {
    ClaudeAdapter.CLAUDE_DELTA, ClaudeAdapter.INTENT_RULE, CODEX_DELTA,
    CodexAdapter.CHANNEL_RULE, CodexAdapter.INTENT_RULE, BASH_CWD_PROMPT,
    pi_module.PI_SAFETY_TEXT, pi_module.PI_DELTA_TEXT, pi_module.SUBAGENT_DELIVERY_NOTICE,
    shepherd_recap._RECAP_CONTEXT_NOTICE, builder.TASK_ENTRY, task_ledger.REVIEW_HINT_HEAD,
}

# Model-facing Markdown sources and their English siblings.
PROMPT_FILES = ('prompts/style.md', 'AGENTS.md', 'common/orientation.md', 'common/principles.md')


def test_every_registered_string_has_exactly_one_translation():
    assert set(prompt_locale.EN) == REGISTERED
    for zh, en in prompt_locale.EN.items():
        assert en.strip() and not CJK.search(en), zh


def test_text_falls_back_to_chinese_for_unknown_and_chinese_locale():
    zh = ClaudeAdapter.CLAUDE_DELTA
    assert prompt_locale.text(zh, 'zh') == zh
    assert prompt_locale.text(zh, '') == zh
    assert prompt_locale.text('未登记', 'en') == '未登记'
    assert prompt_locale.text(zh, 'en-US') == prompt_locale.EN[zh]


@pytest.mark.parametrize('rel', PROMPT_FILES)
def test_prompt_files_have_english_siblings(rel):
    en = prompt_locale.localized_rel(str(ROOT), rel, 'en')
    assert en != rel and (ROOT / en).is_file()
    assert not CJK.search((ROOT / en).read_text(encoding='utf-8').replace('简体中文', ''))
    assert prompt_locale.localized_rel(str(ROOT), rel, 'zh') == rel


def test_english_style_carries_the_detection_marker():
    first = (ROOT / 'prompts/style.en.md').read_text(encoding='utf-8').splitlines()[0]
    assert first == prompt_locale.EN_STYLE_MARKER
    assert prompt_locale.detect('x\n\n' + first + '\nbody') == 'en'
    assert prompt_locale.detect((ROOT / 'prompts/style.md').read_text(encoding='utf-8')) == 'zh'


def test_every_skill_has_an_english_instruction_with_the_same_name():
    skills = sorted((ROOT / '.claude/skills').glob('*/SKILL.md'))
    assert skills
    for skill in skills:
        english = skill.with_name('SKILL.en.md')
        assert english.is_file(), skill
        name = re.search(r'^name:\s*(\S+)', skill.read_text(encoding='utf-8'), re.M).group(1)
        text = english.read_text(encoding='utf-8')
        assert re.search(r'^name:\s*' + re.escape(name) + r'\s*$', text, re.M), english
        assert re.search(r'^description:\s*\S', text, re.M), english


def test_english_user_guide_mirrors_current_pages():
    docs = ROOT / 'desk-docs'
    current = {p.relative_to(docs).as_posix() for p in docs.rglob('*.md')
               if 'en' not in p.relative_to(docs).parts and not re.match(r'\d\d-(?!index)', p.name)}
    english = {p.relative_to(docs / 'en').as_posix() for p in (docs / 'en').rglob('*.md')}
    assert current == english
    for page in (docs / 'en').rglob('*.md'):
        for target in re.findall(r'\]\(([^)#:]+)(?:#[^)]*)?\)', page.read_text(encoding='utf-8')):
            assert (page.parent / target).exists(), (page, target)


def test_english_assets_are_linked_into_the_workspace():
    for rel in ('desktron/bin/desktron', 'desktron/lib/desk_launcher.py'):
        assert 'AGENTS.en.md' in (ROOT / rel).read_text(encoding='utf-8')


def test_grounding_constant_uses_english_sources_only_for_english():
    zh = builder.constant(str(ROOT), '', locale='zh')
    en = builder.constant(str(ROOT), '', locale='en')
    assert '## 工作环境' in zh and '## Working environment' not in zh
    assert '## Working environment' in en and '## 工作环境' not in en
    assert '# Desktron Working Rules' in en and '## Judgment rules' in en
    assert '### Desk documentation' in en
    assert str(ROOT / 'desk-docs/en/00-index.md') in en


def test_compose_system_translates_engine_deltas_for_english():
    zh = compose_system('BASE', {}, ClaudeAdapter.CLAUDE_DELTA)
    en = compose_system('BASE', {'prompt_locale': 'en'}, ClaudeAdapter.CLAUDE_DELTA)
    assert ClaudeAdapter.CLAUDE_DELTA in zh
    assert prompt_locale.EN[ClaudeAdapter.CLAUDE_DELTA] in en
    assert ClaudeAdapter.CLAUDE_DELTA not in en


@pytest.mark.parametrize('cls', (ClaudeAdapter, CodexAdapter, PiAdapter))
def test_system_body_selects_style_by_session_language(cls, tmp_path):
    worker = cls(str(tmp_path / cls.name), str(ROOT), EventBus())
    assert worker.system_body({}).startswith('# 表达要求')
    english = worker.system_body({'prompt_locale': 'en'})
    assert english.startswith(prompt_locale.EN_STYLE_MARKER)
    if cls is CodexAdapter:
        assert prompt_locale.EN[CodexAdapter.CHANNEL_RULE] in english


def test_session_language_is_recorded_at_creation_and_kept(tmp_path, monkeypatch):
    worker = ClaudeAdapter(str(tmp_path / 'claude'), str(ROOT), EventBus())
    monkeypatch.setattr(prompt_locale, 'default_locale', lambda: '')
    legacy = worker.create_session()
    assert 'prompt_locale' not in legacy and worker.session_locale(legacy['id']) == 'zh'
    english = worker.create_session(opts={'prompt_locale': 'en-GB'})
    assert english['prompt_locale'] == 'en'
    assert worker.session_options(english['id'])['prompt_locale'] == 'en'
    monkeypatch.setattr(prompt_locale, 'default_locale', lambda: 'en')
    backend = worker.create_session()
    assert worker.session_locale(backend['id']) == 'en'
    explicit = worker.create_session(opts={'prompt_locale': 'zh'})
    assert worker.session_locale(explicit['id']) == 'zh'


def test_shepherd_static_prompt_and_history_follow_the_system_text():
    zh = static_system_prompt('# 表达要求')
    en = static_system_prompt(prompt_locale.EN_STYLE_MARKER)
    assert zh.endswith(BASH_CWD_PROMPT)
    assert en.endswith(prompt_locale.EN[BASH_CWD_PROMPT])

    class Connector:
        store_root = '/tmp/store'
        binding = {'conversation_id': 'conv'}

    assert history_prompt(Connector()).startswith('较早的对话')
    english = history_prompt(Connector(), 'en')
    assert english.startswith('Earlier conversation') and not CJK.search(english)


def test_review_hint_head_is_translated(monkeypatch, tmp_path):
    assert prompt_locale.text(task_ledger.REVIEW_HINT_HEAD, 'en').startswith('<Task close-out check>')
    assert 'locale' in task_ledger.consume_review_hint.__code__.co_varnames


def test_frontend_reports_the_interface_language():
    page = (ROOT / 'frontend/app-chat-page.js').read_text(encoding='utf-8')
    assert page.count('body.prompt_locale = DeskI18n.locale') == 2
    init = (ROOT / 'frontend/app-init.js').read_text(encoding='utf-8')
    assert 'saveState({ prompt_locale: locale })' in init
    assert 'addEventListener("desk-locale", syncPromptLocale)' in init
