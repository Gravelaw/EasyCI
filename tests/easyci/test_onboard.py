"""`easyci setup`: stack detection, backend choice, starter workflows that EasyCI itself accepts."""
import json
import os
import pathlib

import yaml

from ecsupport import EasyCITest
from easyci import onboard, workflow

PROJECTS = {
    'node': {'package.json': json.dumps({'scripts': {'build': 'x', 'test': 'y', 'lint': 'z'}, 'engines': {'node': '>=22'}}),
             'package-lock.json': '{}'},
    'pnpm': {'package.json': json.dumps({'scripts': {'test': 'y'}}), 'pnpm-lock.yaml': ''},
    'python': {'pyproject.toml': '[project]\nname = "x"\nrequires-python = ">=3.11"\n[project.optional-dependencies]\n'
                                 'test = ["pytest"]\n', 'requirements.txt': 'requests\n'},
    'julia': {'Project.toml': 'name = "X"\n', 'Manifest.toml': 'julia_version = "1.12.7"\n', 'test/runtests.jl': ''},
    'go': {'go.mod': 'module x\n\ngo 1.23\n'},
    'rust': {'Cargo.toml': '[package]\nname = "x"\n', 'Cargo.lock': ''},
    'maven': {'pom.xml': '<project/>', 'mvnw': ''},
    'gradle': {'build.gradle': ''},
    'dotnet': {'App.csproj': '<Project/>'},
}
PROBES = {'node': 'v26.3.0', 'npm': '11.0.0', 'python': 'Python 3.14.6', 'julia': 'julia version 1.12.7',
          'go': 'go version go1.23.4 windows/amd64', 'cargo': 'cargo 1.85.0', 'java': 'openjdk 21.0.2',
          'mvn': 'Apache Maven 3.9', 'dotnet': '9.0.100', 'git': 'git version 2.50'}


