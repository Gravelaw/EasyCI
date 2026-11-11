import unittest

from ecsupport import FIXTURES, ROOT
from easyci import workflow
from easyci.workflow import ConfigError

SHA, BASE = 'a' * 40, 'b' * 40
INP = {'event': 'pull_request', 'ref_name': 'feature', 'default_branch': 'main', 'commit': SHA, 'base': BASE,
       'repository': 'owner/repo'}


def juliaflow():
    return [('.github/workflows/ci.yml', (FIXTURES / 'juliaflow-ci.yml').read_bytes()),
            ('.github/workflows/release-candidate.yml', (FIXTURES / 'juliaflow-release-candidate.yml').read_bytes())]


def single(text):
    return [('.github/workflows/ci.yml', text.encode())]


def jobs(plan):
    return {j['id']: j for j in plan['jobs']}


class JuliaFlowImport(unittest.TestCase):
    def test_literal_reading_without_easyci_yml(self):
        plan = workflow.build_plan(juliaflow(), {}, INP)
        j = jobs(plan)
        self.assertEqual(list(j), ['execution-policy', 'web', 'julia', 'debian-release-gate'])
        self.assertEqual({k: v['run_steps'] for k, v in j.items()},
                         {'execution-policy': 1, 'web': 8, 'julia': 2, 'debian-release-gate': 3})
        self.assertEqual(j['execution-policy']['predicted'], 'run')
        self.assertIn('checkout:actions/checkout@v4', j['web']['mapped'])
        self.assertEqual(j['web']['toolchains'], [{'tool': 'node', 'requested': '25', 'step': 2, 'if': None}])
        self.assertEqual(j['julia']['toolchains'], [{'tool': 'julia', 'requested': '1.12', 'step': 2, 'if': None}])
        self.assertIn('upload-artifact:actions/upload-artifact@v4', j['debian-release-gate']['mapped'])
        # head.sha checkout ref resolves to the run commit, so it is mapped, not unsupported
        self.assertEqual(j['debian-release-gate']['steps'][0]['kind'], 'checkout')
        for name in ('web', 'julia', 'debian-release-gate'):
            self.assertEqual((j[name]['predicted'], j[name]['predicted_reason']), ('skipped', 'condition_false'))
        self.assertTrue(all(job['backend'] == 'wsl' for job in plan['jobs']))
        applies = {w['file']: w['applies'] for w in plan['workflows']}
        self.assertEqual(applies, {'.github/workflows/ci.yml': True, '.github/workflows/release-candidate.yml': False})

    def test_release_gate_if_is_evaluated_with_event_name(self):
        cfg = {'jobs': {'execution-policy': {'skip': True}}}
        plan = workflow.build_plan(juliaflow(), cfg, {**INP, 'event': 'push', 'ref_name': 'main'})
        self.assertEqual(jobs(plan)['debian-release-gate']['predicted'], 'skipped')

    def test_with_example_easyci_yml_jobs_are_selected_and_delegated(self):
        cfg = workflow.load_config((ROOT / 'examples/juliaflow/easyci.yml').read_text(encoding='utf-8'))
        plan = workflow.build_plan(juliaflow(), cfg, {**INP, 'profile': 'pr'})
        j = jobs(plan)
        self.assertEqual(j['execution-policy']['predicted'], 'skipped')
        self.assertFalse(j['execution-policy']['reported'])
        for name in ('web', 'julia', 'debian-release-gate'):
            self.assertEqual(j[name]['predicted'], 'run')
            self.assertTrue(j[name]['reported'])
            self.assertEqual(j[name]['backend'], 'native')
            self.assertEqual(len(j[name]['steps']), 1)
            self.assertIn(f'--job {name} --cache cold', j[name]['steps'][0]['run'])
            self.assertEqual(j[name]['toolchains'], [])
        self.assertEqual(j['debian-release-gate']['timeout_minutes'], 180)
        self.assertEqual(j['julia']['stall_minutes'], 45)

    def test_release_candidate_dispatch_is_unsupported_honestly(self):
        plan = workflow.build_plan(juliaflow(), {}, {**INP, 'event': 'workflow_dispatch', 'workflow': 'release-candidate.yml'})
        gate = jobs(plan)['debian-release-gate']
        self.assertEqual(gate['predicted'], 'unsupported')
        self.assertIn('github.event.repository.default_branch', gate['predicted_reason'])


