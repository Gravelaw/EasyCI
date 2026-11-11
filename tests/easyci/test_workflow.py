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

    def test_release_candidate_dispatch_is_imported(self):
        # Phase 1: github.event.repository.default_branch is known, so this job now plans honestly.
        plan = workflow.build_plan(juliaflow(), {}, {**INP, 'event': 'workflow_dispatch', 'workflow': 'release-candidate.yml'})
        gate = jobs(plan)['debian-release-gate']
        self.assertNotEqual(gate['predicted'], 'unsupported', gate.get('predicted_reason'))


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
                                        ('src/**', 'src/a/b', True), ('docs/?.md', 'docs/a.md', True),
                                        ('docs/?.md', 'docs/ab.md', False), ('a.b', 'aXb', False)):
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
                     'unsupported_checkout:ref other'}
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


class Matrix(unittest.TestCase):
    def plan(self, text, cfg=None, **inp):
        return workflow.build_plan(single(text), cfg or {}, {**INP, 'event': 'push', 'ref_name': 'main', **inp})

    def test_cartesian_product_with_include_and_exclude(self):
        text = """\
on: push
jobs:
  test:
    runs-on: ubuntu-latest
    strategy:
      matrix:
        os: [ubuntu, windows]
        node: [18, 20]
        exclude:
          - {os: windows, node: 18}
        include:
          - {os: ubuntu, experimental: true}
          - {os: macos, node: 22}
    steps:
      - run: echo ${{ matrix.os }} ${{ matrix.node }}
  after:
    needs: test
    runs-on: ubuntu-latest
    steps: [{run: echo done}]
"""
        plan = self.plan(text)
        ids = [j['id'] for j in plan['jobs']]
        # Ids and names list every matrix value, including ones an `include` entry added (as GitHub's names do).
        self.assertEqual(ids, ['test-ubuntu-18-true', 'test-ubuntu-20-true', 'test-windows-20', 'test-macos-22', 'after'])
        by = {j['id']: j for j in plan['jobs']}
        self.assertEqual(by['test-ubuntu-18-true']['matrix'], {'os': 'ubuntu', 'node': 18, 'experimental': True})
        self.assertNotIn('experimental', by['test-windows-20']['matrix'])
        self.assertEqual(by['test-ubuntu-20-true']['name'], 'test (ubuntu, 20, true)')
        self.assertEqual(by['test-macos-22']['strategy']['job-total'], 4)
        self.assertEqual({j['group'] for j in plan['jobs'][:4]}, {'test'})
        self.assertEqual(by['after']['needs'], ['test'])

    def test_include_follows_githubs_documented_example(self):
        # docs.github.com, "Expanding or adding matrix configurations".
        job = {'strategy': {'matrix': {'fruit': ['apple', 'pear'], 'animal': ['cat', 'dog'], 'include': [
            {'color': 'green'}, {'color': 'pink', 'animal': 'cat'}, {'fruit': 'apple', 'shape': 'circle'},
            {'fruit': 'banana'}, {'fruit': 'banana', 'animal': 'cat'}]}}}
        combos = [c for c, _ in workflow.expand_matrix(job, {})]
        self.assertEqual(combos, [
            {'fruit': 'apple', 'animal': 'cat', 'color': 'pink', 'shape': 'circle'},
            {'fruit': 'apple', 'animal': 'dog', 'color': 'green', 'shape': 'circle'},
            {'fruit': 'pear', 'animal': 'cat', 'color': 'pink'},
            {'fruit': 'pear', 'animal': 'dog', 'color': 'green'},
            {'fruit': 'banana'},
            {'fruit': 'banana', 'animal': 'cat'}])
        # Review finding: an entry with only extra keys applies to every combination, adding none.
        job = {'strategy': {'matrix': {'os': ['ubuntu', 'windows'], 'include': [{'color': 'green'}]}}}
        self.assertEqual([c for c, _ in workflow.expand_matrix(job, {})],
                         [{'os': 'ubuntu', 'color': 'green'}, {'os': 'windows', 'color': 'green'}])

    def test_matrix_ids_never_collide_with_other_jobs(self):
        # Review finding: matrix `t` with v: [1, 2] next to an ordinary job `t-1`.
        text = ('on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix: {v: [1, 2]}\n'
                '    steps: [{run: echo hi}]\n  t-1:\n    runs-on: ubuntu-latest\n    steps: [{run: echo plain}]\n'
                '  after:\n    needs: [t, t-1]\n    runs-on: ubuntu-latest\n    steps: [{run: echo done}]\n')
        jobs = self.plan(text)['jobs']
        ids = [j['id'] for j in jobs]
        self.assertEqual(len(ids), len(set(ids)), ids)
        self.assertEqual(sorted((j['group'], j['matrix'].get('v')) for j in jobs if j['group'] == 't'),
                         [('t', 1), ('t', 2)])
        self.assertIn('t-1', ids)
        self.assertEqual(ids[-1], 'after')

    def test_runs_on_from_the_matrix_and_fromjson(self):
        text = ('on: push\njobs:\n  t:\n    runs-on: ${{ matrix.os }}\n    strategy:\n'
                "      matrix: '${{ fromJSON(''{\"os\": [\"ubuntu-latest\", \"windows-latest\"]}'') }}'\n"
                '    steps: [{run: echo hi}]\n')
        plan = self.plan(text, default_backend='fake')
        self.assertEqual([(j['id'], j['backend']) for j in plan['jobs']],
                         [('t-ubuntu-latest', 'fake'), ('t-windows-latest', 'native')])

    def test_matrix_from_needs_outputs_is_unsupported(self):
        text = ('on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    strategy:\n'
                '      matrix: ${{ fromJSON(needs.a.outputs.m) }}\n    steps: [{run: echo hi}]\n')
        job = self.plan(text)['jobs'][0]
        self.assertEqual(job['predicted'], 'unsupported')
        self.assertTrue(job['predicted_reason'].startswith('unsupported_matrix:'), job['predicted_reason'])

    def test_profile_selects_every_combination(self):
        text = ('on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix: {v: [1, 2]}\n'
                '    steps: [{run: echo hi}]\n  other:\n    runs-on: ubuntu-latest\n    steps: [{run: echo hi}]\n')
        plan = self.plan(text, {'profiles': {'p': {'event': 'push', 'jobs': ['t']}}}, profile='p')
        self.assertEqual([j['id'] for j in plan['jobs']], ['t-1', 't-2'])
        self.assertTrue(all(j['reported'] for j in plan['jobs']))


