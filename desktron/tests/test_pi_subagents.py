"""Pinned upstream installation and explicit Desk extension boundary."""
import json
from pathlib import Path
import sys
import shutil
import subprocess
import pytest
from unittest import mock
ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'desktron/lib'))
import pi_extensions,pi_subagents


def fixture(root):
    for name,version in {pi_subagents.PACKAGE:pi_subagents.VERSION,**pi_subagents.DIRECT_DEPENDENCIES}.items():
        p=Path(root)/'node_modules'/name;p.mkdir(parents=True,exist_ok=True)
        (p/'package.json').write_text(json.dumps({'version':version}), encoding='utf-8')
    for path in [pi_subagents.extension_entry(root),pi_subagents.capability_api(root),
                 pi_subagents.child_factory(root),pi_subagents.control_api(root)]:
        p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text('fixture', encoding='utf-8')
    pi_subagents._write_managed_config(str(root))


def test_exact_locked_dependency_and_isolated_policy_slot():
    lock=json.loads(pi_subagents.lockfile().read_text(encoding='utf-8'))
    assert lock['packages']['node_modules/pi-subagents']['version']==pi_subagents.VERSION
    assert lock['packages']['']['dependencies'][pi_subagents.PACKAGE]==pi_subagents.VERSION
    assert lock['packages']['node_modules/pi-subagents']['dependencies']==pi_subagents.DIRECT_DEPENDENCIES
    for name, version in pi_subagents.DIRECT_DEPENDENCIES.items():
        assert lock['packages']['node_modules/'+name]['version']==version
    assert 'desk-v2' in pi_subagents.home()
    assert pi_subagents.MANAGED_CONFIG['maxSubagentSpawnsPerSession']==0
    assert pi_subagents.MANAGED_CONFIG['forceTopLevelAsync'] is False
    assert pi_subagents.MANAGED_CONFIG['globalConcurrencyLimit']==3


def test_valid_install_needs_no_network(tmp_path):
    fixture(tmp_path)
    assert Path(pi_subagents.extension_entry(tmp_path)).name == 'index.js'
    with mock.patch('subprocess.run',side_effect=AssertionError('network not needed')):
        assert pi_subagents.ensure(tmp_path)[0]


@pytest.mark.parametrize('required_file',[
    pi_subagents.extension_entry,
    pi_subagents.capability_api,
    pi_subagents.child_factory,
    pi_subagents.control_api,
])
def test_install_rejects_missing_or_legacy_typescript_layout(tmp_path,required_file):
    fixture(tmp_path)
    path=Path(required_file(tmp_path));path.unlink()
    path.with_suffix('.ts').write_text('legacy fixture')
    assert not pi_subagents.available(tmp_path)


@pytest.mark.parametrize('name', list(pi_subagents.DIRECT_DEPENDENCIES))
def test_missing_dependency_is_not_treated_as_ready(tmp_path, name):
    fixture(tmp_path)
    (Path(tmp_path) / 'node_modules' / name / 'package.json').unlink()
    assert not pi_subagents.available(tmp_path)


def test_config_repair_preserves_package(tmp_path):
    fixture(tmp_path);Path(pi_subagents.config_path(tmp_path)).write_text('{}', encoding='utf-8')
    with mock.patch('subprocess.run',side_effect=AssertionError('network not needed')):
        assert pi_subagents.ensure(tmp_path)[0]
    assert json.loads(Path(pi_subagents.config_path(tmp_path)).read_text(encoding='utf-8'))==pi_subagents.MANAGED_CONFIG


def test_missing_runtime_is_explicit_and_never_loads_ambient_extensions(tmp_path):
    assert not pi_subagents.available(tmp_path)
    with mock.patch('pi_subagents.available',return_value=True):
        assert pi_extensions.subagent_extension_args()==['--extension',str(ROOT/'desktron/lib/engine_adapters/pi_children.ts')]
    assert pi_extensions.subagent_env()=={}
    with mock.patch('pi_subagents.available',return_value=False):
        assert pi_extensions.subagent_extension_args()==[]
        assert pi_extensions.subagent_env(object())=={}


