"""Bounded JSON-RPC pipe transport using synthetic local children only."""
import json
import os
import queue
import sys
import threading
import time
from unittest import mock

import pytest

import codex_appserver as app
from engine_protocol import isolated_env


def connection(tmp_path, script, windows=True):
    # On Windows we also exercise the real platform branch. On POSIX force it
    # to prove that the threaded reader has no Windows-only dependencies.
    with mock.patch.object(app.oscompat, 'IS_WINDOWS', windows), \
         mock.patch.object(app, '_appserver_argv', return_value=[sys.executable, '-u', '-c', script]):
        return app._Connection(cwd=str(tmp_path), env=isolated_env(tmp_path))


def test_windows_pipe_never_uses_socket_selector(tmp_path):
    with mock.patch.object(app.selectors, 'DefaultSelector', side_effect=AssertionError('socket selector')):
        conn = connection(tmp_path, 'print(\'{"ready":true}\',flush=True)')
    try:
        assert conn.read(3) == {'ready': True}
    finally:
        conn.close()


@pytest.mark.parametrize('windows', [True, False] if os.name != 'nt' else [True])
def test_multiframe_and_partial_utf8_bytes_round_trip(tmp_path, windows):
    text = '中文与 emoji 🐑\u2028后续'
    frames = [{'id': 1, 'result': {'text': text}}, {'method': 'notification', 'params': {}},
              {'id': 2, 'result': {'done': True}}]
    raw = b'\r\n'.join(json.dumps(row, ensure_ascii=False).encode('utf-8') for row in frames) + b'\n'
    cut = raw.index('中'.encode('utf-8')) + 1
    script = ('import os,time; raw=' + repr(raw) + '; '
              f'os.write(1,raw[:{cut}]); time.sleep(.15); os.write(1,raw[{cut}:])')
    conn = connection(tmp_path, script, windows)
    try:
        assert [conn.read(3) for _ in frames] == frames
        # The child exits right after the last frame; give a loaded runner time to
        # deliver EOF once, then require it to stay immediate on the next read.
        for timeout in (5, .1):
            with pytest.raises(app.AppServerError) as error:
                conn.read(timeout)
            assert getattr(error.value, 'code', None) == 'NATIVE_EOF'
    finally:
        conn.close()


def test_partial_line_timeout_is_bounded_and_next_read_keeps_prefix(tmp_path):
    script = ('import os,sys; os.write(1,b\'{"result":\'); '
              'sys.stdin.readline(); os.write(1,b\'{"ok":true}}\\n\')')
    conn = connection(tmp_path, script)
    try:
        # Wait for the synthetic child to start and publish a partial frame.
        deadline = time.monotonic() + 3
        while not conn._stdout_queue.qsize() and time.monotonic() < deadline:
            time.sleep(.01)
        started = time.monotonic()
        with pytest.raises(app.AppServerTimeout):
            conn.read(.1)
        assert time.monotonic() - started < .8
        conn.notify('continue')
        assert conn.read(3) == {'result': {'ok': True}}
    finally:
        conn.close()


@pytest.mark.parametrize('raw', [b'not-json\n', b'{"text":"\xff"}\n'])
def test_malformed_protocol_bytes_fail_strictly(tmp_path, raw):
    conn = connection(tmp_path, 'import os; os.write(1,' + repr(raw) + ')')
    try:
        with pytest.raises(app.AppServerError, match='无效 JSON'):
            conn.read(3)
    finally:
        conn.close()


def test_partial_frame_at_eof_is_not_a_terminal(tmp_path):
    conn = connection(tmp_path, 'import os; os.write(1,b\'{"result":\')')
    try:
        with pytest.raises(app.AppServerError) as error:
            conn.read(3)
        assert error.value.code == 'NATIVE_EOF'
    finally:
        conn.close()


def test_reader_io_failure_is_sticky_and_does_not_expose_native_details():
    conn = object.__new__(app._Connection)
    conn.proc = mock.Mock()
    conn._stdout_queue = queue.Queue(maxsize=8)
    conn._stdout_stopped = threading.Event()
    conn._stdout_failed = False
    conn._stdout_eof = False
    with mock.patch.object(app.os, 'read', side_effect=OSError('synthetic-sensitive-details')):
        conn._drain_stdout()
    for _ in range(2):
        with pytest.raises(app.AppServerError, match='输出通道读取失败') as error:
            conn._read_chunk(.1)
        assert 'synthetic-sensitive-details' not in str(error.value)


def test_close_releases_idle_reader_and_is_idempotent(tmp_path):
    conn = connection(tmp_path, 'import time; time.sleep(60)')
    conn.close(); conn.close()
    assert not conn._stdout_thread.is_alive()
    assert all(stream.closed for stream in (conn.proc.stdin, conn.proc.stdout, conn.proc.stderr))


def test_close_wakes_concurrent_long_protocol_wait(tmp_path):
    conn = connection(tmp_path, 'import time; time.sleep(60)')
    failures = []
    entered = threading.Event()

    def read():
        entered.set()
        try:
            conn.read(timeout=30)
        except app.AppServerError as error:
            failures.append(error)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    assert entered.wait(1)
    conn.close()
    reader.join(timeout=.5)
    assert not reader.is_alive()
    assert len(failures) == 1 and failures[0].code == 'NATIVE_EOF'


def test_full_queue_is_bounded_and_does_not_block_close(tmp_path):
    conn = connection(tmp_path, 'import os; block=b"x"*65536\nwhile True: os.write(1,block)')
    try:
        deadline = time.monotonic() + 3
        while not conn._stdout_queue.full() and time.monotonic() < deadline:
            time.sleep(.01)
        assert conn._stdout_queue.full()
        assert conn._stdout_queue.qsize() == conn._stdout_queue.maxsize == 8
    finally:
        conn.close()
    assert not conn._stdout_thread.is_alive()


def test_catalog_fetch_uses_same_rpc_transport_without_turn_requests(tmp_path):
    script = '''
import json,sys
for line in sys.stdin:
 row=json.loads(line); method=row['method']
 if method=='initialized': continue
 if method=='initialize': result={}
 elif method=='model/list': result={'data':[{'id':'fixture','model':'fixture'}]}
 elif method=='collaborationMode/list': result={'data':[{'mode':'default'}]}
 else: raise RuntimeError('Unexpected method; no turns allowed')
 print(json.dumps({'id':row['id'],'result':result}),flush=True)
'''
    conn = connection(tmp_path, script)
    try:
        conn.initialize(overall=3)
        with mock.patch.object(app, '_catalog_save_disk'), mock.patch.object(app, '_CATALOGS', {}):
            catalog = app._catalog_fetch(conn=conn)
        assert catalog == {'models': [{'id': 'fixture', 'model': 'fixture'}],
                           'modes': [{'mode': 'default'}]}
    finally:
        conn.close()