class ServicesAndEnv(unittest.TestCase):
    TEXT = ('on: push\njobs:\n  db:\n    runs-on: ubuntu-latest\n    env: {A: from-job}\n'
            '    steps: [{run: echo hi}]\n')

    def plan(self, cfg_text, **inp):
        cfg = workflow.load_config(cfg_text)
        return workflow.build_plan(single(self.TEXT), cfg, {**INP, 'event': 'push', 'ref_name': 'main', **inp})

    def test_config_validation(self):
        workflow.load_config('jobs:\n  db:\n    services: {postgres: {}}\n    env: {URL: "${{ env.DATABASE_URL }}"}\n')
        workflow.load_config('jobs:\n  db:\n    services: {postgres: {version: 16}}\n')
        for text in ('jobs: {db: {services: {mysql: {}}}}', 'jobs: {db: {services: [postgres]}}',
                     'jobs: {db: {services: {postgres: {image: x}}}}', 'jobs: {db: {env: {"bad name": 1}}}',
                     'jobs: {db: {env: [A]}}'):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                workflow.load_config(text)

    def test_services_need_wsl_and_env_layers_stack(self):
        job = self.plan('jobs:\n  db:\n    services: {postgres: {}}\n    env: {A: from-easyci, B: "${{ env.A }}"}\n',
                        default_backend='wsl:test')['jobs'][0]
        self.assertEqual(job['predicted'], 'run')
        self.assertEqual(job['services'], {'postgres': {}})
        self.assertEqual([layer.get('A') for layer in job['env_layers']], [None, 'from-job', 'from-easyci'])
        native = self.plan('jobs:\n  db:\n    services: {postgres: {}}\n', backend='native')['jobs'][0]
        self.assertEqual(native['predicted'], 'unsupported')
        self.assertEqual(native['predicted_reason'], 'unsupported_services:postgres needs a wsl backend')
