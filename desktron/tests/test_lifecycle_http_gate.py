"""All local mutation transports share the lifecycle admission gate."""
from pathlib import Path
import sys
import unittest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'desktron/lib'))
import lifecycle


class Gate(unittest.TestCase):
    def setUp(self):
        lifecycle._STATE='running';lifecycle._OPERATION=''
        lifecycle._ACTIVE=0;lifecycle._CRITICAL.clear()

    def tearDown(self):
        lifecycle._STATE='running';lifecycle._OPERATION=''
        lifecycle._ACTIVE=0;lifecycle._CRITICAL.clear()

    def test_stopping_rejects_new_write_registration(self):
        lifecycle._STATE='stopping';lifecycle._OPERATION='a'*32
        self.assertFalse(lifecycle.begin(critical='http_write'))
        self.assertEqual(lifecycle.status()['blockers'],[])

    def test_running_write_is_counted_until_finish(self):
        self.assertTrue(lifecycle.begin(critical='http_write'))
        self.assertEqual(lifecycle.status()['blockers'],[{'kind':'http_write','count':1}])
        lifecycle.finish(critical='http_write')
        self.assertEqual(lifecycle.status()['blockers'],[])

    def test_main_and_unix_mutation_transports_use_same_gate(self):
        source=(ROOT/'desktron/lib/ui_server.py').read_text(encoding='utf-8')
        segment=source[source.index('    def _local_mutation'):source.index('    def log_message')]
        self.assertIn('lifecycle.begin(critical="http_write")',segment)
        self.assertIn('"/workbench/restart"',segment)
        self.assertIn('"/workbench/update/apply"',segment)
        self.assertIn('"/workbench/update/dev-app-apply"',segment)
        self.assertIn('"/workbench/app-control/host/result"',segment)
        unix=source[source.index('class AppControlUnixHandler'):source.index('class AppControlUnixServer')]
        self.assertIn('return self._local_mutation("POST")',unix)
        self.assertIn('return self._local_mutation("GET")',unix)
        get=source[source.index('    def do_GET(self):'):source.index('    def do_POST(self):')]
        self.assertIn('/workbench/app-control/host/next',get)
        self.assertIn('_local_mutation("GET")',get)

    def test_unclassified_workbench_write_cannot_bypass_dispatcher(self):
        source=(ROOT/'desktron/lib/ui_server.py').read_text(encoding='utf-8')
        for method in ('POST','PUT','DELETE'):
            body=source[source.index('    def do_'+method):]
            body=body[:body.index('\n    def ',10) if '\n    def ' in body[10:] else len(body)]
            self.assertIn('_local_mutation("'+method+'")',body)


if __name__=='__main__':unittest.main()
