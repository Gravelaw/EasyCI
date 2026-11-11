"""Opt-in checks against the real host: EASYCI_INTEGRATION=1 (plus EASYCI_TEST_DISTRO for WSL runs).

They use real WSL and the real `gh` login, so they never run by default. Nothing here posts a
GitHub status unless EASYCI_TEST_GH_REPO names a repository you own (owner/repo).
"""
import json
import os
import time
import unittest

from ecsupport import EasyCITest
from easyci import store

ENABLED = os.environ.get('EASYCI_INTEGRATION') == '1'
DISTRO = os.environ.get('EASYCI_TEST_DISTRO')


def stub(body, extra=''):
    return f'on: push\njobs:\n  stub:\n    runs-on: ubuntu-latest\n{extra}    steps:\n      - run: |\n' + \
        ''.join(f'          {line}\n' for line in body.splitlines())


@unittest.skipUnless(ENABLED, 'set EASYCI_INTEGRATION=1 to run real WSL/gh checks')
class RealHost(EasyCITest):
    def setUp(self):
        super().setUp()
        for name in ('EASYCI_GH', 'EASYCI_WSL', 'FAKE_GH_LOG'):
            os.environ.pop(name, None)

    def test_doctor_on_this_host(self):  # criterion 1 (read-only: gh auth status, wsl -l -v)
        code, doc, _ = self.call('doctor')
        checks = {c['id']: c for c in doc['data']['checks']}
        self.assertEqual(code, 0, [c for c in checks.values() if not c['ok']])
        for name in ('python', 'git', 'gh', 'wsl'):
            self.assertTrue(checks[name]['ok'], checks[name])
        self.assertIn(checks['docker']['detail'], ('docker: absent (optional)', 'docker: found (not needed)'))

    def test_run_survives_a_session_that_kills_its_job(self):  # criterion 3, the agent-session case
        # Reproduce how an agent harness ends: the launcher runs inside a Job object that forbids
        # breakaway and kills every process in it when its handle closes. EasyCI must detect that,
        # launch through Task Scheduler, and the run must finish after the job is gone.
        import subprocess
        import sys
        from easyci.backends import NO_WINDOW, WinJob
        repo = self.make_repo(stub('sleep 25; echo done'))
        job = WinJob()
        launcher = subprocess.Popen([sys.executable, '-m', 'easyci', 'run', '--repo', str(repo), '--backend', 'native',
                                     '--no-report', '--json'], cwd=str(self.tmp), env=dict(os.environ),
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                                    creationflags=NO_WINDOW | 0x4)  # CREATE_SUSPENDED until it is in the job
        self.assertTrue(job.adopt(launcher))
        out, err = launcher.communicate(timeout=120)
        run = json.loads(out)['data']
        self.assertEqual(run['launch'], 'schtasks', err)
        job.close()  # the session ends: everything still inside its job is killed now
        time.sleep(5)
        rd = store.find_run(run['run_id'])
        beat = store.read_json(rd / 'heartbeat.json') or {}
        self.assertLess(time.time() - store.parse_iso(beat['at']), 30, beat)
        code, data = self.wait(run['run_id'], timeout=180)
        self.assertEqual((code, data['state']), (0, 'passed'), data)
        self.assertGreaterEqual(data['jobs'][0]['elapsed_s'], 25, data)
        self.assertIn('done', (rd / 'jobs/stub/log.txt').read_text(encoding='utf-8'))

    def test_schtasks_launch_path(self):  # criterion 3, fallback path
        os.environ['EASYCI_LAUNCH'] = 'schtasks'
        repo = self.make_repo(stub('sleep 5'))
        run = self.start(repo, '--no-report')
        self.assertEqual(run['launch'], 'schtasks')
        code, data = self.wait(run['run_id'], timeout=120)
        self.assertEqual(code, 0, data)


