"""Phase 3: promote with receipts, the large-artifact store, data hashes, metrics comparison."""
import json
import os
import pathlib
import stat

from ecsupport import EasyCITest
from easyci import store

BUILD = ('on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n'
         '      - run: |\n          mkdir -p dist && echo "package v1" > dist/app.txt\n'
         '          echo \'{"accuracy": 0.91}\' > easyci-metrics.json\n'
         '      - uses: actions/upload-artifact@v4\n        with: {name: package, path: dist}\n')


class Releases(EasyCITest):
    def passed_run(self, workflow=BUILD, files=None, name='repo'):
        repo = self.make_repo(workflow, {'easyci.yml': 'backend: native\n', **(files or {})}, name=name)
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        return repo, data['run_id']

    def test_promote_verify_and_never_overwrite(self):
        repo, run_id = self.passed_run()
        releases = self.tmp / 'releases'
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', releases, '--version', '1.0.0')
        self.assertEqual(code, 0, doc)
        folder = pathlib.Path(doc['data']['release'])
        self.assertEqual(folder.parent, releases)
        self.assertEqual((folder / 'app.txt').read_text().strip(), 'package v1')
        receipt = json.loads((folder / 'receipt.json').read_text())
        self.assertEqual((receipt['run_id'], receipt['artifact'], list(receipt['files'])), (run_id, 'package', ['app.txt']))
        self.assertFalse(os.stat(folder / 'app.txt').st_mode & stat.S_IWRITE)  # read-only
        self.assertEqual(self.call('verify', folder)[0], 0)
        # Never overwritten; another version is fine.
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', releases, '--version', '1.0.0')
        self.assertEqual((code, doc['error']['code']), (2, 'RELEASE_EXISTS'))
        self.assertEqual(self.call('promote', run_id, '--artifact', 'package', '--to', releases, '--version', '1.0.1')[0], 0)
        self.assertEqual(self.call('promote', run_id, '--artifact', 'nope', '--to', releases)[1]['error']['code'],
                         'NO_ARTIFACT')
        # A changed release fails verification.
        os.chmod(folder / 'app.txt', stat.S_IWRITE)
        (folder / 'app.txt').write_text('tampered')
        code, doc, _ = self.call('verify', folder)
        self.assertEqual((code, doc['error']['code']), (1, 'TAMPERED'))
        # A stale lock (another promotion) makes promotion fail closed.
        (releases / '.easyci-promote-lock').mkdir()
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', releases, '--version', '2')
        self.assertEqual(doc['error']['code'], 'PROMOTE_BUSY')

    def test_an_artifact_receipt_json_is_refused_not_overwritten(self):
        # Review finding: the release receipt silently replaced a tested receipt.json from the artifact.
        repo, run_id = self.passed_run(BUILD.replace('> dist/app.txt', '> dist/receipt.json'))
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', self.tmp / 'rel')
        self.assertEqual(doc['error']['code'], 'RESERVED_NAME', doc)
        self.assertEqual(list((self.tmp / 'rel').glob('*')) if (self.tmp / 'rel').exists() else [], [])
        # In a subfolder it is just another file.
        repo, run_id = self.passed_run(BUILD.replace('mkdir -p dist && echo "package v1" > dist/app.txt',
                                                     'mkdir -p dist/meta && echo x > dist/meta/receipt.json'), name='sub')
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', self.tmp / 'rel')
        self.assertEqual(code, 0, doc)
        self.assertEqual(self.call('verify', doc['data']['release'])[0], 0)

    def test_promote_finds_an_artifact_by_its_workflow_name(self):
        repo, run_id = self.passed_run(BUILD.replace('{name: package, path: dist}', '{name: test results, path: dist}'))
        code, doc, _ = self.call('promote', run_id, '--artifact', 'test results', '--to', self.tmp / 'rel')
        self.assertEqual((code, doc['data']['files']), (0, 1), doc)

    def test_only_passed_runs_are_promoted(self):
        failing = BUILD.replace('echo "package v1" > dist/app.txt', 'echo x > dist/app.txt; exit 1')
        repo = self.make_repo(failing, {'easyci.yml': 'backend: native\n'})
        run_id = self.start(repo, '--no-report')['run_id']
        self.wait(run_id)
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', self.tmp / 'r')
        self.assertEqual((code, doc['error']['code']), (2, 'NOT_PASSED'))

    def test_a_second_upload_replaces_an_offloaded_file(self):
        # Review finding: after a big first upload was moved out, a small re-upload of the same file
        # kept the old pointer, and promote published the obsolete big file.
        workflow = ('on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - run: mkdir -p dist && head -c 1500000 /dev/zero > dist/app.bin\n'
                    '      - uses: actions/upload-artifact@v4\n        with: {name: package, path: dist}\n'
                    '      - run: echo small > dist/app.bin\n'
                    '      - uses: actions/upload-artifact@v4\n        with: {name: package, path: dist}\n')
        repo, run_id = self.passed_run(workflow, {'easyci.yml': 'backend: native\nartifact_threshold_mb: 1\n'})
        rd = self.run_dir(run_id)
        self.assertFalse((rd / 'jobs/build/artifacts/package.pointer.json').exists())
        self.assertEqual(self.call('verify', run_id)[0], 0)
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', self.tmp / 'rel')
        self.assertEqual(code, 0, doc)
        self.assertEqual((pathlib.Path(doc['data']['release']) / 'app.bin').read_text().strip(), 'small')

    def test_pointer_named_files_inside_an_artifact_are_just_content(self):
        # Review finding: any *.pointer.json was read as EasyCI metadata, so verify crashed or failed.
        workflow = BUILD.replace('echo "package v1" > dist/app.txt',
                                 'echo \'["model"]\' > dist/model.pointer.json; mkdir -p dist/x; '
                                 'echo \'{"files": {"a": "b"}}\' > dist/x/y.pointer.json')
        repo, run_id = self.passed_run(workflow)
        self.assertEqual(self.call('verify', run_id)[0], 0)
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', self.tmp / 'rel')
        self.assertEqual((code, doc['data']['files']), (0, 2), doc)

    def test_big_artifacts_live_outside_the_run_but_stay_verified(self):
        workflow = BUILD.replace('echo "package v1" > dist/app.txt', 'head -c 1500000 /dev/zero > dist/big.bin')
        repo, run_id = self.passed_run(workflow, {'easyci.yml': 'backend: native\nartifact_threshold_mb: 1\n'})
        rd = self.run_dir(run_id)
        pointer = json.loads((rd / 'jobs/build/artifacts/package.pointer.json').read_text())
        location = pathlib.Path(pointer['location'])
        self.assertTrue((location / 'big.bin').is_file())
        self.assertFalse((rd / 'jobs/build/artifacts/package').exists())
        self.assertTrue(str(location).startswith(str(self.home / 'artifacts')))
        self.assertEqual(self.call('verify', run_id)[0], 0)
        code, doc, _ = self.call('promote', run_id, '--artifact', 'package', '--to', self.tmp / 'rel')
        self.assertEqual(code, 0, doc)
        with open(location / 'big.bin', 'ab') as handle:  # tamper with the offloaded file
            handle.write(b'!')
        code, doc, _ = self.call('verify', run_id)
        self.assertEqual((code, doc['error']['code']), (1, 'TAMPERED'))
        self.assertIn('package.pointer.json:big.bin', doc['error']['message'])

    def test_data_hashes_and_metrics_comparison(self):
        files = {'data/train.csv': 'a,b\n1,2\n', 'model.dvc': 'outs:\n- md5: abc123\n  path: model.pkl\n',
                 'easyci.yml': 'backend: native\ndata: [data/*.csv, "*.dvc"]\n'}
        repo, first = self.passed_run(files=files)
        report = json.loads((self.run_dir(first) / 'report.json').read_text())
        data = report['reproducibility']['data']
        self.assertEqual(data['data/train.csv']['sha256'], store.digest_bytes(b'a,b\n1,2\n'))
        self.assertEqual(data['model.dvc']['dvc_md5'], ['abc123'])
        # A second run with a better metric; compare shows the change.
        (pathlib.Path(repo) / '.github/workflows/ci.yml').write_text(BUILD.replace('0.91', '0.95'))
        self.git(repo, 'commit', '-qam', 'better')
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual(code, 0)
        code, doc, _ = self.call('compare', '--repo', repo)
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc['data']['metrics'], [{'metric': 'build.accuracy', 'before': 0.91, 'after': 0.95,
                                                   'change': 0.04}])


