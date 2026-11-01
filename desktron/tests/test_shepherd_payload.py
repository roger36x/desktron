"""Shepherd runtime must survive the actual release rsync filters."""
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class Payload(unittest.TestCase):
    @unittest.skipUnless(shutil.which('rsync'), 'rsync required')
    def test_shepherd_runtime_and_pinned_sdk_lock_are_distributed(self):
        paths = subprocess.check_output(['git','ls-files','--cached','--others','--exclude-standard',
            'desktron/lib/shepherd','desktron/lib/engine_adapters/shepherd.py',
            'desktron/lib/engine_adapters/shepherd_recap.py',
            'desktron/lib/owned_process.py','desktron/bin/shepherd'], cwd=ROOT, text=True).splitlines()
        paths = sorted(set(path for path in paths if (ROOT/path).is_file()))
        with tempfile.TemporaryDirectory() as folder:
            tmp = Path(folder); listing = tmp/'paths'; listing.write_text('\n'.join(paths)+'\n')
            raw = tmp/'raw'; raw.mkdir(); payload = tmp/'payload'; payload.mkdir()
            subprocess.run(['rsync','-a',f'--files-from={listing}',str(ROOT)+'/',str(raw)+'/'],check=True)
            subprocess.run(['rsync','-a',f'--exclude-from={ROOT / "scripts/payload-excludes.txt"}',
                            str(raw)+'/',str(payload)+'/'],check=True)
            for relative in ('desktron/lib/shepherd/runners.py',
                             'desktron/lib/shepherd/recap.py',
                             'desktron/lib/shepherd/claude_recap.mjs',
                             'desktron/lib/engine_adapters/shepherd_recap.py',
                             'desktron/lib/shepherd/image_content.py',
                             'desktron/lib/shepherd/requests.py',
                             'desktron/lib/shepherd/runner_process.py',
                             'desktron/lib/shepherd/claude_runner.mjs',
                             'desktron/lib/shepherd/claude_sdk_options.mjs',
                             'desktron/lib/shepherd/claude_sqlite_store.mjs',
                             'desktron/lib/shepherd/pi_bridge.mjs',
                             'desktron/lib/shepherd/runtime/claude-agent-sdk-package-lock.json',
                             'desktron/lib/engine_adapters/shepherd.py',
                             'desktron/lib/owned_process.py',
                             'desktron/bin/shepherd'):
                self.assertTrue((payload/relative).is_file(), relative)
                self.assertEqual((payload/relative).read_bytes(), (ROOT/relative).read_bytes())


if __name__ == '__main__': unittest.main()