@unittest.skipUnless(ENABLED and DISTRO, 'set EASYCI_INTEGRATION=1 and EASYCI_TEST_DISTRO=<a disposable distro>')
class RealWsl(EasyCITest):
    def setUp(self):
        super().setUp()
        os.environ.pop('EASYCI_WSL', None)
        self.backend = f'wsl:{DISTRO}'

    def test_postgres_service_as_a_non_root_default_user(self):
        # Review finding: runuser needs root. Run every service command as the distro's `nobody` user,
        # the way a distro whose default user is not root would, and check start, use and stop.
        from unittest import mock
        from easyci.backends import wsl
        real = wsl.wsl_run

        def as_nobody(distro, script, timeout=120):
            return wsl.run_quiet([wsl.wsl_exe(), '-d', distro, '-u', 'nobody', '--exec', 'bash', '-c', script],
                                 timeout=timeout, env={**os.environ, 'WSL_UTF8': '1'})
        backend, run_id = wsl.Wsl(DISTRO), f'nonroot-{os.getpid()}-{int(time.time())}'
        with mock.patch.object(wsl, 'wsl_run', as_nobody):
            env = backend.start_services({'postgres': {}}, run_id, 'j')
            try:
                d = backend.service_dir(run_id, 'j')
                code, text = as_nobody(DISTRO, f'PGPASSWORD={env["PGPASSWORD"]} "$(cat {d}/bindir)/psql" -h 127.0.0.1 '
                                               f'-p {env["PGPORT"]} -U easyci -d easyci -tAc "select 41 + 1"; '
                                               f'ps -o user= -p "$(head -1 {d}/pg/postmaster.pid)"')
                self.assertEqual((code, text.split()), (0, ['42', 'nobody']), text)
            finally:
                backend.stop_services(run_id, 'j')
                backend.cleanup(None, run_id)
        code, text = real(DISTRO, f'ls /var/tmp/easyci/{run_id} 2>/dev/null; pgrep -u nobody -f "[/]var/tmp/easyci/{run_id}"')
        self.assertEqual(text.strip(), '', 'the service left files or processes behind')

    def group_alive(self, rd, index=1):
        from easyci.backends import wsl
        pgid = (rd / 'jobs/stub/steps' / f'{index}.pgid').read_text().strip()
        code, _ = wsl.wsl_run(DISTRO, f'pgrep -g {pgid}')
        return code == 0

    def start_wsl(self, repo):
        code, doc, _ = self.call('run', '--repo', repo, '--backend', self.backend, '--no-report')
        self.assertEqual(code, 0, doc)
        return doc['data']['run_id']

    def test_sleep_survives_and_passes(self):  # criteria 2-3 on wsl
        run_id = self.start_wsl(self.make_repo(stub('sleep 20; echo done')))
        code, data = self.wait(run_id, timeout=180)
        self.assertEqual(code, 0, data)
        # Guard against a launcher that returns before the step runs (bare `setsid` forks).
        self.assertGreaterEqual(data['jobs'][0]['elapsed_s'], 20, data)
        log = (store.find_run(run_id) / 'jobs/stub/log.txt').read_text(encoding='utf-8')
        self.assertIn('done', log)

    def test_timeout_kills_the_process_group(self):  # criterion 9
        repo = self.make_repo(stub('echo hi; sleep 600', '    timeout-minutes: 1\n'))
        run_id = self.start_wsl(repo)
        code, data = self.wait(run_id, timeout=180)
        self.assertEqual((code, data['state']), (6, 'timed_out'))
        self.assertFalse(self.group_alive(store.find_run(run_id)))

    def test_stall_kill_and_cancel(self):  # criteria 8 and 10
        repo = self.make_repo(stub('echo hi; sleep 600'),
                              {'easyci.yml': 'jobs:\n  stub:\n    stall_minutes: 1\n    stall_action: kill\n'})
        run_id = self.start_wsl(repo)
        code, data = self.wait(run_id, timeout=180)
        self.assertEqual((data['state'], data['jobs'][0]['reason']), ('failed', 'stalled'))
        self.assertFalse(self.group_alive(store.find_run(run_id)))
        run_id = self.start_wsl(self.make_repo(stub('echo hi; sleep 600'), name='cancel'))
        self.until(lambda: (store.find_run(run_id) / 'jobs/stub/steps/1.pgid').exists(), timeout=120)
        code, doc, _ = self.call('cancel', run_id)
        self.assertEqual(doc['data']['state'], 'cancelled')
        self.assertFalse(self.group_alive(store.find_run(run_id)))

    def test_postgres_service_cache_and_env_override(self):  # Phase 1
        text = ('on: push\njobs:\n  stub:\n    runs-on: ubuntu-latest\n    steps:\n'
                '      - uses: actions/checkout@v4\n'
                '      - id: c\n        uses: actions/cache@v4\n        with: {path: .deps, key: "k-${{ hashFiles(\'lock.txt\') }}"}\n'
                '      - run: |\n'
                '          psql "$APP_DB" -tAc "select 40 + 2" | grep -x 42\n'
                '          echo "hit=${{ steps.c.outputs.cache-hit }}"\n'
                '          [ -f .deps/made ] && echo "cached $(cat .deps/made)" || { mkdir -p .deps; echo once > .deps/made; }\n')
        cfg = 'jobs:\n  stub:\n    services: {postgres: {}}\n    env: {APP_DB: "${{ env.DATABASE_URL }}"}\n'
        repo = self.make_repo(text, {'lock.txt': 'v1\n', 'easyci.yml': cfg})
        logs = []
        for _ in range(2):
            run_id = self.start_wsl(repo)
            code, data = self.wait(run_id, timeout=300)
            self.assertEqual((code, data['state']), (0, 'passed'), json.dumps(data))
            rd = store.find_run(run_id)
            logs.append((rd / 'jobs/stub/log.txt').read_text(encoding='utf-8'))
            import re
            for path in rd.rglob('*'):  # the generated database password never reaches the evidence
                if path.is_file():
                    self.assertIsNone(re.search(rb'postgresql://easyci:[0-9a-f]{32}@', path.read_bytes()), path)
        self.assertIn('services ready: postgres', logs[0])
        self.assertIn('hit=false', logs[0])
        self.assertIn('hit=true', logs[1])
        self.assertIn('cached once', logs[1])
        from easyci.backends import wsl
        code, _ = wsl.wsl_run(DISTRO, f'ls /var/tmp/easyci/{run_id} 2>/dev/null; pgrep -u postgres -f /var/tmp/easyci/{run_id}/')
        self.assertNotEqual(code, 0, 'service stopped and its folder removed')

    def test_toolchain_verified_in_distro(self):  # criterion 13, wsl half
        wanted = os.environ.get('EASYCI_TEST_NODE_MAJOR', '25')
        text = ('on: push\njobs:\n  stub:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/setup-node@v4\n'
                f'        with: {{node-version: {wanted}}}\n      - run: node --version\n')
        run_id = self.start_wsl(self.make_repo(text))
        code, data = self.wait(run_id, timeout=180)
        self.assertEqual(code, 0, json.dumps(data))
