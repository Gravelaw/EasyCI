"""Adapter boundary fixtures only: no Node, WSL, Jenkins or real product run."""
import contextlib
import importlib.util
import pathlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('ci_rejections', ROOT / 'scripts/ci.py')
ci = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci)


class RunnerFixture:
    """Real isolated Git snapshot with explicitly synthetic runner output."""

    def __init__(self, root):
        self.root = root
        repo = root / 'repo'
        repo.mkdir()
        ci.git(['init', str(repo)], root)
        ci.git(['config', 'user.name', 'Unit Fixture'], repo)
        ci.git(['config', 'user.email', 'fixture@example.invalid'], repo)
        (repo / 'fixture.txt').write_text('Not a JuliaFlow checkout\n')
        ci.git(['add', 'fixture.txt'], repo)
        ci.git(['commit', '-m', 'isolated fixture'], repo)
        self.head = ci.git(['rev-parse', 'HEAD'], repo)
        snapshots = root / 'snapshots'
        snapshots.mkdir()
        snapshot = ci.ingest(repo, 'juliaflow', self.head, self.head, snapshots, 'jf-rejection')
        self.work = root / 'work'
        ci.prepare(snapshots, 'jf-rejection', 'juliaflow', ci.digest(snapshot / 'snapshot.json'), self.work)
        self.source = self.work / 'source'
        self.tree = ci.read_json(self.work / 'owner.json')['snapshot']['tree']
        self.releases = root / 'releases'
        self.releases.mkdir()
        self.run_id = 'jfci-1234567890-12345678'
        self.name = f'juliaflow-1.0.0-{self.head[:12]}-linux.tar.gz'
        self.evidence = self.source / 'artifacts/local-ci' / self.run_id
        self.package = self.evidence / 'release-candidate/delivery' / self.name
        self.upstream = self.source / 'artifacts/local-releases' / self.name.removesuffix('.tar.gz')
        self.delivery_calls = 0
        self.after_run = lambda: None
        self.after_delivery = lambda: None

    @staticmethod
    def inventory(folder):
        ci.write_json(folder / 'sha256.json', {
            p.relative_to(folder).as_posix(): ci.digest(p)
            for p in folder.rglob('*') if p.is_file() and p.name != 'sha256.json'
        })

    @staticmethod
    def update(path, **values):
        ci.write_json(path, {**ci.read_json(path), **values})

    def node(self, args, cwd, **kwargs):
        if pathlib.Path(cwd) != self.source:
            raise AssertionError('Unexpected Node working directory')
        if args == ['node', 'scripts/ci/wsl.mjs', '--ref', self.head, '--base', self.head,
                    '--profile', 'release-candidate', '--cache', 'cold']:
            self.package.parent.mkdir(parents=True)
            self.package.write_bytes(b'Explicit synthetic package: no real product qualification')
            ci.write_json(self.package.parent / 'package.json', {
                'name': self.name, 'version': '1.0.0', 'head': self.head,
                'tree': self.tree, 'sha256': ci.digest(self.package),
            })
            release_summary = self.evidence / 'release-candidate/release-verification/latest'
            release_summary.mkdir(parents=True)
            ci.write_json(release_summary / 'summary.json', {'unitFixture': True})
            ci.write_json(self.evidence / 'summary.json', {
                'head': self.head, 'tree': self.tree, 'base': self.head, 'status': 'passed',
                'completeProfile': True, 'cleanupStatus': 'passed', 'evidenceFinalization': 'passed',
            })
            self.inventory(self.evidence)
            self.after_run()
        elif args == ['node', 'scripts/ci/delivery.mjs', '--run', self.run_id]:
            self.delivery_calls += 1
            self.upstream.mkdir(parents=True)
            (self.upstream / self.name).write_bytes(self.package.read_bytes())
            ci.write_json(self.upstream / 'receipt.json', ci.read_json(self.package.parent / 'package.json'))
            self.inventory(self.upstream)
            ci.write_json(self.upstream.parent / 'current.json', {'directory': self.upstream.name})
            self.after_delivery()
        else:
            raise AssertionError(f'Unexpected Node invocation: {args}')
        return ''

    def run(self):
        result = ci.run(self.work, 'juliaflow')
        self.reviewed = ci.digest(self.work / 'result.json')
        return result

    def promote(self):
        return ci.promote(self.work, 'juliaflow', self.releases, self.reviewed, 'local-admin')


@contextlib.contextmanager
def fixture():
    with tempfile.TemporaryDirectory(prefix='easyci-jf-rejection-') as tmp:
        f = RunnerFixture(pathlib.Path(tmp))
        original_command = ci.command

        def command(args, cwd, **kwargs):
            if args[0] == 'node':
                return f.node(args, cwd, **kwargs)
            if args[0] != 'git':
                raise AssertionError(f'Unexpected executable: {args[0]}')
            return original_command(args, cwd, **kwargs)

        with patch.object(ci.sys, 'platform', 'win32'), patch.object(ci, 'command', side_effect=command):
            yield f


