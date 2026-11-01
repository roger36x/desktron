"""真实文件故障与过期快照；不访问开发者数据。"""
import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

import pytest

from durable_json import read, read_value, write, ConflictError


def _register_process(path, i, barrier):
    snapshot = read(path)
    snapshot[str(i)] = {'domain': str(i)}
    barrier.wait(timeout=10)
    write(path, snapshot)


def test_independent_processes_keep_all_registrations(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(4)
    workers = [ctx.Process(target=_register_process, args=(str(tmp_path / 'index.json'), i, barrier)) for i in range(4)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(15)
            assert worker.exitcode == 0
        assert len(read(tmp_path / 'index.json')) == 4
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(5)


def test_concurrent_registration_preserves_every_session(tmp_path):
    path = str(tmp_path / 'index.json')
    barrier = Barrier(12)

    def register(i):
        snapshot = read(path)
        snapshot[str(i)] = {'engine': 'test', 'domain': str(i)}
        barrier.wait()
        write(path, snapshot)

    with ThreadPoolExecutor(max_workers=12) as pool:
        list(pool.map(register, range(12)))
    assert len(read(path)) == 12


def test_disjoint_fields_merge_but_conflicting_edit_is_visible(tmp_path):
    path = str(tmp_path / 'index.json')
    initial = read(path)
    initial['s'] = {'domain': 'old', 'title': 'old'}
    write(path, initial)
    a, b, c = read(path), read(path), read(path)
    a['s']['domain'] = 'new'
    b['s']['title'] = 'title'
    c['s']['domain'] = 'conflict'
    write(path, a)
    write(path, b)
    with pytest.raises(ConflictError):
        write(path, c)
    assert read(path)['s'] == {'domain': 'new', 'title': 'title'}


@pytest.mark.parametrize('raw', ['{broken', '[]', 'null'])
@pytest.mark.parametrize('reader', [read, read_value])
def test_corrupt_index_is_never_treated_as_empty(tmp_path, raw, reader):
    path = tmp_path / 'index.json'
    path.write_text(raw)
    with pytest.raises((ValueError, OSError)):
        reader(str(path))
    assert path.read_text() == raw


def test_write_error_keeps_old_data_and_retryable_snapshot(tmp_path):
    path = str(tmp_path / 'index.json')
    value = read(path)
    value['s'] = {'domain': 'old'}
    write(path, value)
    value['s']['domain'] = 'new'
    with patch('durable_json.os.replace', side_effect=OSError('disk error')):
        with pytest.raises(OSError):
            write(path, value)
    assert read(path)['s']['domain'] == 'old'
    write(path, value)
    assert read(path)['s']['domain'] == 'new'


def test_file_fsync_failure_never_replaces_index(tmp_path):
    path = tmp_path / 'index.json'
    value = read(path)
    value['s'] = {'domain': 'old'}
    write(path, value)
    value['s']['domain'] = 'new'
    with patch('durable_json.os.fsync', side_effect=OSError('file sync error')):
        with pytest.raises(OSError):
            write(path, value)
    assert read_value(path)['s']['domain'] == 'old'
    assert value.base['s']['domain'] == 'old'
    write(path, value)
    assert read_value(path)['s']['domain'] == 'new'


def test_stale_delete_does_not_remove_concurrent_update(tmp_path):
    path = str(tmp_path / 'index.json')
    value = read(path)
    value['s'] = {'domain': 'old'}
    write(path, value)
    other = read(path)
    value.pop('s')
    other['s']['domain'] = 'new'
    write(path, other)
    with pytest.raises(ConflictError):
        write(path, value)
    assert 's' in read(path)


def test_plain_dictionary_cannot_replace_unknown_snapshot(tmp_path):
    with pytest.raises(ValueError):
        write(str(tmp_path / 'index.json'), {})


def test_owned_read_values_are_independent_and_cannot_be_written(tmp_path):
    path = tmp_path / 'index.json'
    snapshot = read(path)
    snapshot['nested'] = {'items': [{'value': 'original'}]}
    write(path, snapshot)
    first, second = read_value(path), read_value(path)
    first['nested']['items'][0]['value'] = 'caller edit'
    assert second['nested']['items'][0]['value'] == 'original'
    assert read_value(path) == second
    with pytest.raises(ValueError):
        write(path, first)
    snapshot['nested']['items'].append({'value': 'persisted'})
    write(path, snapshot)
    assert read_value(path)['nested']['items'][-1]['value'] == 'persisted'
    assert len(second['nested']['items']) == 1


@pytest.mark.parametrize('reader', [read, read_value])
def test_dangling_symlink_is_not_a_missing_index(tmp_path, reader):
    path = tmp_path / 'index.json'
    try:
        path.symlink_to(tmp_path / 'absent.json')
    except OSError:
        if os.name == 'nt':
            pytest.skip('Windows link fallback cannot create a dangling symlink without privileges')
        raise
    with pytest.raises(FileNotFoundError):
        reader(path)


def test_last_good_backup_and_private_permissions(tmp_path):
    path = tmp_path / 'index.json'
    value = read(str(path))
    value['s'] = {'domain': 'old'}
    write(str(path), value)
    value['s']['domain'] = 'new'
    write(str(path), value)
    assert json.loads((tmp_path / 'index.json.previous').read_text())['s']['domain'] == 'old'
    if os.name != 'nt':  # Windows permissions are ACLs, not POSIX mode bits.
        assert path.stat().st_mode & 0o777 == 0o600


def test_save_preserves_live_row_identity_and_plain_baseline(tmp_path):
    path = str(tmp_path / 'index.json')
    value = read(path)
    row = value['s'] = {'title': 'one'}
    for title in ('one', 'two', 'three'):
        row['title'] = title
        write(path, value)
        assert value['s'] is row
        assert type(value.base) is dict
        assert read(path)['s']['title'] == title
    other = read(path)
    other['s']['domain'] = 'remote update'
    write(path, other)
    row['title'] = 'four'
    write(path, value)
    assert value['s'] is row
    assert row == {'title': 'four', 'domain': 'remote update'}


def test_disappeared_index_cannot_be_silently_recreated(tmp_path):
    path = tmp_path / 'index.json'
    value = read(path)
    value['s'] = {'title': 'one'}
    write(path, value)
    path.unlink()
    with pytest.raises(ConflictError):
        write(path, value)
    assert not path.exists()


@pytest.mark.parametrize('windows', [True, False])
def test_reader_retries_transient_replace_denial_only_on_windows(tmp_path, windows):
    # Windows CI: an unlocked reader opened the file while a writer's os.replace
    # swapped it in and got errno 13 (run 37672982426).
    import builtins
    import durable_json
    path = tmp_path / 'index.json'
    path.write_text(json.dumps({'a': 1}), encoding='utf-8')
    real_open = builtins.open
    calls = []
    def flaky_open(file, *args, **kwargs):
        if os.fspath(file) == str(path) and not calls:
            calls.append(file)
            raise PermissionError(13, 'Permission denied', str(path))
        return real_open(file, *args, **kwargs)
    with patch.object(durable_json.oscompat, 'IS_WINDOWS', windows), \
            patch.object(durable_json.time, 'sleep'), \
            patch('builtins.open', side_effect=flaky_open):
        if windows:
            assert read_value(path) == {'a': 1}
        else:
            with pytest.raises(PermissionError):
                read_value(path)
    assert len(calls) == 1


def test_reader_gives_up_after_bounded_windows_retry(tmp_path):
    import durable_json
    path = tmp_path / 'index.json'
    path.write_text(json.dumps({'a': 1}), encoding='utf-8')
    clock = iter(range(100))
    with patch.object(durable_json.oscompat, 'IS_WINDOWS', True), \
            patch.object(durable_json.time, 'sleep'), \
            patch.object(durable_json.time, 'monotonic', side_effect=lambda: next(clock)), \
            patch('builtins.open', side_effect=PermissionError(13, 'Permission denied')):
        with pytest.raises(PermissionError):
            read_value(path)
