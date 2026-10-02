import importlib.util
import json
import os
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci', ROOT / 'scripts/ci.py')
ci = importlib.util.module_from_spec(spec); spec.loader.exec_module(ci)


class SnapshotAndArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='jenkins-ci-test-')
        self.root = pathlib.Path(self.temp.name)
        self.repo = self.root / 'repo'; self.repo.mkdir()
        self.snapshots = self.root / 'snapshots'; self.snapshots.mkdir()
        self.releases = self.root / 'releases'; self.releases.mkdir()
        ci.git(['init', str(self.repo)], self.root)
        ci.git(['config', 'user.name', 'Local Test'], self.repo)
        ci.git(['config', 'user.email', 'test@example.invalid'], self.repo)
        (self.repo / 'message.json').write_text('{"message":"hello","version":"1.0.0"}')
        ci.git(['add', 'message.json'], self.repo)
        ci.git(['commit', '-m', 'demo fixture'], self.repo)
        self.head = ci.git(['rev-parse', 'HEAD'], self.repo)
        # Dirty user data must never enter the source snapshot.
        (self.repo / 'message.json').write_text('{"message":"dirty secret","version":"9.9.9"}')
        self.snapshot = ci.ingest(self.repo, 'demo', self.head, self.head, self.snapshots, 'demo-fixture')
        self.metadata_hash = ci.digest(self.snapshot / 'snapshot.json')
        self.work = self.root / 'work'

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self):
        ci.prepare(self.snapshots, 'demo-fixture', 'demo', self.metadata_hash, self.work)

    def test_build_test_promote_same_bytes_and_preserve_dirty_source(self):
        self.prepare()
        self.assertIn('dirty secret', (self.repo / 'message.json').read_text())
        result = ci.run(self.work, 'demo')
        self.assertEqual(ci.read_json(self.work / result['artifact'])['message'], 'HELLO')
        reviewed = ci.digest(self.work / 'result.json')
        destination = ci.promote(self.work, 'demo', self.releases, reviewed, 'local-admin')
        self.assertEqual(ci.digest(destination / result['artifact']), result['artifactSha256'])
        self.assertEqual(ci.read_json(destination / 'receipt.json')['reviewedResultSha256'], reviewed)
        with self.assertRaisesRegex(ValueError, 'already exists'):
            ci.promote(self.work, 'demo', self.releases, reviewed, 'local-admin')

    def test_mutated_bundle_rejected(self):
        with (self.snapshot / 'source.bundle').open('ab') as f:
            f.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'bundle checksum'):
            self.prepare()
        self.assertFalse(self.work.exists())

    def test_mutated_metadata_rejected(self):
        m = ci.read_json(self.snapshot / 'snapshot.json'); m['base'] = '0' * 40
        ci.write_json(self.snapshot / 'snapshot.json', m)
        with self.assertRaisesRegex(ValueError, 'metadata checksum'):
            self.prepare()

    def test_candidate_drift_rejected(self):
        self.prepare()
        (self.work / 'source/message.json').write_text('{"message":"changed"}')
        with self.assertRaisesRegex(ValueError, 'Tracked source drift'):
            ci.run(self.work, 'demo')

    def test_changed_tested_artifact_cannot_promote(self):
        self.prepare(); result = ci.run(self.work, 'demo')
        reviewed = ci.digest(self.work / 'result.json')
        (self.work / result['artifact']).write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed after testing'):
            ci.promote(self.work, 'demo', self.releases, reviewed, 'local-admin')
        self.assertFalse((self.releases / '.promotion-lock').exists())

    def test_result_changed_during_review_rejected(self):
        self.prepare(); ci.run(self.work, 'demo')
        reviewed = ci.digest(self.work / 'result.json')
        (self.work / 'result.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'Reviewed result changed'):
            ci.promote(self.work, 'demo', self.releases, reviewed, 'local-admin')

    def test_missing_reviewer_and_busy_promotion_lock_rejected(self):
        self.prepare(); ci.run(self.work, 'demo')
        reviewed = ci.digest(self.work / 'result.json')
        with self.assertRaisesRegex(ValueError, 'reviewer'):
            ci.promote(self.work, 'demo', self.releases, reviewed, '')
        (self.releases / '.promotion-lock').mkdir()
        with self.assertRaises(FileExistsError):
            ci.promote(self.work, 'demo', self.releases, reviewed, 'local-admin')

    def test_run_workspace_and_snapshot_never_overwritten(self):
        self.prepare()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.prepare()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            ci.ingest(self.repo, 'demo', self.head, self.head, self.snapshots, 'demo-fixture')

    def test_juliaflow_command_contract_and_linux_rejection(self):
        p = ci.config('juliaflow')
        self.assertEqual(ci.juliaflow_command(p, {'commit': self.head, 'base': self.head}),
                         ['node', 'scripts/ci/wsl.mjs', '--ref', self.head, '--base', self.head,
                          '--profile', 'release-candidate', '--cache', 'cold'])
        with patch.object(ci.sys, 'platform', 'linux'), self.assertRaisesRegex(ValueError, 'native Windows'):
            # Owner/config identity is real; no runner success is fabricated.
            ci.ingest(self.repo, 'juliaflow', self.head, self.head, self.snapshots, 'jf-fixture')
            m = ci.digest(self.snapshots / 'jf-fixture/snapshot.json')
            jfwork = self.root / 'jfwork'
            ci.prepare(self.snapshots, 'jf-fixture', 'juliaflow', m, jfwork)
            ci.run(jfwork, 'juliaflow')

    def test_escape_and_bad_commit_rejected(self):
        for path in ('../repo/message.json', '/etc/passwd', 'C:/outside', 'a\\b', 'a/../b'):
            with self.assertRaises(ValueError):
                ci.confined(self.root, path)
        for commit in ('HEAD', '--help', self.head + ';x'):
            with self.assertRaises(ValueError):
                ci.sha(commit)

    def test_external_evidence_escape_and_hash_mismatch(self):
        folder = self.root / 'evidence'; folder.mkdir()
        ci.write_json(folder / 'sha256.json', {'../repo/message.json': '0' * 64})
        with self.assertRaisesRegex(ValueError, 'relative path'):
            ci.verify_inventory(folder)
        (folder / 'summary.json').write_text('{}')
        ci.write_json(folder / 'sha256.json', {'summary.json': '0' * 64})
        with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
            ci.verify_inventory(folder)

    def test_juliaflow_evidence_and_durable_copy_with_stub_runner(self):
        # Exercises adapter boundaries only. This stub is never runtime qualification.
        ci.ingest(self.repo, 'juliaflow', self.head, self.head, self.snapshots, 'jf-stub')
        jfwork = self.root / 'jfwork'
        ci.prepare(self.snapshots, 'jf-stub', 'juliaflow', ci.digest(self.snapshots / 'jf-stub/snapshot.json'), jfwork)
        source = jfwork / 'source'
        m = ci.read_json(jfwork / 'owner.json')['snapshot']
        run_id = 'jfci-1234567890-12345678'
        artifact_name = f'juliaflow-1.0.0-{self.head[:12]}-linux.tar.gz'

        def inventory(folder):
            return {f.relative_to(folder).as_posix(): ci.digest(f) for f in folder.rglob('*') if f.is_file() and f.name != 'sha256.json'}

        def stub(args, cwd, **kwargs):
            if args[1] == 'scripts/ci/wsl.mjs':
                evidence = source / f'artifacts/local-ci/{run_id}'
                package = evidence / 'release-candidate/delivery'; package.mkdir(parents=True)
                release_summary = evidence / 'release-candidate/release-verification/latest'; release_summary.mkdir(parents=True)
                (package / artifact_name).write_bytes(b'Explicit unit fixture, not a real product package')
                ci.write_json(package / 'package.json', {'name': artifact_name, 'version': '1.0.0', 'head': self.head,
                              'tree': m['tree'], 'sha256': ci.digest(package / artifact_name)})
                ci.write_json(release_summary / 'summary.json', {'unitFixture': True})
                ci.write_json(evidence / 'summary.json', {'head': self.head, 'tree': m['tree'], 'base': self.head,
                              'status': 'passed', 'completeProfile': True, 'cleanupStatus': 'passed', 'evidenceFinalization': 'passed'})
                ci.write_json(evidence / 'sha256.json', inventory(evidence))
            else:
                self.assertEqual(args, ['node', 'scripts/ci/delivery.mjs', '--run', run_id])
                evidence = source / f'artifacts/local-ci/{run_id}'
                metadata = ci.read_json(evidence / 'release-candidate/delivery/package.json')
                promoted = source / 'artifacts/local-releases' / artifact_name.removesuffix('.tar.gz'); promoted.mkdir(parents=True)
                (promoted / artifact_name).write_bytes((evidence / 'release-candidate/delivery' / artifact_name).read_bytes())
                ci.write_json(promoted / 'receipt.json', metadata)
                ci.write_json(promoted / 'sha256.json', inventory(promoted))
                ci.write_json(promoted.parent / 'current.json', {'directory': promoted.name})
            return ''

        original_command = ci.command

        def only_stub_node(args, cwd, **kwargs):
            return stub(args, cwd, **kwargs) if args[0] == 'node' else original_command(args, cwd, **kwargs)

        with patch.object(ci.sys, 'platform', 'win32'), patch.object(ci, 'command', side_effect=only_stub_node):
            result = ci.run(jfwork, 'juliaflow')
            reviewed = ci.digest(jfwork / 'result.json')
            target = ci.promote(jfwork, 'juliaflow', self.releases, reviewed, 'local-admin')
        self.assertEqual(ci.digest(target / artifact_name), result['artifactSha256'])
        self.assertEqual(ci.read_json(target / 'jenkins-review.json')['reviewer'], 'local-admin')


if __name__ == '__main__':
    unittest.main(verbosity=2)
