"""Ordinary adapter bridge is explicit, root-scoped and not a rollout switch."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))
argv = sys.argv
sys.argv = ['ui_server.py', str(ROOT / 'frontend'), '7999', '7998', str(ROOT)]
try:
    import engine_adapters as adapters
finally:
    sys.argv = argv
from engine_profiles import ProfileError

pytestmark = pytest.mark.skipif(os.name != 'posix', reason='POSIX descriptor backend only')


@pytest.fixture(params=[adapters.ClaudeAdapter, adapters.CodexAdapter, adapters.PiAdapter])
def adapter(tmp_path, request):
    data = tmp_path / 'data'
    data.mkdir(mode=0o700)
    work = tmp_path / 'workspace'
    work.mkdir(mode=0o700)
    return request.param(str(data), str(work), adapters.EventBus())


def test_constructing_adapter_does_not_activate_private_storage(adapter):
    root = Path(adapter.dir).parents[1]
    assert not (root / 'engine-profiles').exists()
    assert '_ordinary_profile_runtime' not in adapter.__dict__
    # Explicit bridge access prepares a profile; it does not migrate/register
    # any legacy rows or replace the adapter's production dispatch implementation.
    runtime = adapter.ordinary_storage_runtime()
    assert runtime.profile.root == str(root)
    assert runtime.profile.engine == adapter.name
    assert runtime.owned() == {}
    assert runtime is adapter.ordinary_storage_runtime()


def test_bridge_initialization_shared_across_concurrent_callers(adapter):
    with ThreadPoolExecutor(max_workers=4) as pool:
        runtimes = list(pool.map(lambda _: adapter.ordinary_storage_runtime(), range(8)))
    assert len({id(runtime) for runtime in runtimes}) == 1


def test_hidden_worker_cannot_use_ordinary_scope(adapter):
    adapter.owner = object()
    with pytest.raises(ProfileError, match='ordinary_adapter'):
        adapter.ordinary_storage_runtime()
    assert not (Path(adapter.dir).parents[1] / 'engine-profiles').exists()


def test_replaced_profile_invalidates_cached_bridge(adapter):
    runtime = adapter.ordinary_storage_runtime()
    home = Path(runtime.binding.home)
    home.rename(home.with_name('detached'))
    home.mkdir(mode=0o700)
    with pytest.raises(ProfileError):
        adapter.ordinary_storage_runtime()


def test_shepherd_facade_cannot_prepare_ordinary_profile(tmp_path):
    facade = adapters.ShepherdAdapter.__new__(adapters.ShepherdAdapter)
    facade.dir = str(tmp_path / 'adapter/shepherd')
    with pytest.raises(ProfileError, match='ordinary_adapter'):
        facade.ordinary_storage_runtime()
    assert not (tmp_path / 'engine-profiles').exists()