class Onboarding(EasyCITest):
    def project(self, name, files):
        root = self.tmp / name
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        return root

    def test_every_stack_gets_a_workflow_easyci_accepts(self):
        os.environ['EASYCI_FAKE_PROBES'] = json.dumps(PROBES)
        for name, files in PROJECTS.items():
            with self.subTest(stack=name):
                root = self.project(name, files)
                stacks = onboard.detect(root)
                self.assertEqual(len(stacks), 1, stacks)
                for linux in (True, False):
                    text = onboard.workflow_text(stacks, PROBES, linux)
                    yaml.safe_load(text)
                    plan = workflow.build_plan([('.github/workflows/ci.yml', text.encode())], {},
                                               {'event': 'pull_request', 'ref_name': 'feature', 'default_branch': 'main',
                                                'commit': 'a' * 40, 'base': 'b' * 40, 'repository': 'o/r',
                                                'default_backend': 'wsl:x'})
                    job = plan['jobs'][0]
                    self.assertNotEqual(job['predicted'], 'unsupported', (name, linux, job.get('predicted_reason'), text))

    def test_details_feed_the_template(self):
        node = onboard.detect(self.project('n', PROJECTS['node']))[0]
        self.assertEqual((node['manager'], node['locked'], node['version'], node['requires']), ('npm', True, None, '>=22'))
        text = onboard.workflow_text([node], PROBES, True)
        # Review finding: engines.node is a constraint; the installed Node 26 meets >=22, so it is requested.
        self.assertIn('node-version: 26', text)
        self.assertIn('node-version: 20', onboard.workflow_text([{**node, 'requires': '^20'}], PROBES, True))
        self.assertIn('node-version: 18', onboard.workflow_text([{**node, 'version': '18'}], PROBES, True))  # .nvmrc pins
        self.assertLess(text.index('npm ci'), text.index('npm run lint'))
        self.assertLess(text.index('npm run build'), text.index('npm run test'))
        self.assertIn('corepack enable && pnpm install --frozen-lockfile',
                      onboard.workflow_text(onboard.detect(self.project('p', PROJECTS['pnpm'])), PROBES, True))
        py = onboard.detect(self.project('py', PROJECTS['python']))[0]
        self.assertEqual((py['version'], py['requires'], py['extras'], py['pytest'], py['requirements']),
                         (None, '>=3.11', ['test'], True, ['requirements.txt']))
        text = onboard.workflow_text([py], PROBES, True)
        # Review finding: requires-python is a constraint. The installed 3.14 meets >=3.11, so it is
        # requested (an exact "3.11" would be rejected as a mismatch and nothing would run).
        self.assertIn('python-version: "3.14"', text)
        self.assertIn('python-version: "3.13"', onboard.workflow_text([{**py, 'requires': '>=3.13,<3.14'}],
                                                                      {**PROBES, 'python': 'Python 3.12.1'}, True))
        self.assertIn('python3 -m venv .venv', text)
        self.assertIn('pip install -e ".[test]"', text)
        self.assertIn('. .venv/Scripts/activate', onboard.workflow_text([py], PROBES, False))
        self.assertIn('version: "1.12"', onboard.workflow_text(onboard.detect(self.project('j', PROJECTS['julia'])),
                                                               PROBES, True))
        self.assertEqual(onboard.detect(self.project('empty', {'README.md': 'x'})), [])

    def test_backend_choice_prefers_the_candidate_with_every_tool(self):
        stacks = [{'stack': 'julia'}, {'stack': 'node'}]
        partial = lambda tools: {t: PROBES[t] for t in tools if t in ('git', 'node', 'npm')}  # noqa: E731
        full = lambda tools: {t: PROBES[t] for t in tools}  # noqa: E731
        choice = onboard.choose_backend(stacks, [('wsl:a', partial), ('wsl:b', full), ('native', partial)])
        self.assertEqual((choice['backend'], choice['missing']), ('wsl:b', []))
        choice = onboard.choose_backend(stacks, [('wsl:a', partial), ('native', lambda tools: {})])
        self.assertEqual((choice['backend'], choice['missing']), ('wsl:a', ['julia']))

    def test_setup_command_writes_commits_and_runs(self):
        os.environ['EASYCI_FAKE_PROBES'] = json.dumps(PROBES)
        repo = self.make_repo(None, {'calc.py': 'def add(a, b):\n    return a + b\n',
                                     'test_calc.py': 'import unittest\nfrom calc import add\n\n\nclass T(unittest.TestCase):\n'
                                                     '    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n',
                                     'requirements.txt': ''}, name='calc')
        # 1. JSON mode only plans: nothing is written unless asked.
        code, doc, _ = self.call('setup', '--repo', repo)
        self.assertEqual(code, 0, doc)
        self.assertEqual([f['path'] for f in doc['data']['files']], ['.github/workflows/ci.yml', 'easyci.yml'])
        self.assertEqual(doc['data']['written'], [])
        self.assertFalse((pathlib.Path(repo) / 'easyci.yml').exists())
        # 2. --write alone cannot --run: EasyCI tests committed code.
        code, doc, _ = self.call('setup', '--repo', repo, '--write', '--run')
        self.assertEqual((code, doc['error']['code']), (2, 'UNCOMMITTED_SETUP'))
        for rel in ('.github/workflows/ci.yml', 'easyci.yml'):
            (pathlib.Path(repo) / rel).unlink()
        # 3. --commit writes and commits only those files; --run then passes; --desktop makes a launcher.
        launcher_dir = self.tmp / 'desktop'
        launcher_dir.mkdir()
        os.environ['EASYCI_DESKTOP'] = str(launcher_dir)
        from ecsupport import GIT_ENV
        os.environ.update(GIT_ENV)  # the commit is made as the user; give the test user an identity
        code, doc, _ = self.call('setup', '--repo', repo, '--commit', '--run', '--desktop')
        self.assertEqual(code, 0, doc)
        data = doc['data']
        self.assertEqual(data['written'], ['.github/workflows/ci.yml', 'easyci.yml'])
        self.assertEqual(self.git(repo, 'log', '-1', '--format=%s'), 'Add EasyCI checks')
        self.assertEqual(pathlib.Path(data['launcher']).parent, launcher_dir)
        self.assertIn('-m easyci run --wait', pathlib.Path(data['launcher']).read_text())
        self.assertIn('-m easyci dashboard --open', pathlib.Path(data['dashboard_launcher']).read_text())
        code, result = self.wait(data['run']['run_id'], timeout=120)
        self.assertEqual((code, result['state']), (0, 'passed'), result)
        # 4. Run again: nothing left to write, and nothing is overwritten.
        code, doc, _ = self.call('setup', '--repo', repo)
        self.assertEqual((code, doc['data']['files']), (0, []))

    def test_gradle_without_a_wrapper_needs_gradle(self):
        # Review finding: only java and git were probed, so a distro without gradle could be chosen.
        stacks = [{'stack': 'gradle', 'wrapper': False}]
        probes = [('wsl:java-only', lambda tools: {'java': 'openjdk 21', 'git': 'git 2'}),
                  ('wsl:with-gradle', lambda tools: {'java': 'openjdk 21', 'git': 'git 2', 'gradle': 'Gradle 8.10'})]
        self.assertEqual(onboard.choose_backend(stacks, probes)['backend'], 'wsl:with-gradle')
        self.assertEqual(onboard.choose_backend([{'stack': 'gradle', 'wrapper': True}], probes)['backend'],
                         'wsl:java-only')
        # `gradle --version` starts with a blank line; the probe reads the first non-empty one.
        from easyci.backends import native
        bindir = self.tmp / 'fakebin'
        bindir.mkdir()
        (bindir / 'gradle.cmd').write_text('@echo off\r\necho.\r\necho Gradle 8.10\r\n')
        os.environ['PATH'] = f'{bindir}{os.pathsep}{os.environ["PATH"]}'
        self.assertEqual(native.Native().probe(['gradle'])['gradle'], 'Gradle 8.10')

    def test_commit_after_write_and_project_registered(self):
        # Review findings: `setup --commit` after `setup --write` committed nothing, and setup did not
        # register the project, so the dashboard had nothing to run.
        from easyci import dashboard
        from ecsupport import GIT_ENV
        os.environ['EASYCI_FAKE_PROBES'] = json.dumps(PROBES)
        os.environ.update(GIT_ENV)
        repo = self.make_repo(None, {'calc.py': 'x = 1\n', 'requirements.txt': ''}, name='later')
        code, doc, _ = self.call('setup', '--repo', repo, '--write')
        self.assertEqual(doc['data']['uncommitted'], ['.github/workflows/ci.yml', 'easyci.yml'])
        self.assertEqual(self.git(repo, 'log', '-1', '--format=%s'), 'fixture')  # nothing committed yet
        self.assertIn(str(repo), [p['repo'] for p in dashboard.projects()])
        code, doc, _ = self.call('setup', '--repo', repo, '--commit')
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc['data']['written'], [])
        self.assertEqual(self.git(repo, 'log', '-1', '--format=%s'), 'Add EasyCI checks')
        self.assertEqual(self.git(repo, 'show', '--name-only', '--format=', 'HEAD').split(),
                         ['.github/workflows/ci.yml', 'easyci.yml'])
        self.assertEqual(self.call('setup', '--repo', repo)[1]['data']['uncommitted'], [])
