import os
import plistlib
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktron/lib'))

import macos_codesign as signing


class MacOSCodeSigning(unittest.TestCase):
    def test_identity_selection_prefers_single_developer_id_then_local_fallback(self):
        developer = {'hash':'A' * 40, 'name':'Developer ID Application: Fixture'}
        local = {'hash':'B' * 40, 'name':signing.LOCAL_IDENTITY_LABEL}
        with mock.patch.object(signing, 'code_signing_identities', return_value=[developer, local]):
            self.assertEqual(signing.select_identity({}), developer['hash'])
            self.assertEqual(signing.select_identity(
                {'DESKTRON_CODESIGN_IDENTITY':local['hash']}), local['hash'])
        with mock.patch.object(signing, 'code_signing_identities', return_value=[local]):
            self.assertEqual(signing.select_identity({}), local['hash'])

    def test_ambiguous_or_unknown_identity_fails_closed(self):
        rows = [
            {'hash':'A' * 40, 'name':'Developer ID Application: One'},
            {'hash':'B' * 40, 'name':'Developer ID Application: Two'},
        ]
        with mock.patch.object(signing, 'code_signing_identities', return_value=rows):
            with self.assertRaisesRegex(signing.CodeSignError, '多个 Developer ID'):
                signing.select_identity({})
            with self.assertRaisesRegex(signing.CodeSignError, '未唯一匹配'):
                signing.select_identity({'DESKTRON_CODESIGN_IDENTITY':'missing'})

    def test_designated_requirement_must_use_certificate_not_cdhash(self):
        stable = ('designated => identifier "io.github.desktron" and anchor apple generic '
                  'and certificate leaf[subject.CN] = "Fixture"')
        self.assertTrue(signing.stable_requirement(stable))
        self.assertFalse(signing.stable_requirement(
            'designated => cdhash H"0123" and identifier "io.github.desktron"'))
        self.assertFalse(signing.stable_requirement(
            'designated => identifier "com.example.other" and anchor apple'))
        actual_adhoc = '# designated => cdhash H"0123" and identifier "io.github.desktron"'
        with mock.patch.object(signing, '_run', return_value=
                subprocess.CompletedProcess([], 0, '', actual_adhoc)):
            self.assertEqual(signing.signature_requirement('/fixture'), actual_adhoc[2:])

    def test_verify_checks_bundle_identifier_integrity_and_requirement(self):
        with tempfile.TemporaryDirectory() as folder:
            app = Path(folder) / 'Desktron.app'
            contents = app / 'Contents'; contents.mkdir(parents=True)
            with (contents / 'Info.plist').open('wb') as stream:
                plistlib.dump({'CFBundleIdentifier':signing.BUNDLE_ID}, stream)
            stable = ('designated => identifier "io.github.desktron" and anchor apple generic '
                      'and certificate leaf[subject.CN] = "Fixture"')
            responses = [
                subprocess.CompletedProcess([], 0, '', 'Authority=Fixture\nSignature size=1234'),
                subprocess.CompletedProcess([], 0, '', ''),
                subprocess.CompletedProcess([], 0, '', stable),
            ]
            with mock.patch.object(signing, '_run', side_effect=responses):
                self.assertEqual(signing.verify_stable_app(app), stable)
            custom_adhoc = subprocess.CompletedProcess(
                [], 0, '', 'Signature=adhoc\n# designated => identifier "io.github.desktron" '
                'and certificate leaf[subject.CN] = "Fake"')
            with mock.patch.object(signing, '_run', return_value=custom_adhoc):
                with self.assertRaisesRegex(signing.CodeSignError, '拒绝 ad-hoc'):
                    signing.verify_stable_app(app)

    def test_compatible_app_preserves_stable_identity_and_allows_proven_legacy(self):
        with mock.patch.object(signing.os.path, 'exists', return_value=True), \
             mock.patch.object(signing, 'verify_stable_app', return_value='same'), \
             mock.patch.object(signing, 'installed_requirement_or_legacy', return_value='same'):
            self.assertEqual(signing.verify_compatible_app('/candidate','/current'), 'same')
        with mock.patch.object(signing.os.path, 'exists', return_value=True), \
             mock.patch.object(signing, 'verify_stable_app', return_value='new'), \
             mock.patch.object(signing, 'installed_requirement_or_legacy', return_value='old'):
            with self.assertRaisesRegex(signing.CodeSignError, '身份.*不一致'):
                signing.verify_compatible_app('/candidate','/current')
        with mock.patch.object(signing.os.path, 'exists', return_value=True), \
             mock.patch.object(signing, 'verify_stable_app', return_value='stable'), \
             mock.patch.object(signing, 'installed_requirement_or_legacy', return_value=''):
            self.assertEqual(signing.verify_compatible_app('/candidate','/current'), 'stable')
        with mock.patch.object(signing.os.path, 'exists', return_value=True), \
             mock.patch.object(signing, 'verify_stable_app', return_value='stable'), \
             mock.patch.object(signing, 'installed_requirement_or_legacy',
                               side_effect=signing.CodeSignError('codesign timeout')):
            with self.assertRaisesRegex(signing.CodeSignError, 'timeout'):
                signing.verify_compatible_app('/candidate','/current')

    def test_recorded_requirement_blocks_a_second_legacy_migration(self):
        stable = ('designated => identifier "io.github.desktron" and anchor apple generic '
                  'and certificate leaf[subject.CN] = "Fixture"')
        other = ('designated => identifier "io.github.desktron" and anchor apple generic '
                 'and certificate leaf[subject.CN] = "Other"')
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); marker = root / 'requirement'; current = root / 'current.app'
            current.mkdir(); marker.write_text(stable + '\n', encoding='utf-8')
            with mock.patch.object(signing, 'verify_stable_app', return_value=stable), \
                 mock.patch.object(signing, 'installed_requirement_or_legacy', return_value=''):
                with self.assertRaisesRegex(signing.CodeSignError, '已记录'):
                    signing.verify_compatible_app('/candidate', current, marker)
            with mock.patch.object(signing, 'verify_stable_app', return_value=stable), \
                 mock.patch.object(signing, 'installed_requirement_or_legacy', return_value=stable):
                self.assertEqual(signing.verify_compatible_app('/candidate', current, marker), stable)
            with mock.patch.object(signing, 'verify_stable_app', return_value=other), \
                 mock.patch.object(signing, 'installed_requirement_or_legacy', return_value=stable):
                with self.assertRaisesRegex(signing.CodeSignError, '候选.*已记录'):
                    signing.verify_compatible_app('/candidate', current, marker)

    def test_record_requirement_is_atomic_and_never_overwrites_another_identity(self):
        stable = ('designated => identifier "io.github.desktron" and anchor apple generic '
                  'and certificate leaf[subject.CN] = "Fixture"')
        other = ('designated => identifier "io.github.desktron" and anchor apple generic '
                 'and certificate leaf[subject.CN] = "Other"')
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / 'identity' / 'requirement'
            with mock.patch.object(signing, 'verify_stable_app', return_value=stable):
                self.assertEqual(signing.record_requirement('/candidate', marker), stable)
            self.assertEqual(marker.read_text(encoding='utf-8').strip(), stable)
            with mock.patch.object(signing, 'verify_stable_app', return_value=other):
                with self.assertRaisesRegex(signing.CodeSignError, '拒绝覆盖'):
                    signing.record_requirement('/candidate', marker)
            self.assertEqual(marker.read_text(encoding='utf-8').strip(), stable)

    def test_installed_requirement_classifies_only_proven_unsigned_adhoc_or_stable(self):
        stable = ('designated => identifier "io.github.desktron" and anchor apple generic '
                  'and certificate leaf[subject.CN] = "Fixture"')
        unsigned = subprocess.CompletedProcess([],1,'','code object is not signed at all')
        adhoc_variants = (
            'Executable=/fixture\nSignature=adhoc\nTeamIdentifier=not set',
            'Executable=/fixture\nSignature=adhoc\nTeamIdentifier=not set\n'
            'code has no resources but signature indicates they must be present',
            'Executable=/fixture\nSignature=adhoc\nTeamIdentifier=not set\n'
            'a sealed resource is missing or invalid',
        )
        with mock.patch.object(signing, '_bundle_identifier', return_value=signing.BUNDLE_ID), \
             mock.patch.object(signing, '_run', return_value=unsigned):
            self.assertEqual(signing.installed_requirement_or_legacy('/current'), '')
        for metadata in adhoc_variants:
            with self.subTest(metadata=metadata.splitlines()[-1]), \
                 mock.patch.object(signing, '_bundle_identifier', return_value=signing.BUNDLE_ID), \
                 mock.patch.object(signing, '_run', return_value=
                     subprocess.CompletedProcess([],0,'',metadata)):
                self.assertEqual(signing.installed_requirement_or_legacy('/current'), '')
        with mock.patch.object(signing, '_bundle_identifier', return_value=signing.BUNDLE_ID), \
             mock.patch.object(signing, 'signature_requirement', return_value=stable), \
             mock.patch.object(signing, '_run', side_effect=[
                 subprocess.CompletedProcess([],0,'','Authority=Fixture\nSignature size=1234'),
                 subprocess.CompletedProcess([],0,'','')]):
            self.assertEqual(signing.installed_requirement_or_legacy('/current'), stable)
        with mock.patch.object(signing, '_bundle_identifier', return_value=signing.BUNDLE_ID), \
             mock.patch.object(signing, '_run', side_effect=[
                 subprocess.CompletedProcess([],0,'','Authority=Fixture\nSignature size=1234'),
                 subprocess.CompletedProcess([],1,'','a sealed resource is missing or invalid')]):
            with self.assertRaisesRegex(signing.CodeSignError, '稳定应用签名'):
                signing.installed_requirement_or_legacy('/current')
        with mock.patch.object(signing, '_bundle_identifier', return_value=signing.BUNDLE_ID), \
             mock.patch.object(signing, 'signature_requirement', return_value=
                 'designated => cdhash H"0123" and identifier "io.github.desktron"'), \
             mock.patch.object(signing, '_run', side_effect=[
                 subprocess.CompletedProcess([],0,'','Authority=Fixture\nSignature size=1234'),
                 subprocess.CompletedProcess([],0,'','')]):
            with self.assertRaisesRegex(signing.CodeSignError, '指定要求'):
                signing.installed_requirement_or_legacy('/current')
        with mock.patch.object(signing, '_bundle_identifier', return_value=signing.BUNDLE_ID), \
             mock.patch.object(signing, '_run', side_effect=signing.CodeSignError('timeout')):
            with self.assertRaisesRegex(signing.CodeSignError, 'timeout'):
                signing.installed_requirement_or_legacy('/current')
        with mock.patch.object(signing, '_bundle_identifier',
                               side_effect=signing.CodeSignError('plist unreadable')):
            with self.assertRaisesRegex(signing.CodeSignError, 'plist unreadable'):
                signing.installed_requirement_or_legacy('/current')

    @unittest.skipUnless(sys.platform == 'darwin' and Path('/usr/bin/cc').exists()
                         and Path('/usr/bin/codesign').exists(), 'requires real macOS codesign')
    def test_real_adhoc_bundle_matches_codesign_output_and_is_migratable(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); app = root / 'Desktron.app'; contents = app / 'Contents'
            binary = contents / 'MacOS' / 'desktron'; binary.parent.mkdir(parents=True)
            with (contents / 'Info.plist').open('wb') as stream:
                plistlib.dump({'CFBundleIdentifier':signing.BUNDLE_ID,
                               'CFBundleExecutable':'desktron',
                               'CFBundlePackageType':'APPL'}, stream)
            source = root / 'main.c'; source.write_text('int main(void){return 0;}\n', encoding='utf-8')
            subprocess.run(['/usr/bin/cc', str(source), '-o', str(binary)], check=True,
                           capture_output=True, text=True, encoding='utf-8')
            subprocess.run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-',
                            '--identifier', signing.BUNDLE_ID, str(app)], check=True,
                           capture_output=True, text=True, encoding='utf-8')
            requirement = signing.signature_requirement(app)
            self.assertTrue(requirement.startswith('designated =>'))
            self.assertIn('cdhash', requirement)
            self.assertEqual(signing.installed_requirement_or_legacy(app), '')

    def test_sign_uses_selected_identity_then_verifies(self):
        with mock.patch.object(signing.sys, 'platform', 'darwin'), \
             mock.patch.object(signing, 'select_identity', return_value='A' * 40), \
             mock.patch.object(signing, '_run') as run, \
             mock.patch.object(signing, 'verify_stable_app', return_value='stable') as verify:
            self.assertEqual(signing.sign_app('/tmp/Desktron.app'), 'stable')
        command = run.call_args.args[0]
        self.assertEqual(command[:4], ['/usr/bin/codesign','--force','--deep','--sign'])
        self.assertIn('--timestamp=none', command)
        verify.assert_called_once_with(os.path.realpath('/tmp/Desktron.app'))

    def test_app_install_signs_before_activation_submission(self):
        script = (ROOT / 'desktron/bin/desktron').read_text(encoding='utf-8')
        sign = 'macos_codesign.py" sign "$APPDIR"'
        activate = 'dev_app_update.py" activate --stage'
        self.assertIn(sign, script)
        self.assertLess(script.index(sign), script.index(activate))


if __name__ == '__main__':
    unittest.main()