class JuliaFlowRejectionTests(unittest.TestCase):
    def assert_no_promotion(self, f, calls):
        self.assertEqual(f.delivery_calls, calls)
        self.assertFalse((f.work / 'promotion.json').exists())
        # Includes no durable release, staging directory or stale lock.
        self.assertEqual(list(f.releases.iterdir()), [])
        self.assertTrue(f.evidence.is_dir(), 'Retain runner evidence for diagnosis')

    def assert_run_rejected(self, f, message):
        with self.assertRaisesRegex(ValueError, message):
            f.run()
        self.assertFalse((f.work / 'result.json').exists())
        self.assertFalse((f.work / 'result.sha256').exists())
        self.assert_no_promotion(f, 0)

    def test_fixture_control_promotes_identical_bytes(self):
        with fixture() as f:
            result = f.run()
            target = f.promote()
            self.assertEqual(ci.digest(target / f.name), result['artifactSha256'])
            self.assertEqual(f.delivery_calls, 1)
            self.assertEqual(list(f.releases.iterdir()), [target])
            self.assertEqual(ci.read_json(target / 'jenkins-review.json')['reviewedResultSha256'], f.reviewed)

    def test_invalid_runner_identity_rejected(self):
        for field in ('head', 'tree', 'base'):
            with self.subTest(field=field), fixture() as f:
                def alter():
                    f.update(f.evidence / 'summary.json', **{field: '0' * 40})
                    f.inventory(f.evidence)  # Valid checksums cannot legitimize wrong identity.
                f.after_run = alter
                self.assert_run_rejected(f, 'Runner result gate failed')

    def test_incomplete_runner_gates_rejected(self):
        cases = [('status', 'failed'), ('completeProfile', False), ('completeProfile', 1),
                 ('cleanupStatus', 'failed'), ('evidenceFinalization', 'failed')]
        for field, value in cases:
            with self.subTest(field=field, value=value), fixture() as f:
                def alter():
                    f.update(f.evidence / 'summary.json', **{field: value})
                    f.inventory(f.evidence)
                f.after_run = alter
                self.assert_run_rejected(f, 'Runner result gate failed')

    def test_invalid_package_identity_rejected(self):
        for field, value in [('head', '0' * 40), ('tree', '0' * 40), ('sha256', '0' * 64)]:
            with self.subTest(field=field), fixture() as f:
                def alter():
                    f.update(f.package.parent / 'package.json', **{field: value})
                    f.inventory(f.evidence)
                f.after_run = alter
                self.assert_run_rejected(f, 'Package identity gate failed')

    def test_evidence_and_package_mutation_before_result_rejected(self):
        for member in ('summary', 'package'):
            with self.subTest(member=member), fixture() as f:
                def alter():
                    path = f.evidence / 'summary.json' if member == 'summary' else f.package
                    path.write_bytes(path.read_bytes() + b'\n')
                f.after_run = alter
                self.assert_run_rejected(f, 'Evidence checksum mismatch')

    def test_inventory_mutation_after_review_rejected_before_delivery(self):
        with fixture() as f:
            f.run()
            manifest = f.evidence / 'sha256.json'
            manifest.write_bytes(manifest.read_bytes() + b'\n')
            with self.assertRaisesRegex(ValueError, 'Evidence inventory changed'):
                f.promote()
            self.assert_no_promotion(f, 0)

    def test_evidence_and_package_mutation_after_review_rejected_before_delivery(self):
        for member in ('summary', 'package'):
            with self.subTest(member=member), fixture() as f:
                f.run()
                path = f.evidence / 'summary.json' if member == 'summary' else f.package
                path.write_bytes(path.read_bytes() + b'\n')
                with self.assertRaisesRegex(ValueError, 'Evidence checksum mismatch'):
                    f.promote()
                self.assert_no_promotion(f, 0)

    def test_delivery_failure_preserves_upstream_without_durable_promotion(self):
        for error in (ValueError('node failed (1)'), subprocess.TimeoutExpired('node', 120)):
            with self.subTest(error=type(error).__name__), fixture() as f:
                f.run()
                def fail():
                    raise error
                f.after_delivery = fail  # Model failure after upstream side effects.
                with self.assertRaises(type(error)) as raised:
                    f.promote()
                self.assertIs(raised.exception, error)
                self.assert_no_promotion(f, 1)
                self.assertTrue((f.upstream / 'receipt.json').exists())
                self.assertTrue((f.upstream.parent / 'current.json').exists())

    def test_invalid_delivery_receipt_identity_rejected(self):
        for field, value in [('head', '0' * 40), ('sha256', '0' * 64)]:
            with self.subTest(field=field), fixture() as f:
                f.run()
                def alter():
                    f.update(f.upstream / 'receipt.json', **{field: value})
                    f.inventory(f.upstream)
                f.after_delivery = alter
                with self.assertRaisesRegex(ValueError, 'Promoted identity mismatch'):
                    f.promote()
                self.assert_no_promotion(f, 1)

    def test_partial_durable_copy_failure_cleans_staging_and_retains_upstream(self):
        with fixture() as f:
            f.run()
            def fail_copy(source, destination, **kwargs):
                self.assertEqual(source, f.upstream)
                destination.mkdir()
                (destination / 'partial').write_bytes(b'incomplete copy')
                raise OSError('injected copy failure')
            with patch.object(ci.shutil, 'copytree', side_effect=fail_copy) as copy:
                with self.assertRaisesRegex(OSError, 'injected copy failure'):
                    f.promote()
            copy.assert_called_once()
            self.assert_no_promotion(f, 1)
            self.assertTrue((f.upstream / f.name).exists())

    def test_corrupted_durable_copy_rejected(self):
        with fixture() as f:
            result = f.run()
            original_copy = ci.shutil.copytree
            def corrupt_copy(source, destination, **kwargs):
                copied = original_copy(source, destination, **kwargs)
                (destination / f.name).write_bytes(b'corrupted in transit')
                return copied
            with patch.object(ci.shutil, 'copytree', side_effect=corrupt_copy):
                with self.assertRaisesRegex(ValueError, 'Durable package checksum mismatch'):
                    f.promote()
            self.assert_no_promotion(f, 1)
            self.assertEqual(ci.digest(f.upstream / f.name), result['artifactSha256'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
