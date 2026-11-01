"""The backend advertises only commands executable in the resolved context."""
import unittest

from engine_adapters.command_manifest import resolve


class CommandManifest(unittest.TestCase):
    def names(self, value):
        return [row['name'] for row in value['commands']]

    def test_shepherd_never_inherits_native_command_rows(self):
        value=resolve(engine='pi',shepherd=True,caps={'compact':True,'fork':False},
                      native_commands=[{'n':'skill:test','d':'fixture'}, {'n':'compact'}])
        self.assertNotIn('/skill:test',self.names(value))
        compact=next(row for row in value['commands'] if row['name']=='/compact')
        self.assertEqual((compact['owner'],compact['transport'],compact['history']),
                         ('desk','endpoint','turn'))
        self.assertEqual(compact['result_scope'],'engine_model_checkpoint')

    def test_native_rows_are_engine_owned_and_desk_wins_collisions(self):
        value=resolve(engine='codex',shepherd=False,caps={'compact':True,'fork':True},
                      native_commands=[{'n':'goal','d':'set goal'}, {'n':'compact'}, {'n':'task'}])
        rows={row['name']:row for row in value['commands']}
        self.assertEqual(rows['/goal']['owner'],'codex')
        self.assertEqual(rows['/goal']['transport'],'native_prompt')
        self.assertFalse(rows['/goal']['portable'])
        self.assertEqual(rows['/compact']['owner'],'desk')
        self.assertEqual(rows['/task']['owner'],'desk')
        self.assertEqual(self.names(value).count('/compact'),1)

    def test_capability_gates_are_resolved_by_backend(self):
        value=resolve(engine='pi',shepherd=False,caps={'compact':False,'fork':False})
        self.assertNotIn('/compact',self.names(value))
        self.assertNotIn('/fork',self.names(value))
        enabled=resolve(engine='pi',shepherd=False,caps={'compact':True,'fork':True})
        self.assertIn('/compact',self.names(enabled));self.assertIn('/fork',self.names(enabled))

    def test_engine_specific_desk_commands_do_not_bleed(self):
        claude=set(self.names(resolve(engine='claude',shepherd=True,caps={})))
        codex=set(self.names(resolve(engine='codex',shepherd=True,caps={})))
        pi=set(self.names(resolve(engine='pi',shepherd=True,caps={})))
        self.assertIn('/login',claude);self.assertNotIn('/login',codex|pi)
        self.assertIn('/permissions',codex);self.assertNotIn('/permissions',claude|pi)
        self.assertIn('/plan',codex);self.assertNotIn('/plan',claude|pi)

    def test_invalid_native_names_are_not_advertised(self):
        value=resolve(engine='claude',shepherd=False,caps={},
                      native_commands=['ok:name','bad name','../../escape',''])
        self.assertIn('/ok:name',self.names(value))
        self.assertNotIn('/bad name',self.names(value))
        self.assertNotIn('/../../escape',self.names(value))


if __name__ == '__main__':
    unittest.main()