def test_web_tools_default_on_allow_explicit_disable_and_report_zero_key_provider():
    env={}
    assert pi_extensions.web_extension_args(env)==['--extension',str(ROOT/'desktron/lib/engine_adapters/pi_web.ts')]
    assert pi_extensions.web_extension_args({'DESKTRON_WEB_TOOLS':'0'})==[]
    assert pi_extensions.without_legacy_web_secrets({**env, 'VOLCENGINE_WEB_SEARCH_API_KEY':'fixture-secret'}) == env


def test_prompt_isolation_removes_explicit_skills_and_precedes_message_separator():
    command = pi_extensions.enforce_prompt_isolation([
        'pi', '--skill', '/tmp/one', '--skill=/tmp/two', '--', 'question'])
    assert command == [
        'pi', '--no-skills', '--no-context-files', '--', 'question']


def test_adapter_passes_only_bridge_environment():
    adapter=object()
    with mock.patch('pi_children.environment',return_value={'fixture':'value'}) as env, mock.patch('pi_subagents.available',return_value=True):
        assert pi_extensions.subagent_env(adapter)=={'fixture':'value'}
    env.assert_called_once_with(adapter)


@pytest.mark.parametrize('entry', ['command', 'absolute', 'relative'])
def test_sdk_root_resolves_command_names_and_explicit_paths(tmp_path, monkeypatch, entry):
    from shepherd.pi_connector import pi_sdk_root
    package = tmp_path / 'node_modules/@earendil-works/pi-coding-agent'
    (package / 'dist/bundle').mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'name':'@earendil-works/pi-coding-agent'}), encoding='utf-8')
    (package / 'dist/index.js').write_text('fixture', encoding='utf-8')
    cli = package / 'dist/bundle/cli.js'
    cli.write_text('#!/usr/bin/env node\n', encoding='utf-8'); cli.chmod(0o755)
    bin_dir = tmp_path / 'bin'; bin_dir.mkdir()
    if sys.platform == 'win32':
        launcher = bin_dir / 'pi.cmd'
        launcher.write_text('@node "%~dp0..\\node_modules\\@earendil-works\\pi-coding-agent\\dist\\bundle\\cli.js" %*\n', encoding='utf-8')
        (bin_dir / 'node.exe').touch()  # Discovery only; never executed.
    else:
        launcher = bin_dir / 'pi'
        launcher.symlink_to(cli)
    monkeypatch.setenv('PATH', str(bin_dir))
    monkeypatch.chdir(tmp_path)
    binary = {'command':'pi', 'absolute':str(launcher), 'relative':'./bin/' + launcher.name}[entry]
    assert pi_sdk_root(binary) == str(package)
    assert pi_sdk_root() == str(package)
    # Even with a valid PATH installation, an explicit invalid path fails closed.
    assert pi_sdk_root(str(tmp_path / 'missing-pi')) is None


def test_sdk_root_missing_or_invalid_explicit_entry_never_falls_back(tmp_path, monkeypatch):
    from shepherd.pi_connector import pi_sdk_root
    monkeypatch.setenv('PATH', str(tmp_path))
    assert pi_sdk_root('missing-pi') is None
    assert pi_sdk_root(str(tmp_path / 'missing-pi')) is None
    fake = tmp_path / 'pi'; fake.write_text('#!/bin/sh\n'); fake.chmod(0o755)
    assert pi_sdk_root('pi') is None
    assert pi_sdk_root() is None


def test_timeout_parameter_contract():
    node=shutil.which('node')
    if not node:pytest.skip('Node required for extension contract')
    result=subprocess.run([node,str(ROOT/'desktron/tests/pi-subagents/timeout.mjs')],
                          capture_output=True,text=True,timeout=30, encoding='utf-8')
    assert result.returncode==0,result.stderr+result.stdout


def test_old_standalone_worker_is_not_shipped():
    assert not (ROOT/'desktron/lib/engine_adapters/pi_child_worker.mjs').exists()
    assert 'subprocess.Popen' not in (ROOT/'desktron/lib/pi_children.py').read_text(encoding='utf-8')
