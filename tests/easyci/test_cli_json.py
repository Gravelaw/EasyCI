"""Criterion 15: every command, success and failure, prints exactly one easyci/1 JSON document."""
import contextlib
import io
import json
import os

from ecsupport import EasyCITest
from easyci import cli

QUICK = 'on: push\njobs:\n  quick:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hello\n'
DOCKER = 'on: push\njobs:\n  img:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: docker/build-push-action@v5\n'


class JsonDiscipline(EasyCITest):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(QUICK, remote='https://github.com/owner/repo.git')
        self.not_repo = self.tmp / 'plain-folder'
        self.not_repo.mkdir()

    def finished_run(self):
        code, doc, _ = self.call('run', '--repo', self.repo, '--backend', 'native', '--wait')
        self.assertEqual(code, 0, doc)
        self.run_id = doc['data']['run_id']
        return self.run_id

    def expect(self, code, *args, error=None):
        got, doc, err = self.call(*args)
        self.assertEqual(got, code, (args, doc, err))
        if error:
            self.assertEqual(doc['error']['code'], error)
        return doc

    def test_success_documents(self):
        self.finished_run()
        for args in (('status', self.run_id), ('list', '--repo', self.repo), ('logs', self.run_id),
                     ('wait', self.run_id), ('cancel', self.run_id), ('explain', self.run_id),
                     ('plan', '--repo', self.repo), ('report', self.run_id), ('verify', self.run_id),
                     ('clean', '--keep', '5')):
            with self.subTest(args=args):
                doc = self.expect(0, *args)
                self.assertTrue(doc['ok'])
                self.assertIn('data', doc)
        self.assertEqual(self.expect(0, 'logs', self.run_id)['data']['job'], 'quick')
        self.assertEqual(self.expect(0, 'status', self.run_id)['data']['state'], 'passed')

    def test_json_before_the_command_is_kept(self):
        # `easyci --json plan` must give the same document as `easyci plan --json` (review finding).
        self.finished_run()
        for args in (('plan', '--repo', self.repo), ('explain', self.run_id), ('logs', self.run_id)):
            with self.subTest(args=args):
                after = self.expect(0, *args)['data']
                out = io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = cli.main(['--json', *map(str, args)])
                self.assertEqual(code, 0)
                before = json.loads(out.getvalue())['data']
                self.assertTrue(before)
                self.assertEqual(before.keys(), after.keys())

    def test_failure_documents(self):
        bad = '20990101-000000-ffff'
        for args, code, error in (
                (('status', bad), 7, 'RUN_NOT_FOUND'), (('logs', bad), 7, 'RUN_NOT_FOUND'),
                (('wait', bad), 7, 'RUN_NOT_FOUND'), (('cancel', bad), 7, 'RUN_NOT_FOUND'),
                (('explain', bad), 7, 'RUN_NOT_FOUND'), (('report', bad), 7, 'RUN_NOT_FOUND'),
                (('verify', bad), 7, 'RUN_NOT_FOUND'), (('list', '--repo', self.not_repo), 7, 'REPO_NOT_FOUND'),
                (('plan', '--repo', self.not_repo), 7, 'REPO_NOT_FOUND'),
                (('run', '--repo', self.not_repo), 7, 'REPO_NOT_FOUND'),
                (('run', '--repo', self.repo, '--commit', 'nope'), 7, 'COMMIT_NOT_FOUND'),
                (('logs', bad, '--follow'), 2, 'USAGE'), (('status', '--watch'), 2, 'USAGE'),
                (('clean', '--keep', 'abc'), 2, 'USAGE'), (('frobnicate',), 2, 'USAGE'), ((), 2, 'USAGE'),
                (('run', '--repo', self.repo, '--profile', 'missing'), 2, 'CONFIG_INVALID')):
            with self.subTest(args=args):
                self.expect(code, *args, error=error)

    def test_config_unsupported_environment_and_reporting_errors(self):
        cfg = self.tmp / 'bad.yml'
        cfg.write_text('jobs: {quick: {when: sometimes}}\n')
        self.expect(2, 'plan', '--repo', self.repo, '--config', cfg, error='CONFIG_INVALID')
        docker = self.make_repo(DOCKER, name='docker')
        doc = self.expect(4, 'plan', '--repo', docker, error='UNSUPPORTED')
        self.assertEqual(doc['data']['jobs'][0]['predicted_reason'], 'unsupported_uses:docker/build-push-action@v5')
        os.environ['FAKE_GH_AUTH'] = '1'
        doc = self.expect(3, 'run', '--repo', self.repo, '--backend', 'native', error='GH_NOT_LOGGED_IN')
        self.assertIn('gh auth login', doc['error']['fix'])
        offline = self.make_repo(QUICK, name='offline')
        code, doc, _ = self.call('run', '--repo', offline, '--backend', 'native', '--wait')
        self.assertEqual(code, 0)
        self.expect(2, 'report', doc['data']['run_id'], error='REPORTING_OFF')

    def test_doctor(self):
        code, doc, _ = self.call('doctor')
        checks = {c['id']: c for c in doc['data']['checks']}
        self.assertTrue(checks['gh']['ok'])
        self.assertIn('fake-user', checks['gh']['detail'])
        self.assertEqual(checks['docker']['required'], False)
        self.assertFalse(checks['wsl']['required'], 'WSL is optional unless a backend needs it')
        others = [c['id'] for c in doc['data']['checks'] if c['required'] and not c['ok']]
        self.assertEqual(code, 3 if others else 0, others)
        os.environ['FAKE_GH_AUTH'] = '1'
        code, doc, _ = self.call('doctor', '--repo', self.repo)
        self.assertEqual(code, 3)
        gh = next(c for c in doc['data']['checks'] if c['id'] == 'gh')
        self.assertFalse(gh['ok'])
        self.assertTrue(gh['fix'].startswith('Run: gh auth login'))
        self.assertIn('workflows', {c['id'] for c in doc['data']['checks']})

    def test_stdout_holds_only_the_document(self):
        self.finished_run()
        result = self.subprocess_cli('status', self.run_id, '--json')
        self.assertEqual(result.returncode, 0, result.stderr)
        doc = json.loads(result.stdout)
        self.assertEqual((doc['schema'], doc['data']['run_id']), ('easyci/1', self.run_id))
        result = self.subprocess_cli('status', 'missing-run', '--json')
        self.assertEqual(result.returncode, 7)
        self.assertEqual(json.loads(result.stdout)['error']['code'], 'RUN_NOT_FOUND')
        result = self.subprocess_cli('--version')
        self.assertIn('easyci', result.stdout)