class DataFolders(EasyCITest):
    def test_folder_patterns_record_every_file(self):
        # Review finding: `data: [data]` and `data/**` (folders) recorded nothing.
        repo = self.make_repo(None, {'data/train.csv': 'a\n', 'data/sub/test.csv': 'b\n'}, name='dataset')
        for patterns in (['data'], ['data/**']):
            self.assertEqual(sorted(store.data_hashes(repo, patterns)), ['data/sub/test.csv', 'data/train.csv'], patterns)

    def test_tracked_data_is_hashed_as_tested_untracked_from_the_folder(self):
        # Review finding: tracked data was hashed from the working copy even when it differed from the
        # commit the job checked out.
        repo = self.make_repo(None, {'data/train.csv': 'tested\n'}, name='dataset-commit')
        commit = self.git(repo, 'rev-parse', 'HEAD')
        (pathlib.Path(repo) / 'data' / 'train.csv').write_text('edited, not committed\n')
        (pathlib.Path(repo) / 'data' / 'big.parquet').write_text('untracked\n')
        hashes = store.data_hashes(repo, ['data'], commit)
        self.assertEqual(hashes['data/train.csv']['sha256'], store.digest_bytes(b'tested\n'))
        self.assertEqual(hashes['data/train.csv']['source'], 'commit')
        self.assertEqual(hashes['data/big.parquet']['source'], 'working tree')


class OffloadedExtras(EasyCITest):
    passed_run = Releases.passed_run

    def test_files_added_to_an_offloaded_artifact_are_reported(self):
        # Review finding: verify only checked the files a pointer listed.
        workflow = BUILD.replace('echo "package v1" > dist/app.txt', 'head -c 1500000 /dev/zero > dist/big.bin')
        repo, run_id = self.passed_run(workflow, {'easyci.yml': 'backend: native\nartifact_threshold_mb: 1\n'})
        pointer = json.loads((self.run_dir(run_id) / 'jobs/build/artifacts/package.pointer.json').read_text())
        (pathlib.Path(pointer['location']) / 'added.txt').write_text('not tested')
        code, doc, _ = self.call('verify', run_id)
        self.assertEqual((code, doc['error']['code']), (1, 'TAMPERED'))
        self.assertIn('package.pointer.json:added.txt', doc['error']['message'])
