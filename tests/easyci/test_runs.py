"""Real runs through the detached supervisor and the native backend with tiny stub steps."""
import json
import os
import pathlib
import re
import subprocess
import sys
import time

from ecsupport import EasyCITest
from easyci import store

PIPELINE = """\
on: [push, pull_request]
jobs:
  build:
    runs-on: ubuntu-latest
    outputs:
      answer: ${{ steps.calc.outputs.answer }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with: {node-version: 25}
      - id: calc
        name: Compute
        run: |
          test -f data.txt
          echo "answer=42" >> "$GITHUB_OUTPUT"
          echo "FROM_ENV=carried" >> "$GITHUB_ENV"
          echo "## summary" >> "$GITHUB_STEP_SUMMARY"
          echo '{"accuracy": 0.9}' > easyci-metrics.json
      - run: exit 3
        continue-on-error: true
      - run: test "$FROM_ENV" = carried && test "$CI" = true && test -z "$GITHUB_ACTIONS" && test "$EASYCI" = 1
      - uses: actions/upload-artifact@v4
        if: always()
        with: {name: out, path: data.txt, if-no-files-found: error}
  use:
    needs: build
    runs-on: ubuntu-latest
    steps:
      - run: test "${{ needs.build.outputs.answer }}" = 42
  never:
    needs: build
    if: github.event_name == 'pull_request'
    runs-on: ubuntu-latest
    steps:
      - run: echo should-not-run
"""

FAILING = """\
on: push
jobs:
  web:
    runs-on: ubuntu-latest
    steps:
      - run: echo preparing
      - name: npm run test
        run: |
          echo "AssertionError: expected 1 to equal 2"
          exit 1
      - run: echo after-failure
  later:
    needs: web
    runs-on: ubuntu-latest
    steps:
      - run: echo never
"""

UNSUPPORTED = """\
on: push
jobs:
  image:
    runs-on: ubuntu-latest
    steps:
      - run: echo executed > "$RUNNER_TEMP/../../../marker"
      - uses: docker/build-push-action@v5
  node:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-node@v4
        with: {node-version: 25}
      - run: echo executed > "$RUNNER_TEMP/../../../marker"
"""


def sleeper(seconds, extra=''):
    return f"""\
on: push
jobs:
  slow:
    runs-on: ubuntu-latest
    steps:
      - run: |
          cat /proc/$$/winpid > "$RUNNER_TEMP/../pid" 2>/dev/null || echo $$ > "$RUNNER_TEMP/../pid"
          echo started
          sleep {seconds} &
          cat /proc/$!/winpid > "$RUNNER_TEMP/../sleep.pid" 2>/dev/null || echo $! > "$RUNNER_TEMP/../sleep.pid"
          wait $!
          echo finished
{extra}"""


