import json
import os
from pathlib import Path
import subprocess
import unittest
from unittest import mock

from shepherd import resident_process as resident

ROOT=Path(__file__).resolve().parents[2]


class ClaudeMemoryStore(unittest.TestCase):
    def test_claude_autocompact_tier_reaches_resident_and_oneshot_sdk(self):
        result=subprocess.run(['node',str(ROOT/'desktron/tests/shepherd/claude-sdk-options.test.mjs')],
                              capture_output=True,text=True,timeout=30, encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])
        resident=(ROOT/'desktron/lib/shepherd/claude_resident_runner.mjs').read_text(encoding='utf-8')
        oneshot=(ROOT/'desktron/lib/shepherd/claude_runner.mjs').read_text(encoding='utf-8')
        runner=(ROOT/'desktron/lib/shepherd/unified_runners.py').read_text(encoding='utf-8')
        self.assertIn('claudeExtraArgs(bootstrap.thinkingDisplay,bootstrap.autocompact)',resident)
        self.assertIn('claudeExtraArgs(payload.thinkingDisplay,payload.autocompact)',oneshot)
        self.assertGreaterEqual(runner.count("'autocompact':self.options.get('autocompact','')"),3)
        self.assertIn("checkpoint=self.checkpoint is not None",runner)
        self.assertIn("engine=self.connector.engine",runner)

    def test_resident_turn_avoids_whole_history_load(self):
        resident=(ROOT/'desktron/lib/shepherd/claude_resident_runner.mjs').read_text(encoding='utf-8')
        prepare=resident[resident.index('async function prepareTurn'):resident.index('await unifiedBootstrap')]
        settle=resident[resident.index('async function settle'):resident.index('async function receiveNative')]
        self.assertNotIn('store.load(',prepare)
        self.assertNotIn('store.load(',settle)
        self.assertIn('store.size()',prepare)
        self.assertIn('store.slice(current.beforeSize)',settle)

    def test_store_semantics_and_revision_match_previous_identity(self):
        result=subprocess.run(['node',str(ROOT/'desktron/tests/shepherd/claude-memory-store.test.mjs')],
                              capture_output=True,text=True,timeout=30, encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertTrue(json.loads(result.stdout)['ok'])

    def test_large_store_operations_do_not_leave_history_sized_temporary_heap(self):
        result=subprocess.run(['node','--expose-gc',
            str(ROOT/'desktron/tests/benchmark_claude_memory_store.mjs'),'2500','2048'],
            capture_output=True,text=True,timeout=30, encoding='utf-8')
        self.assertEqual(result.returncode,0,result.stderr)
        report=json.loads(result.stdout)
        self.assertEqual(report['size'],2500)
        self.assertLess(report['post_ops_heap_mb'],2.0,report)


class ResidentRotation(unittest.TestCase):
    def setUp(self):
        with resident._LOCK:
            self.old=resident._HOSTS;resident._HOSTS={}
        self.old_limit=resident._SOFT_RSS_MB;resident._SOFT_RSS_MB=384
        self.addCleanup(self.restore)

    def restore(self):
        for key in list(resident._HOSTS):resident.drop(key)
        with resident._LOCK:resident._HOSTS=self.old
        resident._SOFT_RSS_MB=self.old_limit

    def host(self,pid=991):
        class Proc:
            def __init__(self):self.pid=pid;self.dead=False
            def poll(self):return 0 if self.dead else None
        class Host:
            def __init__(self):self.proc=Proc();self.closed=False
            def close(self):self.closed=True;self.proc.dead=True
        host=Host();got,_=resident.acquire('binding','key',0,lambda:host)
        self.assertIs(got,host);self.assertTrue(resident.release('binding',host,1))
        return host

    def test_policy_is_observable(self):
        got=resident.policy()
        self.assertEqual(got['claude_soft_rss_mb'],resident._SOFT_RSS_MB)
        self.assertEqual(got['idle_seconds'],resident._IDLE)
        self.assertEqual(got['max_hosts'],resident._MAX)

    def test_zero_environment_value_disables_soft_rss_rotation(self):
        with mock.patch.dict(os.environ, {'DESKTRON_SHEPHERD_HOST_RSS_MB':'0'}):
            self.assertEqual(resident._soft_rss_mb(),0)

    def test_checkpoint_rotates_committed_idle_cache(self):
        host=self.host()
        self.assertTrue(resident.rotate_after_commit('binding',host,engine='pi',checkpoint=True))
        self.assertTrue(host.closed);self.assertEqual(resident.roster(),[])

    def test_only_claude_rotates_at_soft_rss_limit(self):
        host=self.host()
        with mock.patch('resident.tree_rss_bytes',return_value=385*(1<<20)) as rss:
            self.assertTrue(resident.rotate_after_commit('binding',host,engine='claude'))
        rss.assert_called_once_with(host.proc.pid,fresh=True)
        self.assertTrue(host.closed)
        other=self.host(992)
        with mock.patch('resident.tree_rss_bytes',return_value=999*(1<<20)):
            self.assertFalse(resident.rotate_after_commit('binding',other,engine='pi'))
        self.assertFalse(other.closed)

    def test_unknown_rss_never_forces_eviction(self):
        host=self.host()
        with mock.patch('resident.tree_rss_bytes',return_value=0):
            self.assertFalse(resident.rotate_after_commit('binding',host,engine='claude'))
        self.assertFalse(host.closed)

    def test_rotation_cleanup_failure_fences_second_writer_without_failing_caller(self):
        host=self.host()
        host.close=mock.Mock(side_effect=OSError('cleanup failed'))
        self.assertFalse(resident.rotate_after_commit('binding',host,checkpoint=True))
        self.assertEqual(len(resident.roster()),1)
        self.assertTrue(resident.roster()[0][2])
        with self.assertRaisesRegex(Exception,'禁止并发 Writer'):
            resident.acquire('binding','new',1,lambda:self.host(992))


if __name__=='__main__':unittest.main()
