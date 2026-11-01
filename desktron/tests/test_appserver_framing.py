"""真实 pipe 测 JSONL 帧读取，不启动引擎。"""
import os
import queue
import selectors
import threading
import time
import types
import unittest

from test_codex_recall import app


class Framing(unittest.TestCase):
    def setUp(self):
        read_fd, self.write_fd = os.pipe()
        self.reader = os.fdopen(read_fd, "r")
        self.conn = app._Connection.__new__(app._Connection)
        self.conn.proc = types.SimpleNamespace(stdout=self.reader, poll=lambda: 0)
        self.conn._stderr = []
        self.conn._read_buffer = bytearray()
        self.conn._selector = None
        self.conn._stdout_queue = None
        self.conn._stdout_thread = None
        self.conn._stdout_stopped = threading.Event()
        self.conn._stdout_eof = False
        self.conn._stdout_failed = False
        if app.oscompat.IS_WINDOWS:
            self.conn._stdout_queue = queue.Queue(maxsize=8)
            self.conn._stdout_thread = threading.Thread(target=self.conn._drain_stdout, daemon=True)
            self.conn._stdout_thread.start()
        else:
            self.conn._selector = selectors.DefaultSelector()
            self.conn._selector.register(self.reader, selectors.EVENT_READ)

    def tearDown(self):
        self.conn._stdout_stopped.set()
        if self.write_fd is not None:
            os.close(self.write_fd)
        if self.conn._stdout_thread is not None:
            self.conn._stdout_thread.join(timeout=2)
            self.assertFalse(self.conn._stdout_thread.is_alive(), 'owned pipe reader must exit')
        if self.conn._selector is not None:
            self.conn._selector.close()
        self.reader.close()

    def test_coalesced_frames_are_read_without_waiting_for_new_bytes(self):
        os.write(self.write_fd, b'{"method":"notice"}\n{"id":1,"result":{}}\n')
        self.assertEqual(self.conn.read(.1)["method"], "notice")
        self.assertEqual(self.conn.read(.1)["id"], 1)

    def test_partial_frame_times_out_and_can_complete_later(self):
        os.write(self.write_fd, b'{"id":')
        started = time.monotonic()
        with self.assertRaises(app.AppServerTimeout):
            self.conn.read(.03)
        self.assertLess(time.monotonic() - started, .5)
        os.write(self.write_fd, b'2}\n')
        self.assertEqual(self.conn.read(.1), {"id": 2})

    def test_utf8_character_split_across_chunks(self):
        data = '{"text":"撤回"}\n'.encode()
        cut = data.index('撤'.encode()) + 1
        os.write(self.write_fd, data[:cut])
        with self.assertRaises(app.AppServerTimeout):
            self.conn.read(.01)
        os.write(self.write_fd, data[cut:])
        self.assertEqual(self.conn.read(.1), {"text": "撤回"})

    def test_eof_after_buffered_frame(self):
        os.write(self.write_fd, b'{"id":1}\n')
        os.close(self.write_fd)
        self.write_fd = None
        self.assertEqual(self.conn.read(.1), {"id": 1})
        with self.assertRaises(app.AppServerError) as error:
            self.conn.read(.1)
        self.assertEqual(error.exception.code, 'NATIVE_EOF')
        self.assertEqual(error.exception.returncode, 0)

    def test_invalid_json_does_not_leak_frame_contents(self):
        os.write(self.write_fd, b'secret-not-json\n')
        with self.assertRaises(app.AppServerError) as error:
            self.conn.read(.1)
        self.assertNotIn('secret', str(error.exception))
