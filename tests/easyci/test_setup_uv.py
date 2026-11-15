"""astral-sh/setup-uv is checked like the other setup actions: uv must already be on the backend,
at the requested version; inputs that would change the job's environment are reported unsupported."""
import contextlib
import io
import json
import os
import unittest
from unittest import mock

from ecsupport import EasyCITest
from easyci import cli, workflow

SHA, BASE = 'a' * 40, 'b' * 40
INP = {'event': 'pull_request', 'ref_name': 'feature', 'default_branch': 'main', 'commit': SHA, 'base': BASE,
       'repository': 'owner/repo'}


def uv_job(with_='', run='uv --version'):
    step = '      - uses: astral-sh/setup-uv@v10.3.3\n' + (f'        with:\n{with_}' if with_ else '')
    return ('on: pull_request\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n'
            '      - uses: actions/checkout@v4\n' + step + f'      - run: {run}\n')


def plan_job(text):
    plan = workflow.build_plan([('.github/workflows/ci.yml', text.encode())], {}, dict(INP))
    return {j['id']: j for j in plan['jobs']}['j']


class Mapping(unittest.TestCase):
    def test_setup_uv_is_a_toolchain_check(self):
        cases = {'': None,
                 '          version: "0.5.1"\n': '0.5.1',
                 '          version: latest\n': None,  # setup-uv's default: any installed uv
                 '          enable-cache: true\n          cache-dependency-glob: uv.lock\n': None}
        for with_, requested in cases.items():
            with self.subTest(with_=with_):
                job = plan_job(uv_job(with_))
                self.assertEqual(job['predicted'], 'run', job.get('predicted_reason'))
                step = job['steps'][1]
                self.assertEqual((step['kind'], step['tool'], step['requested']), ('toolchain', 'uv', requested))
                self.assertEqual(job['toolchains'], [{'tool': 'uv', 'requested': requested, 'step': 2, 'if': None}])

    def test_inputs_that_change_the_environment_are_unsupported(self):
        cases = {'          python-version: "3.12"\n': 'toolchain_spec_unsupported:uv python-version',
                 '          activate-environment: true\n': 'toolchain_spec_unsupported:uv activate-environment',
                 '          tool-dir: /opt/uv\n          tool-bin-dir: /opt/bin\n':
                     'toolchain_spec_unsupported:uv tool-bin-dir, tool-dir',
                 '          version-file: pyproject.toml\n': 'toolchain_spec_unsupported:uv version file',
                 '          version: ">=0.4"\n': 'toolchain_spec_unsupported:uv >=0.4'}
        for with_, reason in cases.items():
            with self.subTest(reason=reason):
                job = plan_job(uv_job(with_))
                self.assertEqual((job['predicted'], job['predicted_reason']), ('unsupported', reason))

    def test_manifest_file_is_still_a_version_file_for_other_actions(self):
        job = plan_job('on: pull_request\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n'
                       '      - uses: actions/setup-node@v4\n        with: {manifest-file: x.json}\n      - run: node -v\n')
        self.assertEqual(job['predicted_reason'], 'toolchain_spec_unsupported:node version file')


class Runs(EasyCITest):
    def test_uv_is_verified_on_the_backend(self):
        repo = self.make_repo(uv_job('          version: "0.12"\n', run='echo uv-ok'), remote='https://github.com/owner/repo.git')
        for probe, code, state, reason in (('uv 0.12.1 (x86_64-unknown-linux-gnu)', 0, 'passed', None),
                                           ('uv 0.11.4', 4, 'unsupported', 'toolchain_mismatch:uv 0.11.4 != 0.12'),
                                           (None, 4, 'unsupported', 'toolchain_missing:uv')):
            with self.subTest(probe=probe):
                os.environ['EASYCI_FAKE_PROBES'] = json.dumps({'uv': probe, 'git': 'git version 2.50'})
                got, doc, err = self.call('run', '--repo', repo, '--backend', 'fake', '--no-report', '--event', 'pull_request')
                self.assertEqual(got, 0, (doc, err))
                got, data = self.wait(doc['data']['run_id'])
                job = data['jobs'][0]
                self.assertEqual((got, job['state'], job.get('reason')), (code, state, reason), data)


class SetupPrompt(EasyCITest):
    def test_a_terminal_with_no_input_writes_nothing_instead_of_crashing(self):
        # Review of a real onboarding (ithax): stdin was a terminal, but nobody could answer.
        repo = self.make_repo(None, {'calc.py': 'x = 1\n', 'requirements.txt': ''}, name='calc')
        os.environ['EASYCI_FAKE_PROBES'] = json.dumps({'python': 'Python 3.14.6', 'git': 'git version 2.50'})
        stdin = mock.Mock()
        stdin.isatty.return_value = True
        out, err = io.StringIO(), io.StringIO()
        with mock.patch('sys.stdin', stdin), mock.patch('builtins.input', side_effect=EOFError), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(['setup', '--repo', str(repo)])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn('Nothing written', text)
        self.assertNotIn('hit a bug', text)
        self.assertFalse((repo / 'easyci.yml').exists())


if __name__ == '__main__':
    unittest.main()