class Mapping(unittest.TestCase):
    def plan(self, text, cfg=None, **inp):
        return workflow.build_plan(single(text), cfg or {}, {**INP, **inp})

    def test_paths_filters_use_the_changed_files(self):
        text = ('on:\n  push:\n    paths: ["src/**", "!src/docs/**"]\njobs:\n  t:\n    runs-on: ubuntu-latest\n'
                '    steps: [{run: x}]\n')
        run = lambda changed: len(self.plan(text, event='push', ref_name='main', changed=changed)['jobs'])  # noqa: E731
        self.assertEqual(run(['src/app.py']), 1)
        self.assertEqual(run(['README.md']), 0)
        self.assertEqual(run(['src/docs/guide.md']), 0)  # excluded again by the ordered `!` pattern
        self.assertEqual(run(None), 1)  # nothing to compare with: included, with a note
        ignore = text.replace('paths: ["src/**", "!src/docs/**"]', 'paths-ignore: ["**.md"]')
        plan = lambda changed: self.plan(ignore, event='push', ref_name='main', changed=changed)['jobs']  # noqa: E731
        self.assertEqual(plan(['a.md', 'docs/b.md']), [])
        self.assertEqual(len(plan(['a.md', 'src/x.py'])), 1)

    def test_globstar_slash_matches_zero_folders(self):
        # Review finding: `**/` must also match no folder at all, as on GitHub.
        for pattern, path, expected in (('**/*.py', 'main.py', True), ('**/*.py', 'a/b/main.py', True),
                                        ('src/**/*.py', 'src/main.py', True), ('src/**/*.py', 'src/a/main.py', True),
                                        ('src/**/*.py', 'srcx/main.py', False), ('*.py', 'a/main.py', False),
                                        ('src/**', 'src/a/b', True), ('a.b', 'aXb', False)):
            self.assertEqual(workflow.glob_match(pattern, path), expected, (pattern, path))

    def test_github_filter_pattern_syntax(self):
        # Review finding: GitHub's `?` means "zero or one of the preceding character", `+` "one or
        # more", and `[...]` is a character class (workflow syntax, filter pattern cheat sheet).
        for pattern, path, expected in (('*.jsx?', 'page.js', True), ('*.jsx?', 'page.jsx', True),
                                        ('*.jsx?', 'page.jsxx', False), ('v+.txt', 'vvv.txt', True),
                                        ('v+.txt', '.txt', False), ('[ab]x', 'ax', True), ('[ab]x', 'cx', False),
                                        ('[a-c]x', 'bx', True), ('[!a]x', 'bx', True), ('[!a]x', 'ax', False),
                                        ('releases/[0-9]+.0', 'releases/10.0', True), ('a+b', 'a+b', False)):
            self.assertEqual(workflow.glob_match(pattern, path), expected, (pattern, path))

    def test_changed_files_are_read_raw_not_quoted(self):
        # Review finding: git quotes non-ASCII names in its default output.
        import os
        import subprocess
        import tempfile
        from easyci import snapshot
        with tempfile.TemporaryDirectory() as tmp:
            git = lambda *a: subprocess.run(['git', '-C', tmp, '-c', 'user.name=t', '-c', 'user.email=t@t', *a],  # noqa: E731
                                            check=True, capture_output=True)
            git('init', '-q')
            git('commit', '-q', '--allow-empty', '-m', 'base')
            os.makedirs(os.path.join(tmp, 'src'))
            for name in ('café.py', 'über ü.py', ' lead.py'):  # Windows forbids tabs in names
                with open(os.path.join(tmp, 'src', name), 'w') as f:
                    f.write('x')
            git('add', '-A')
            git('commit', '-q', '-m', 'files')
            changed = snapshot.changed_files(tmp, 'HEAD~1', 'HEAD')
        self.assertEqual(sorted(changed), sorted(['src/café.py', 'src/über ü.py', 'src/ lead.py']))
        self.assertTrue(all(workflow.glob_match('src/**', name) for name in changed))

    def test_busy_settings_are_validated(self):
        workflow.load_config('busy_paths: [artifacts/*/summary.json]\nbusy_stale_minutes: 5\nbusy_wait_minutes: 30\n')
        for text in ('busy_paths: x', 'busy_paths: [""]', 'busy_stale_minutes: 0', 'busy_wait_minutes: -1'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                workflow.load_config(text)

    def test_unknown_action_marks_job_unsupported_with_its_name(self):
        plan = self.plan('on: push\njobs:\n  img:\n    runs-on: ubuntu-latest\n    steps:\n'
                         '      - uses: docker/build-push-action@v5\n      - run: echo never\n', event='push', ref_name='main')
        job = jobs(plan)['img']
        self.assertEqual(job['predicted'], 'unsupported')
        self.assertEqual(job['predicted_reason'], 'unsupported_uses:docker/build-push-action@v5')

    def test_runner_shell_and_keys(self):
        cases = {'runs-on: macos-latest\n    steps: [{run: x}]': 'unsupported_runs_on:macos-latest',
                 'runs-on: ubuntu-latest\n    services: {pg: {image: postgres}}\n    steps: [{run: x}]': 'unsupported_job_key:services',
                 'runs-on: ubuntu-latest\n    steps: [{run: x, shell: fish}]': 'unsupported_shell:fish',
                 'runs-on: ubuntu-latest\n    steps: [{uses: actions/setup-node@v4, with: {node-version: lts/*}}]':
                     'toolchain_spec_unsupported:node lts/*',
                 'runs-on: ubuntu-latest\n    steps: [{uses: actions/checkout@v4, with: {submodules: true}}]':
                     'unsupported_checkout:submodules',
                 'runs-on: ubuntu-latest\n    steps: [{uses: actions/checkout@v4, with: {ref: other}}]':
                     'unsupported_checkout:ref other',
                 'runs-on: ubuntu-latest\n    strategy: {matrix: {os: [a, b]}}\n    steps: [{run: x}]':
                     'unsupported_job_key:strategy (matrix is Phase 1)'}
        for body, reason in cases.items():
            with self.subTest(reason=reason):
                plan = self.plan('on: pull_request\njobs:\n  j:\n    ' + body + '\n')
                self.assertEqual(jobs(plan)['j']['predicted_reason'], reason)

    def test_windows_runs_native_with_pwsh_and_cache_is_noop(self):
        plan = self.plan('on: pull_request\njobs:\n  w:\n    runs-on: windows-latest\n    steps:\n'
                         '      - uses: actions/cache@v4\n        with: {path: x, key: k}\n      - run: dir\n')
        job = jobs(plan)['w']
        self.assertEqual(job['backend'], 'native')
        self.assertEqual([s['kind'] for s in job['steps']], ['cache', 'run'])
        self.assertEqual(job['steps'][1]['shell'], 'pwsh')

    def test_branch_filters_and_needs_order(self):
        text = ('on:\n  push:\n    branches: [main, "release/**"]\njobs:\n  b:\n    needs: a\n    runs-on: ubuntu-latest\n'
                '    steps: [{run: x}]\n  a:\n    runs-on: ubuntu-latest\n    steps: [{run: x}]\n')
        self.assertEqual([j['id'] for j in self.plan(text, event='push', ref_name='main')['jobs']], ['a', 'b'])
        self.assertEqual(self.plan(text, event='push', ref_name='release/1.0')['jobs'][0]['id'], 'a')
        self.assertEqual(self.plan(text, event='push', ref_name='feature')['jobs'], [])
        # Ordered `!` exclusions, as on GitHub (review finding).
        neg = text.replace('[main, "release/**"]', '["**", "!release/**", "release/keep"]')
        self.assertEqual(self.plan(neg, event='push', ref_name='release/beta')['jobs'], [])
        self.assertEqual(len(self.plan(neg, event='push', ref_name='feature/x')['jobs']), 2)
        self.assertEqual(len(self.plan(neg, event='push', ref_name='release/keep')['jobs']), 2)
        self.assertTrue(workflow.filter_match(['**', '!release/**'], 'main'))
        self.assertFalse(workflow.filter_match(['**', '!release/**'], 'release/beta'))
        with self.assertRaises(ConfigError):
            self.plan(text.replace('  a:\n    runs-on', '  a:\n    needs: b\n    runs-on'), event='push', ref_name='main')

    def test_overrides(self):
        text = ('on: pull_request\njobs:\n  j:\n    if: false\n    runs-on: ubuntu-latest\n    steps:\n'
                '      - uses: actions/setup-node@v4\n        with: {node-version: 25}\n'
                '      - name: flaky\n        run: x\n      - uses: some/unknown@v1\n')
        cfg = workflow.load_config('jobs:\n  j:\n    when: always\n    backend: native\n    skip_steps: [flaky, 3]\n'
                                   '    toolchains: {node: ignore}\n    stall_action: kill\n    stall_minutes: 2\n')
        job = jobs(self.plan(text, cfg))['j']
        self.assertEqual(job['predicted'], 'run')
        self.assertEqual([s['kind'] for s in job['steps']], ['toolchain', 'skipped', 'skipped'])
        self.assertEqual(job['toolchains'], [])
        self.assertEqual((job['backend'], job['stall_action'], job['stall_minutes']), ('native', 'kill', 2))
        self.assertEqual((job['on_kill'], job['on_kill_minutes']), (None, 10))
        cfg = workflow.load_config('jobs:\n  j:\n    on_kill: ./release.sh\n    on_kill_minutes: 3\n')
        job = jobs(self.plan(text, cfg))['j']
        self.assertEqual((job['on_kill'], job['on_kill_minutes']), ('./release.sh', 3))
        self.assertEqual(jobs(self.plan(text, cfg, backend='fake'))['j']['backend'], 'fake')

    def test_config_errors(self):
        for text in ('nope: 1', 'backend: docker', 'jobs: {j: {when: sometimes}}', 'jobs: {j: {timeout_minutes: 0}}',
                     'jobs: {j: {required_env: ["bad name"]}}', 'version: 2', ': [', 'jobs: {j: {on_kill: ""}}',
                     'jobs: {j: {on_kill: [x]}}', 'jobs: {j: {on_kill_minutes: 0}}'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                workflow.load_config(text)
        text = 'on: pull_request\njobs:\n  j:\n    runs-on: ubuntu-latest\n    steps: [{run: x}]\n'
        with self.assertRaises(ConfigError):
            self.plan(text, {'jobs': {'missing': {'skip': True}}})
        with self.assertRaises(ConfigError):
            self.plan(text, {}, profile='pr')

    def test_version_matching(self):
        self.assertTrue(workflow.version_matches('25', 'v25.9.0'))
        self.assertFalse(workflow.version_matches('25', 'v26.3.0'))
        self.assertTrue(workflow.version_matches('1.12', 'julia version 1.12.7'))
        self.assertFalse(workflow.version_matches('1.12.6', 'julia version 1.12.7'))
        self.assertTrue(workflow.version_matches('3.x', 'Python 3.14.6'))


if __name__ == '__main__':
    unittest.main()