class NativeRuns(EasyCITest):
    def test_pipeline_passes_with_outputs_env_artifacts_and_statuses(self):
        os.environ['EASYCI_FAKE_PROBES'] = json.dumps({'node': 'v25.9.0', 'git': 'git version 2.50'})
        repo = self.make_repo(PIPELINE, {'data.txt': 'payload\n'}, remote='https://github.com/owner/repo.git')
        cfg = self.tmp / 'outside.yml'
        cfg.write_text('report: true\n')
        code, doc, _ = self.call('run', '--repo', repo, '--backend', 'fake', '--event', 'push', '--config', cfg)
        self.assertEqual(code, 0, doc)
        data = doc['data']
        self.assertRegex(data['run_id'], r'^\d{8}-\d{6}-[0-9a-f]{4}$')
        self.assertIn(data['state'], ('queued', 'preparing', 'running'))
        self.assertIn(data['launch'], ('detached', 'detached-in-job', 'schtasks'))
        code, data = self.wait(data['run_id'])
        self.assertEqual(code, 0, data)
        self.assertEqual(data['state'], 'passed')
        jobs = {j['name']: j for j in data['jobs']}
        self.assertEqual((jobs['build']['state'], jobs['use']['state'], jobs['never']['state']),
                         ('passed', 'passed', 'skipped'))
        self.assertEqual(jobs['build']['metrics'], {'accuracy': 0.9})
        rd = self.run_dir(data['run_id'])
        self.assertEqual((rd / 'jobs/build/artifacts/out/data.txt').read_text(), 'payload\n')
        self.assertIn('## summary', (rd / 'jobs/build/summary.md').read_text())
        self.assertFalse((rd / 'workspace').exists(), 'workspace removed after the run')
        request = store.read_json(rd / 'request.json')
        self.assertEqual(request['config']['sha256'], store.digest(cfg))
        report = store.read_json(rd / 'report.json')
        self.assertEqual(report['reproducibility']['commit'], data['commit'])
        self.assertIn('.github/workflows/ci.yml', report['reproducibility']['workflows'])
        self.assertEqual(report['toolchains']['fake']['node'], 'v25.9.0')
        statuses = self.final_statuses()
        self.assertEqual({k: v[0] for k, v in statuses.items()},
                         {'easyci/build': 'success', 'easyci/use': 'success', 'easyci/never': 'success', 'easyci': 'success'})
        self.assertTrue(all(data['run_id'] in d for _, d in statuses.values()))
        self.assertTrue(all(f'repos/owner/repo/statuses/{data["commit"]}' in c for c in self.gh_calls()))
        self.assertTrue(data['github']['reported'])
        self.assertTrue(data['plain'].startswith('All checks passed in 0 minutes. GitHub shows green on commit'))
        code, doc, _ = self.call('verify', data['run_id'])
        self.assertEqual(code, 0)
        code, doc, _ = self.call('logs', data['run_id'], '--job', 'build', '--tail', 50)
        self.assertTrue(any('step 3/6: Compute' in line for line in doc['data']['lines']))
        code, doc, _ = self.call('list', '--repo', repo)
        self.assertEqual([r['run_id'] for r in doc['data']['runs']], [data['run_id']])

    def test_failure_is_explained_and_dependents_skip(self):
        repo = self.make_repo(FAILING)
        run = self.start(repo)
        code, data = self.wait(run['run_id'])
        self.assertEqual(code, 1)
        jobs = {j['name']: j for j in data['jobs']}
        self.assertEqual((jobs['web']['state'], jobs['web']['reason']), ('failed', 'step_failed'))
        self.assertEqual(jobs['later']['state'], 'skipped')
        self.assertTrue(data['plain'].startswith('Checks failed: web stopped at step 2 (`npm run test`).'))
        code, doc, _ = self.call('explain', run['run_id'])
        self.assertEqual(code, 0)
        info = doc['data']
        self.assertEqual((info['failing_job'], info['failing_step']['index']), ('web', 2))
        self.assertIn('AssertionError', info['summary'])
        self.assertTrue(any('test failed' in s for s in info['suggestions']))
        log = (self.run_dir(run['run_id']) / 'jobs/web/log.txt').read_text()
        self.assertNotIn('after-failure', log.replace('Run echo after-failure', ''))

    def test_unsupported_and_toolchain_mismatch_run_nothing(self):
        os.environ['EASYCI_FAKE_PROBES'] = json.dumps({'node': 'v26.3.0', 'git': 'git version 2.50'})
        repo = self.make_repo(UNSUPPORTED, remote='git@github.com:owner/repo.git')
        code, doc, _ = self.call('run', '--repo', repo, '--backend', 'fake')
        self.assertEqual(code, 0, doc)  # one job is statically unsupported, the other only at run time
        code, data = self.wait(doc['data']['run_id'])
        self.assertEqual(code, 4)
        jobs = {j['name']: j for j in data['jobs']}
        self.assertEqual((jobs['image']['state'], jobs['image']['reason']),
                         ('unsupported', 'unsupported_uses:docker/build-push-action@v5'))
        self.assertEqual((jobs['node']['state'], jobs['node']['reason']),
                         ('unsupported', 'toolchain_mismatch:node 26.3.0 != 25'))
        self.assertIn('asks for 25', jobs['node']['fix'])
        rd = self.run_dir(data['run_id'])
        self.assertFalse((rd / 'jobs').exists() and any((rd / 'jobs').rglob('marker')))
        self.assertFalse((rd / 'jobs' / 'image' / 'log.txt').exists(), 'no step of that job executed')
        self.assertEqual({k: v[0] for k, v in self.final_statuses().items()},
                         {'easyci/image': 'error', 'easyci/node': 'error', 'easyci': 'error'})

    def test_detached_supervisor_outlives_its_launcher(self):
        repo = self.make_repo(sleeper(30))
        launcher = self.subprocess_cli('run', '--repo', repo, '--backend', 'native', '--no-report', '--json')
        self.assertEqual(launcher.returncode, 0, launcher.stderr)
        data = json.loads(launcher.stdout)['data']  # the launching process has exited
        rd = self.run_dir(data['run_id'])
        self.until(lambda: (store.read_json(rd / 'run.json') or {}).get('state') == 'running')
        first = store.read_json(rd / 'heartbeat.json')
        time.sleep(2.5)
        second = store.read_json(rd / 'heartbeat.json')
        self.assertGreater(store.parse_iso(second['at']), store.parse_iso(first['at']))
        self.assertTrue(store.pid_alive(second['pid'], 'python'))
        self.assertEqual(store.read_json(rd / 'run.json')['launch'], data['launch'])
        # A plain `easyci status` prints one sentence with elapsed minutes.
        status = self.subprocess_cli('status', data['run_id'])
        self.assertEqual(len(status.stdout.strip().splitlines()), 1)
        self.assertRegex(status.stdout, r'^Your tests are running: slow \(job 1 of 1\), step 1 of 1, \d+ minutes? so far\.')
        # Queue: a second run waits; --no-queue refuses with exit 5.
        code, doc, _ = self.call('run', '--repo', repo, '--backend', 'native', '--no-report', '--no-queue')
        self.assertEqual((code, doc['error']['code']), (5, 'BUSY'))
        second_run = self.start(repo, '--no-report')
        sd = self.run_dir(second_run['run_id'])
        time.sleep(1)
        code, doc, _ = self.call('status', second_run['run_id'])
        self.assertEqual(doc['data']['state'], 'queued')
        self.assertEqual(doc['data']['plain'], 'Waiting for the previous run to finish (it is on job slow).')
        # Cancel the first: it ends cancelled, its process tree is gone, the second starts by itself.
        pid = int(self.until(lambda: (rd / 'jobs/slow/pid').exists() and (rd / 'jobs/slow/pid').read_text().strip()))
        code, doc, _ = self.call('cancel', data['run_id'])
        self.assertEqual((code, doc['data']['state']), (0, 'cancelled'))
        self.assertFalse(store.pid_alive(pid), 'step process killed')
        self.assertFalse(store.pid_alive(self.sleep_pid(rd)), 'whole process tree killed')
        self.until(lambda: store.read_json(sd / 'run.json')['state'] == 'running')
        code, doc, _ = self.call('cancel', second_run['run_id'])
        self.assertEqual((code, doc['data']['state']), (0, 'cancelled'))
        self.assertTrue(doc['data']['plain'].startswith('Stopped because you asked.'))
        code, _ = self.wait(second_run['run_id'])
        self.assertEqual(code, 6)

    def sleep_pid(self, rd):
        return int(self.until(lambda: (rd / 'jobs/slow/sleep.pid').exists()
                              and (rd / 'jobs/slow/sleep.pid').read_text().strip()))

    def test_stall_mark_then_kill_and_timeout(self):
        extra = '    timeout-minutes: 0.07\n'
        repo = self.make_repo(sleeper(30, extra), {'easyci.yml': 'jobs:\n  slow:\n    stall_minutes: 0.02\n'},
                              remote='https://github.com/owner/repo.git')
        run = self.start(repo)
        rd = self.run_dir(run['run_id'])
        self.until(lambda: store.read_json(rd / 'run.json').get('stalled'))
        code, doc, _ = self.call('status', run['run_id'])
        self.assertEqual((doc['data']['state'], doc['data']['stalled']), ('running', True))
        self.assertIn('but nothing printed for', doc['data']['plain'])
        code, data = self.wait(run['run_id'])
        self.assertEqual((code, data['state']), (6, 'timed_out'))
        self.assertEqual(data['jobs'][0]['reason'], 'timeout')
        self.assertTrue(data['plain'].startswith('slow ran longer than 0.07 minutes and was stopped.'))
        self.assertEqual({k: v[0] for k, v in self.final_statuses().items()}, {'easyci/slow': 'error', 'easyci': 'error'})
        self.assertFalse(store.pid_alive(int((rd / 'jobs/slow/pid').read_text().strip())))
        self.assertFalse(store.pid_alive(self.sleep_pid(rd)))

    def test_stall_kill_and_redaction(self):
        workflow = sleeper(30).replace('echo started', 'echo "secret is $MY_TOKEN"')
        repo = self.make_repo(workflow, {'easyci.yml': 'jobs:\n  slow:\n    stall_minutes: 0.03\n    stall_action: kill\n'
                                                       '    required_env: [MY_TOKEN]\n'})
        os.environ['MY_TOKEN'] = 'tok-Zx81-unique-value'
        run = self.start(repo, '--no-report')
        code, data = self.wait(run['run_id'])
        self.assertEqual((code, data['state'], data['jobs'][0]['reason']), (1, 'failed', 'stalled'))
        rd = self.run_dir(run['run_id'])
        self.assertIn('secret is ***', (rd / 'jobs/slow/log.txt').read_text())
        for path in rd.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'tok-Zx81-unique-value', path.read_bytes(), path)
        self.assertFalse(store.pid_alive(int((rd / 'jobs/slow/pid').read_text().strip())))
        self.assertFalse(store.pid_alive(self.sleep_pid(rd)))

    def test_on_kill_runs_after_cancel_and_stall_kill_but_not_after_a_pass(self):
        # A delegated runner (JuliaFlow) sets up resources outside EasyCI's process tree; on_kill must
        # get a chance to release them, in the same workspace, before evidence is collected.
        hook = ('    on_kill: |\n      echo "reason=$EASYCI_KILL_REASON job=$EASYCI_JOB sha=$EASYCI_SHA" > released.txt\n'
                '      echo releasing\n    evidence: [released.txt]\n')
        repo = self.make_repo(sleeper(60), {'easyci.yml': 'jobs:\n  slow:\n' + hook})
        run = self.start(repo, '--no-report')
        rd = self.run_dir(run['run_id'])
        log_path = rd / 'jobs/slow/log.txt'
        self.until(lambda: log_path.exists() and b'started' in log_path.read_bytes())
        code, doc, _ = self.call('cancel', run['run_id'])
        self.assertEqual(doc['data']['state'], 'cancelled')
        code, data = self.wait(run['run_id'])
        released = (rd / 'jobs/slow/artifacts/evidence/released.txt').read_text().strip()
        self.assertEqual(released, f'reason=cancelled job=slow sha={data["commit"]}')
        job = store.read_json(rd / 'jobs/slow/job.json')
        self.assertEqual((job['state'], job['on_kill']['reason'], job['on_kill']['exit_code']),
                         ('cancelled', 'cancelled', 0))
        log = log_path.read_text()
        self.assertLess(log.index('cancelled on request'), log.index('running on_kill cleanup'))
        self.assertIn('releasing', log)
        self.assertFalse(store.pid_alive(self.sleep_pid(rd)))
        self.assertEqual(self.call('verify', run['run_id'])[0], 0)
        # Stall kill: the hook runs with that reason, and a failing hook does not change the outcome.
        repo = self.make_repo(sleeper(60), {'easyci.yml': 'jobs:\n  slow:\n    stall_minutes: 0.03\n'
                                            '    stall_action: kill\n    on_kill: echo "$EASYCI_KILL_REASON"; exit 3\n'},
                              name='stall')
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((data['state'], data['jobs'][0]['reason']), ('failed', 'stalled'))
        self.assertEqual((data['jobs'][0]['on_kill']['reason'], data['jobs'][0]['on_kill']['exit_code']),
                         ('stalled', 3))
        # A passing job never runs it.
        repo = self.make_repo(sleeper(1), {'easyci.yml': 'jobs:\n  slow:\n' + hook}, name='pass')
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'))
        self.assertNotIn('on_kill', data['jobs'][0])
        self.assertNotIn('on_kill', (self.run_dir(data['run_id']) / 'jobs/slow/log.txt').read_text())

    def test_secrets_reach_later_steps_unmasked_but_never_persist(self):
        # Review findings: a secret forwarded through $GITHUB_ENV / $GITHUB_OUTPUT must arrive intact
        # (masking is for evidence, not execution), and a secret interpolated into a step script by
        # ${{ }} must not stay in jobs/<job>/steps/.
        workflow = """\
on: push
jobs:
  sec:
    runs-on: ubuntu-latest
    outputs:
      tok: ${{ steps.s1.outputs.out }}
    steps:
      - id: s1
        run: |
          echo "NEXT=$MY_TOKEN" >> "$GITHUB_ENV"
          echo "out=$MY_TOKEN" >> "$GITHUB_OUTPUT"
      - name: Check ${{ env.NEXT }}
        run: |
          test "$NEXT" = "$MY_TOKEN"
          test "${{ env.NEXT }}" = "$MY_TOKEN"
          test "${{ steps.s1.outputs.out }}" = "$MY_TOKEN"
          echo "checked ${{ env.NEXT }}"
"""
        repo = self.make_repo(workflow, {'easyci.yml': 'jobs:\n  sec:\n    required_env: [MY_TOKEN]\n'})
        os.environ['MY_TOKEN'] = 'tok-Qv27-unique-value'
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        rd = self.run_dir(data['run_id'])
        self.assertIn('checked ***', (rd / 'jobs/sec/log.txt').read_text())
        self.assertEqual(data['jobs'][0]['steps_done'], 2)
        self.assertEqual(store.read_json(rd / 'jobs/sec/job.json')['steps'][1]['name'], 'Check ***')
        self.assertIn('***', (rd / 'jobs/sec/steps/2.sh').read_text())
        for path in rd.rglob('*'):
            if path.is_file():
                self.assertNotIn(b'tok-Qv27-unique-value', path.read_bytes(), path)
        self.assertEqual(self.call('verify', data['run_id'])[0], 0)

    def test_working_directory_expression_is_resolved(self):
        workflow = ('on: push\njobs:\n  wd:\n    runs-on: ubuntu-latest\n    env: {SUBDIR: sub}\n    steps:\n'
                    '      - uses: actions/checkout@v4\n'
                    '      - working-directory: ${{ env.SUBDIR }}\n        run: test -f data.txt\n')
        repo = self.make_repo(workflow, {'sub/data.txt': 'x\n'})
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)

    def test_multiline_secret_lines_are_masked(self):
        workflow = 'on: push\njobs:\n  ml:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo "$MY_PASSWORD"\n'
        repo = self.make_repo(workflow, {'easyci.yml': 'jobs:\n  ml:\n    required_env: [MY_PASSWORD]\n'})
        os.environ['MY_PASSWORD'] = 'first-line-x91\nsecond-line-y82'
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        rd = self.run_dir(data['run_id'])
        self.assertIn('***\n***', (rd / 'jobs/ml/log.txt').read_text())
        for path in rd.rglob('*'):
            if path.is_file():
                for part in (b'first-line-x91', b'second-line-y82'):
                    self.assertNotIn(part, path.read_bytes(), path)

    def test_output_without_newlines_is_not_a_stall(self):
        workflow = ('on: push\njobs:\n  dots:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - run: for i in $(seq 1 40); do printf .; sleep 0.1; done; echo done\n')
        repo = self.make_repo(workflow, {'easyci.yml': 'jobs:\n  dots:\n    stall_minutes: 0.02\n    stall_action: kill\n'})
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        self.assertIn('.' * 40 + 'done', (self.run_dir(data['run_id']) / 'jobs/dots/log.txt').read_text())

    def test_checkout_outside_the_workspace_is_refused(self):
        outside = self.tmp / 'precious'
        outside.mkdir()
        (outside / 'keep.txt').write_text('mine')
        for number, path in enumerate((outside.as_posix(), '../precious', 'sub/../../x')):
            with self.subTest(path=path):
                workflow = ('on: push\njobs:\n  co:\n    runs-on: ubuntu-latest\n    steps:\n'
                            f'      - uses: actions/checkout@v4\n        with: {{path: "{path}"}}\n')
                repo = self.make_repo(workflow, name=f'co{number}')
                code, data = self.wait(self.start(repo, '--no-report')['run_id'])
                self.assertEqual((code, data['state']), (1, 'failed'), data)
                self.assertEqual((outside / 'keep.txt').read_text(), 'mine')
                log = (self.run_dir(data['run_id']) / 'jobs/co/log.txt').read_text()
                self.assertIn('must be a relative path inside the workspace', log)

    def test_exception_text_is_masked(self):
        # A secret used as a working directory that does not exist makes the step fail to start;
        # the error text holds the resolved path and must be masked everywhere it is written.
        workflow = ('on: push\njobs:\n  ex:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - run: echo "NEXT=$MY_TOKEN" >> "$GITHUB_ENV"\n'
                    '      - working-directory: ${{ env.NEXT }}\n        run: echo never\n')
        repo = self.make_repo(workflow, {'easyci.yml': 'jobs:\n  ex:\n    required_env: [MY_TOKEN]\n'})
        os.environ['MY_TOKEN'] = 'tok-Wd53-unique-value'
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual(data['state'], 'failed', data)
        self.assertTrue(data['jobs'][0]['reason'].startswith('internal_error:'), data)
        rd = self.run_dir(data['run_id'])
        for path in [*rd.rglob('*'), self.home / 'easyci.log']:
            if path.is_file():
                self.assertNotIn(b'tok-Wd53-unique-value', path.read_bytes(), path)

    def test_passed_through_variables_are_in_the_env_context(self):
        workflow = ('on: push\njobs:\n  pt:\n    runs-on: ubuntu-latest\n    steps:\n'
                    "      - if: env.RUN_TESTS == 'yes'\n        name: tests ${{ env.RUN_TESTS }}\n        run: exit 42\n")
        repo = self.make_repo(workflow, {'easyci.yml': 'jobs:\n  pt:\n    required_env: [RUN_TESTS]\n'})
        os.environ['RUN_TESTS'] = 'yes'
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (1, 'failed'), data)
        self.assertEqual(data['jobs'][0]['failed_step']['name'], 'tests yes')
        # Job env still wins over the passed-through value.
        repo = self.make_repo(workflow.replace('    steps:', '    env: {RUN_TESTS: no}\n    steps:'),
                              {'easyci.yml': 'jobs:\n  pt:\n    required_env: [RUN_TESTS]\n'}, name='override')
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)

    def test_job_outputs_see_the_last_steps_exports(self):
        workflow = ('on: push\njobs:\n  a:\n    runs-on: ubuntu-latest\n    outputs:\n      run_tests: ${{ env.RUN_TESTS }}\n'
                    '    steps:\n      - run: echo "RUN_TESTS=true" >> "$GITHUB_ENV"\n'
                    '  b:\n    needs: a\n    if: needs.a.outputs.run_tests == \'true\'\n    runs-on: ubuntu-latest\n'
                    '    steps:\n      - run: exit 42\n')
        code, data = self.wait(self.start(self.make_repo(workflow), '--no-report')['run_id'])
        jobs = {j['name']: j['state'] for j in data['jobs']}
        self.assertEqual((code, jobs), (1, {'a': 'passed', 'b': 'failed'}), data)

    def test_job_env_can_use_resolved_workflow_env(self):
        workflow = ('on: push\nenv:\n  EVENT: ${{ github.event_name }}\njobs:\n  ev:\n    runs-on: ubuntu-latest\n'
                    '    env:\n      RUN_EVENT: ${{ env.EVENT }}\n    steps:\n'
                    "      - if: env.RUN_EVENT == 'push'\n        run: exit 42\n"
                    '      - if: false\n        run: exit 43\n')
        code, data = self.wait(self.start(self.make_repo(workflow), '--event', 'push', '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (1, 'failed'), data)
        self.assertEqual(data['jobs'][0]['failed_step']['index'], 1)

    def test_step_env_is_visible_to_its_own_if(self):
        workflow = ('on: push\njobs:\n  se:\n    runs-on: ubuntu-latest\n    steps:\n'
                    "      - env: {DO_TEST: 'yes'}\n        if: env.DO_TEST == 'yes'\n        run: exit 42\n")
        code, data = self.wait(self.start(self.make_repo(workflow), '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (1, 'failed'), data)

    def test_artifact_inputs_see_step_env(self):
        workflow = ('on: push\njobs:\n  ar:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - run: echo kept > result.txt\n'
                    '      - uses: actions/upload-artifact@v4\n        env: {ARTIFACT_PATH: result.txt}\n'
                    '        with: {name: res, path: "${{ env.ARTIFACT_PATH }}", if-no-files-found: error}\n')
        code, data = self.wait(self.start(self.make_repo(workflow), '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        self.assertEqual((self.run_dir(data['run_id']) / 'jobs/ar/artifacts/res/result.txt').read_text().strip(), 'kept')

    def test_skipped_dependency_is_not_a_failure(self):
        workflow = ('on: push\njobs:\n  deploy:\n    if: false\n    runs-on: ubuntu-latest\n    steps: [{run: exit 0}]\n'
                    '  rollback:\n    needs: deploy\n    if: failure()\n    runs-on: ubuntu-latest\n    steps: [{run: exit 42}]\n'
                    '  cleanup:\n    needs: deploy\n    if: always()\n    runs-on: ubuntu-latest\n    steps: [{run: exit 0}]\n')
        code, data = self.wait(self.start(self.make_repo(workflow), '--no-report')['run_id'])
        states = {j['name']: j['state'] for j in data['jobs']}
        self.assertEqual(states, {'deploy': 'skipped', 'rollback': 'skipped', 'cleanup': 'passed'}, data)
        self.assertEqual(code, 0)

    def test_disabled_setup_step_does_not_block_the_job(self):
        os.environ['EASYCI_FAKE_PROBES'] = json.dumps({'node': 'v26.3.0'})
        workflow = ('on: push\njobs:\n  tc:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - uses: actions/setup-node@v4\n        if: false\n        with: {node-version: 25}\n'
                    '      - run: echo tests ran\n')
        code, data = self.wait(self.start(self.make_repo(workflow), '--backend', 'fake', '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        # Enabled at run time (by an env value) the same setup step is enforced and fails the job.
        enabled = workflow.replace('if: false', "if: env.WANT == 'yes'").replace('    steps:', "    env: {WANT: 'yes'}\n    steps:")
        code, data = self.wait(self.start(self.make_repo(enabled, name='on'), '--backend', 'fake', '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (1, 'failed'), data)
        log = (self.run_dir(data['run_id']) / 'jobs/tc/log.txt').read_text()
        self.assertIn('toolchain_mismatch:node 26.3.0 != 25', log)

    def test_checkout_path_sees_step_env(self):
        workflow = ('on: push\njobs:\n  co:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - uses: actions/checkout@v4\n        env: {SOURCE_DIR: source}\n'
                    '        with: {path: "${{ env.SOURCE_DIR }}"}\n'
                    '      - run: test -f source/data.txt\n')
        code, data = self.wait(self.start(self.make_repo(workflow, {'data.txt': 'x\n'}), '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)

    def test_waits_for_a_busy_external_runner(self):
        import pathlib
        os.environ['EASYCI_BUSY_POLL_S'] = '0.2'
        workflow = 'on: push\njobs:\n  q:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ran\n'
        repo = self.make_repo(workflow, {'easyci.yml': 'busy_paths: [artifacts/*/summary.json]\n'})
        summary = pathlib.Path(repo) / 'artifacts' / 'manual-run' / 'summary.json'
        summary.parent.mkdir(parents=True)
        summary.write_text('{"status": "running"}')
        run = self.start(repo, '--no-report')
        rd = self.run_dir(run['run_id'])
        self.until(lambda: store.read_json(rd / 'run.json').get('waiting_for'))
        code, doc, _ = self.call('status', run['run_id'])
        self.assertEqual(doc['data']['state'], 'preparing')
        self.assertIn('artifacts/manual-run/summary.json', doc['data']['plain'])
        summary.write_text('{"status": "passed"}')
        code, data = self.wait(run['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)

    def test_matrix_runs_each_combination_with_fail_fast(self):
        workflow = ('on: push\njobs:\n  t:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix: {v: [1, 2, 3]}\n'
                    '    steps:\n      - run: echo "v=${{ matrix.v }} of ${{ strategy.job-total }}"; test ${{ matrix.v }} != 2\n'
                    '  after:\n    needs: t\n    if: always()\n    runs-on: ubuntu-latest\n'
                    "    steps:\n      - run: echo \"t was ${{ needs.t.result }}\"\n")
        code, data = self.wait(self.start(self.make_repo(workflow), '--no-report', '--backend', 'fake')['run_id'])
        states = {j['name']: (j['state'], j.get('reason')) for j in data['jobs']}
        self.assertEqual(states, {'t-1': ('passed', None), 't-2': ('failed', 'step_failed'),
                                  't-3': ('cancelled', 'fail_fast'), 'after': ('passed', None)}, data)
        rd = self.run_dir(data['run_id'])
        self.assertIn('v=1 of 3', (rd / 'jobs/t-1/log.txt').read_text())
        self.assertIn('t was failure', (rd / 'jobs/after/log.txt').read_text())
        # Without fail-fast every combination runs.
        repo = self.make_repo(workflow.replace('    strategy:\n', '    strategy:\n      fail-fast: false\n'), name='nofail')
        code, data = self.wait(self.start(repo, '--no-report', '--backend', 'fake')['run_id'])
        self.assertEqual({j['name']: j['state'] for j in data['jobs']},
                         {'t-1': 'passed', 't-2': 'failed', 't-3': 'passed', 'after': 'passed'})

    def test_cache_restore_save_and_prefix_fallback(self):
        workflow = ('on: push\njobs:\n  c:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - uses: actions/checkout@v4\n'
                    '      - id: cache\n        uses: actions/cache@v4\n'
                    "        with: {path: .deps, key: \"deps-${{ hashFiles('lock.txt') }}\", restore-keys: deps-}\n"
                    '      - run: |\n          echo "hit=${{ steps.cache.outputs.cache-hit }}"\n'
                    '          if [ -f .deps/built.txt ]; then echo "found $(cat .deps/built.txt)"; '
                    'else mkdir -p .deps; echo first > .deps/built.txt; echo built; fi\n')
        repo = self.make_repo(workflow, {'lock.txt': 'v1\n'})
        logs = []
        for change in (None, None, 'v2\n'):
            if change:
                (pathlib.Path(repo) / 'lock.txt').write_text(change)
                self.git(repo, 'commit', '-qam', 'bump lock')
            code, data = self.wait(self.start(repo, '--no-report', '--backend', 'fake')['run_id'])
            self.assertEqual((code, data['state']), (0, 'passed'), data)
            logs.append((self.run_dir(data['run_id']) / 'jobs/c/log.txt').read_text())
        self.assertIn('hit=false', logs[0])
        self.assertIn('built', logs[0])
        self.assertIn('hit=true', logs[1])          # exact key: cache-hit true, contents restored
        self.assertIn('found first', logs[1])
        self.assertIn('hit=false', logs[2])         # new key: restored from the deps- prefix
        self.assertIn('found first', logs[2])
        self.assertIn('restored from deps-', logs[2])
        entries = list((self.home / 'cache').rglob('*.tar'))
        self.assertEqual(len(entries), 2, entries)  # one entry per distinct key

    def test_download_artifact_between_jobs(self):
        workflow = ('on: push\njobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - run: mkdir -p out && echo payload > out/result.txt\n'
                    '      - uses: actions/upload-artifact@v4\n        with: {name: res, path: out}\n'
                    '  use:\n    needs: build\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - uses: actions/download-artifact@v4\n        with: {name: res, path: got}\n'
                    '      - run: test "$(cat got/result.txt)" = payload\n'
                    '      - uses: actions/download-artifact@v4\n        with: {name: missing}\n        continue-on-error: true\n')
        code, data = self.wait(self.start(self.make_repo(workflow), '--no-report', '--backend', 'fake')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        log = (self.run_dir(data['run_id']) / 'jobs/use/log.txt').read_text()
        self.assertIn('downloaded 1 artifact(s), 1 file(s)', log)
        self.assertIn('artifact missing was not uploaded earlier in this run', log)

    def test_github_path_reaches_later_steps(self):
        workflow = ('on: push\njobs:\n  p:\n    runs-on: ubuntu-latest\n    steps:\n'
                    '      - run: |\n          mkdir -p tools\n          printf \'#!/bin/sh\\necho from-mytool\\n\' > tools/mytool\n'
                    '          chmod +x tools/mytool\n          echo "$PWD/tools" >> "$GITHUB_PATH"\n'
                    '      - run: mytool\n')
        code, data = self.wait(self.start(self.make_repo(workflow), '--no-report', '--backend', 'fake')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        self.assertIn('from-mytool', (self.run_dir(data['run_id']) / 'jobs/p/log.txt').read_text())
        from easyci.backends.native import windows_path
        self.assertEqual((windows_path('/c/tools/bin'), windows_path('/d'), windows_path('rel')),
                         ('C:\\tools\\bin', 'D:\\', 'rel'))

    def test_checkout_path_expression_is_resolved(self):
        workflow = ('on: push\njobs:\n  co:\n    runs-on: ubuntu-latest\n    env: {SOURCE_DIR: source}\n    steps:\n'
                    '      - uses: actions/checkout@v4\n        with: {path: "${{ env.SOURCE_DIR }}"}\n'
                    '      - run: test -f source/data.txt\n')
        repo = self.make_repo(workflow, {'data.txt': 'x\n'})
        code, data = self.wait(self.start(repo, '--no-report')['run_id'])
        self.assertEqual((code, data['state']), (0, 'passed'), data)

    def test_killed_supervisor_is_reaped(self):
        repo = self.make_repo(sleeper(60), remote='https://github.com/owner/repo.git')
        run = self.start(repo)
        rd = self.run_dir(run['run_id'])
        child = int(self.until(lambda: (rd / 'jobs/slow/pid').exists() and (rd / 'jobs/slow/pid').read_text().strip()))
        sup = store.read_json(rd / 'heartbeat.json')['pid']
        if os.name == 'nt':
            subprocess.run(['taskkill', '/F', '/PID', str(sup)], capture_output=True)
        else:
            os.kill(sup, 9)
        time.sleep(float(os.environ['EASYCI_STALE_S']) + 1.5)
        code, doc, _ = self.call('status', run['run_id'])
        self.assertEqual(doc['data']['state'], 'interrupted')
        self.assertFalse(store.lock_dir().exists())
        self.assertFalse(store.pid_alive(child), 'leftover step tree killed by the reaper')
        self.assertFalse(store.pid_alive(self.sleep_pid(rd)))
        self.assertEqual(self.final_statuses()['easyci/slow'][0], 'error')
        self.assertEqual(self.final_statuses()['easyci'][0], 'error')
        code, doc, _ = self.call('wait', run['run_id'])
        self.assertEqual(code, 6)
