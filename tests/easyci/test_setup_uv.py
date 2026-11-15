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


def plan_job(text, files=None):
    inp = dict(INP)
    if files is not None:  # the repository at the tested commit, as the CLI gives it
        inp['read_file'] = lambda path: files[path].encode() if path in files else None
    plan = workflow.build_plan([('.github/workflows/ci.yml', text.encode())], {}, inp)
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

    def test_latest_from_a_matrix_is_the_same_as_literal_latest(self):
        text = ('on: pull_request\njobs:\n  j:\n    runs-on: ubuntu-latest\n'
                '    strategy: {matrix: {uv: [latest, "0.12"]}}\n    steps:\n'
                '      - uses: astral-sh/setup-uv@v10.3.3\n        with:\n          version: ${{ matrix.uv }}\n'
                '      - run: uv --version\n')
        plan = workflow.build_plan([('.github/workflows/ci.yml', text.encode())], {}, dict(INP))
        got = {j['id']: (j['predicted'], j['steps'][0].get('requested')) for j in plan['jobs']}
        self.assertEqual(got, {'j-latest': ('run', None), 'j-0.12': ('run', '0.12')})

    def test_without_a_version_input_the_project_pin_is_checked(self):
        # setup-uv reads required-version from uv.toml, else [tool.uv] in pyproject.toml.
        pyproject = '[project]\nname = "x"\n[tool.uv]\nrequired-version = "{}"\n'
        cases = [({}, None),
                 ({'pyproject.toml': '[project]\nname = "x"\n'}, None),
                 ({'pyproject.toml': pyproject.format('==0.12.1')}, '0.12.1'),
                 ({'pyproject.toml': pyproject.format('0.12')}, '0.12'),
                 ({'uv.toml': 'required-version = "0.11.4"\n', 'pyproject.toml': pyproject.format('0.12')}, '0.11.4'),
                 ({'uv.toml': 'native-tls = true\n', 'pyproject.toml': pyproject.format('0.12')}, '0.12')]
        for files, requested in cases:
            with self.subTest(files=files):
                job = plan_job(uv_job(), files)
                self.assertEqual((job['predicted'], job['steps'][1]['requested']), ('run', requested))
        # An explicit version input wins over the files, and working-directory picks the folder.
        job = plan_job(uv_job('          version: "0.10"\n'), {'pyproject.toml': pyproject.format('0.12')})
        self.assertEqual(job['steps'][1]['requested'], '0.10')
        job = plan_job(uv_job('          working-directory: app\n'),
                       {'pyproject.toml': pyproject.format('0.12'), 'app/uv.toml': 'required-version = "0.9.2"\n'})
        self.assertEqual(job['steps'][1]['requested'], '0.9.2')
        # A range cannot be checked against one installed version; an unreadable file is not skipped.
        for files, reason in (({'pyproject.toml': pyproject.format('>=0.5')},
                               'toolchain_spec_unsupported:uv required-version >=0.5 in pyproject.toml'),
                              ({'uv.toml': 'required-version = \n'}, 'toolchain_spec_unsupported:uv cannot read uv.toml')):
            with self.subTest(reason=reason):
                job = plan_job(uv_job(), files)
                self.assertEqual((job['predicted'], job['predicted_reason']), ('unsupported', reason))

    def test_pin_lookup_follows_the_checkout_path(self):
        pin = '[tool.uv]\nrequired-version = "0.12"\n'
        files = {'pyproject.toml': pin, 'app/uv.toml': 'required-version = "0.9.2"\n'}

        def job(checkout, workdir):
            first = ('      - uses: actions/checkout@v4\n' + (f'        with: {{path: "{checkout}"}}\n' if checkout else '')
                     if checkout is not None else '')
            with_ = f'        with: {{working-directory: "{workdir}"}}\n' if workdir else ''
            return ('on: pull_request\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n' + first +
                    '      - uses: astral-sh/setup-uv@v10.3.3\n' + with_ + '      - run: uv --version\n')

        for checkout, workdir, requested in (('source', 'source', '0.12'), ('source', 'source/app', '0.9.2'),
                                             ('./source/', './source', '0.12'), ('', '', '0.12'),
                                             ('', 'app/.', '0.9.2'), ('', 'app/..', '0.12'),
                                             ('source', 'source/app/../app', '0.9.2'),
                                             (None, '', None)):  # nothing checked out: no files to read
            with self.subTest(checkout=checkout, workdir=workdir):
                planned = plan_job(job(checkout, workdir), files)
                uv = next(s for s in planned['steps'] if s['kind'] == 'toolchain')
                self.assertEqual((planned['predicted'], uv['requested']), ('run', requested))
        for checkout, workdir, reason in (
                ('source', 'other', 'toolchain_spec_unsupported:uv working-directory other is outside the checkout'),
                ('source', '', 'toolchain_spec_unsupported:uv working-directory (the workspace root) is outside the checkout'),
                ('${{ github.event_name }}', '', 'toolchain_spec_unsupported:uv checkout path expression'),
                ('', '/srv/app', 'toolchain_spec_unsupported:uv working-directory /srv/app is absolute'),
                ('', 'app/../..', 'toolchain_spec_unsupported:uv working-directory app/../.. is outside the workspace')):
            with self.subTest(reason=reason):
                planned = plan_job(job(checkout, workdir), files)
                self.assertEqual((planned['predicted'], planned['predicted_reason']), ('unsupported', reason))

    def test_a_conditional_checkout_makes_the_pin_unknowable(self):
        # The checkout may be skipped at run time, and then there are no files to pin uv.
        files = {'pyproject.toml': '[tool.uv]\nrequired-version = "0.12"\n'}
        for condition, expected in (('false', ('unsupported', 'toolchain_spec_unsupported:uv pin after a conditional checkout')),
                                    ("github.event_name == 'push'",
                                     ('unsupported', 'toolchain_spec_unsupported:uv pin after a conditional checkout')),
                                    ('true', ('run', None))):
            with self.subTest(condition=condition):
                text = ('on: pull_request\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps:\n'
                        f'      - uses: actions/checkout@v4\n        if: {condition}\n'
                        '      - uses: astral-sh/setup-uv@v10.3.3\n      - run: uv --version\n')
                planned = plan_job(text, files)
                self.assertEqual((planned['predicted'], planned.get('predicted_reason')), expected)
                if expected[0] == 'run':
                    self.assertEqual(planned['steps'][1]['requested'], '0.12')

    def test_inputs_that_change_the_environment_are_unsupported(self):
        cases = {'          python-version: "3.12"\n': 'toolchain_spec_unsupported:uv python-version',
                 '          cache-local-path: /tmp/uv\n': 'toolchain_spec_unsupported:uv cache-local-path',
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
    def test_a_pin_in_pyproject_is_read_from_the_tested_commit(self):
        files = {'pyproject.toml': '[project]\nname = "x"\n[tool.uv]\nrequired-version = "0.12"\n'}
        repo = self.make_repo(uv_job(run='echo uv-ok'), files, remote='https://github.com/owner/repo.git')
        os.environ['EASYCI_FAKE_PROBES'] = json.dumps({'uv': 'uv 0.11.4', 'git': 'git version 2.50'})
        code, doc, err = self.call('plan', '--repo', repo, '--backend', 'fake', '--event', 'pull_request')
        self.assertEqual(code, 0, (doc, err))
        self.assertEqual(doc['data']['jobs'][0]['toolchains'][0]['requested'], '0.12')
        got, doc, err = self.call('run', '--repo', repo, '--backend', 'fake', '--no-report', '--event', 'pull_request')
        got, data = self.wait(doc['data']['run_id'])
        self.assertEqual((got, data['jobs'][0].get('reason')), (4, 'toolchain_mismatch:uv 0.11.4 != 0.12'))

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
