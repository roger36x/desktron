"""Shepherd 只把开头整段命令名当命令；以“/”开头的普通正文照常发送。

前端 hsSend 与后端 command_manifest.command_token 必须同一口径：
单独的“/”、“/ 文字”、绝对路径都是正文；声明的 Desk 命令走本地入口；
其余命令形状的输入在前端提示并保留输入与附件，不进入发送队列。
"""
import json
import subprocess
import unittest

from test_frontend_recall import fn
from engine_adapters.command_manifest import command_token


CASES = {
    '/': '', '  /': '', '/ 正文': '', '/opt/a/b.txt 是什么': '', '//注释': '',
    '/中文': '', '/tmp/x': '', '/a.b': '', '/foo/': '', 'a /new': '', '': '',
    '/new': '/new', '  /task 标题': '/task', '/foo': '/foo', '/foo bar': '/foo',
    '/skill:x arg': '/skill:x', '/compact\n': '/compact', '/tmp 下有什么': '/tmp',
    # 已知边界：只有一段的绝对目录与命令形状相同，按命令拒绝，不按路径放行。
    '/tmp': '/tmp', '/Users': '/Users', '/opt 是什么': '/opt',
}


class CommandToken(unittest.TestCase):
    def test_backend_token(self):
        for text, want in CASES.items():
            with self.subTest(text=text):
                self.assertEqual(command_token(text), want)

    def test_frontend_matches_backend(self):
        functions = [fn('app-chat-composer.js', 'hsSend'),
                     fn('app-chat-input.js', 'hsSlashLookup'),
                     fn('app-chat-input.js', 'hsSlashAll'),
                     fn('app-chat-input.js', 'hsLocalSlash')]
        script = r"""
const log = [];
const rows = [
  {name:'/new', owner:'desk'}, {name:'/task', owner:'desk'},
  {name:'/interrupt', owner:'desk', control:true}];
function hsCommandRows() { return rows; }
function hsCommandHandler(name) { return () => log.push(['local', name]); }
function hsModels() { return []; } function hsEfforts() { return []; }
function hsInpText(inp) { return inp.textContent; }
function hsSlashHide() {} function hsDraftSave() {} function hsAttPaint() {}
function hsRunLocalSlash(v, c, args) { c.fn(v, args); }
function hsErrorNotice(v, msg) { log.push(['notice', msg]); }
function hsQPaint() {} function hsQIssue() {} async function hsQFlush() {}
function hsCompact() { log.push(['compact']); }
async function hsDispatch(v, text, atts) { log.push(['send', text, atts.length]); return true; }
function hsRestoreUnsent() {}
async function run(text, atts) {
  log.length = 0;
  const v = {shepherdMode:true, turnReady:true, inp:{textContent:text}, atts:atts.slice()};
  await hsSend(v);
  return {log:log.slice(), left:v.inp.textContent, atts:(v.atts || []).length};
}
(async () => {
  const out = {};
  for (const text of CASES) out[text] = await run(text, []);
  out.withImage = await run('/foo', [{data_b64:'x'}]);
  out.deskWithImage = await run('/task x', [{data_b64:'x'}]);
  out.pathWithImage = await run('/opt/a.png', [{data_b64:'x'}]);
  // 拒绝后再发普通正文仍可发送，不受上一条影响。
  out.rejectThenSend = [await run('/foo', []), await run('/', [])];
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e); process.exit(1); });
"""
        texts = [t for t in CASES if t.strip()]
        code = 'const CASES=%s;\n%s\n%s' % (json.dumps(texts, ensure_ascii=False),
                                          '\n'.join(functions), script)
        done = subprocess.run(['node', '-e', code], capture_output=True, text=True,
                              timeout=10, encoding='utf-8')
        self.assertEqual(done.returncode, 0, done.stderr)
        out = json.loads(done.stdout)
        for text in texts:
            with self.subTest(text=text):
                got = out[text]
                token = command_token(text)
                if not token:
                    self.assertEqual(got['log'], [['send', text.strip(), 0]])
                elif token in ('/new', '/task'):
                    self.assertEqual(got['log'], [['local', token]])
                else:
                    self.assertEqual(len(got['log']), 1)
                    self.assertEqual(got['log'][0][0], 'notice')
                    self.assertIn(token + ' 是斜杠命令', got['log'][0][1])
                    self.assertEqual(got['left'], text)
        for key in ('withImage', 'deskWithImage'):
            with self.subTest(case=key):
                self.assertEqual(out[key]['log'][0][0], 'notice')
                self.assertEqual(out[key]['atts'], 1)
                self.assertNotEqual(out[key]['left'], '')
        self.assertEqual(out['pathWithImage']['log'], [['send', '/opt/a.png', 1]])
        first, second = out['rejectThenSend']
        self.assertEqual(first['log'][0][0], 'notice')
        self.assertEqual(second['log'], [['send', '/', 0]])


if __name__ == '__main__':
    unittest.main()
