"""Pairwise tool-call identity contracts for Shepherd engine switching."""
import copy
from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron' / 'lib'))

from shepherd.claude_codec import encode_rounds as claude_encode
from shepherd.codex_codec import encode_round as codex_encode
from shepherd.pi_codec import encode_round as pi_encode


SOURCE_IDS = {
    'claude': 'toolu_' + 'a' * 24,
    'pi': ('call_3D7bUZ7eLFrQFWgz0UEJ4k4r|'
           'fc_0af2bfaeb884fe00016aaa93b4c9b087d286275f76ef625fe1'),
    'codex': 'call_' + 'c' * 24,
}
TARGET_LIMITS = {'claude': 30, 'pi': 40, 'codex': 64}


class PairwiseToolCallIds(unittest.TestCase):
    def public(self, call_id):
        return [
            {'type':'function_call','call_id':call_id,'name':'read','arguments':'{}'},
            {'type':'function_call_output','call_id':call_id,'output':'ok'},
        ]

    def encode(self, target, public):
        row={'round_id':'conversation:1','offset':1,'entry_hash':'a'*64,
             'committed_at':10,'public':public}
        if target == 'claude':
            entries=claude_encode({'binding_id':'binding','native_session_id':'session',
                'native_cwd':'/tmp'},[row])
            return (entries[0]['message']['content'][0]['id'],
                    entries[1]['message']['content'][0]['tool_use_id'])
        if target == 'pi':
            messages=pi_encode(row)
            call=next(block for message in messages if message.get('role')=='assistant'
                      for block in message.get('content') or [] if block.get('type')=='toolCall')
            result=next(message for message in messages if message.get('role')=='toolResult')
            return call['id'],result['toolCallId']
        items=codex_encode(row)
        return items[0]['call_id'],items[1]['call_id']

    def test_all_source_target_pairs_are_stable_safe_and_paired(self):
        for source,source_id in SOURCE_IDS.items():
            for target in TARGET_LIMITS:
                with self.subTest(source=source,target=target):
                    public=self.public(source_id);original=copy.deepcopy(public)
                    first=self.encode(target,public);second=self.encode(target,public)
                    self.assertEqual(first,second)
                    self.assertEqual(first[0],first[1])
                    self.assertLessEqual(len(first[0]),TARGET_LIMITS[target])
                    self.assertTrue(re.fullmatch(r'[A-Za-z0-9_-]+',first[0]))
                    self.assertEqual(public,original)


if __name__ == '__main__':
    unittest.main()
