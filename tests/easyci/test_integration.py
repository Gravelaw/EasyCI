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


PODMAN = os.environ.get('EASYCI_TEST_PODMAN_DISTRO')


@unittest.skipUnless(ENABLED and PODMAN, 'set EASYCI_INTEGRATION=1 and EASYCI_TEST_PODMAN_DISTRO=<a distro with podman>')
class RealPodman(EasyCITest):  # Phase 3
    def setUp(self):
        super().setUp()
        os.environ.pop('EASYCI_WSL', None)

    def test_job_container_and_service(self):
        text = """\
on: push
jobs:
  inside:
    runs-on: ubuntu-latest
    container: docker.io/library/alpine:3.20
    services:
      cache:
        image: docker.io/library/redis:7-alpine
        options: --health-cmd "redis-cli ping" --health-interval 2s --health-timeout 2s --health-retries 15
    steps:
      - uses: actions/checkout@v4
      - id: probe
        run: |
          cat /etc/alpine-release
          test -f data.txt
          printf 'PING\\r\\n' | nc -w 3 cache 6379 | grep -q PONG && echo "service by name: ok"
          echo "where=$(cat /etc/alpine-release)" >> "$GITHUB_OUTPUT"
      - run: echo "output=${{ steps.probe.outputs.where }}"
  outside:
    runs-on: ubuntu-latest
    services:
      cache:
        image: docker.io/library/redis:7-alpine
        ports: ['6379']
        options: --health-cmd "redis-cli ping" --health-interval 2s --health-timeout 2s --health-retries 15
    steps:
      - run: |
          exec 3<>/dev/tcp/127.0.0.1/${{ job.services.cache.ports['6379'] }}
          printf 'PING\\r\\n' >&3; read -t 5 reply <&3; echo "reply=$reply"; [[ $reply == +PONG* ]]
"""
        repo = self.make_repo(text, {'data.txt': 'x\n', 'easyci.yml': f'container_backend: podman:{PODMAN}\n'})
        code, doc, _ = self.call('run', '--repo', repo, '--no-report')
        self.assertEqual(code, 0, doc)
        run_id = doc['data']['run_id']
        code, data = self.wait(run_id, timeout=900)  # first use pulls the images
        self.assertEqual((code, data['state']), (0, 'passed'), json.dumps(data))
        rd = store.find_run(run_id)
        inside = (rd / 'jobs/inside/log.txt').read_text(encoding='utf-8')
        self.assertIn('service by name: ok', inside)
        self.assertRegex(inside, r'output=3\.20')
        self.assertIn('reply=+PONG', (rd / 'jobs/outside/log.txt').read_text(encoding='utf-8'))
        from easyci.backends import wsl
        code, text = wsl.wsl_run(PODMAN, f'podman ps -a --format "{{{{.Names}}}}" | grep -c easyci-{run_id.lower()} || true')
        self.assertEqual(text.strip(), '0', 'containers removed')

    def containers(self, run_id):
        from easyci.backends import wsl
        code, text = wsl.wsl_run(PODMAN, f'podman ps -a --format "{{{{.Names}}}}" | grep -c easyci-{run_id.lower()} || true')
        return int(text.strip() or 0)

    def test_container_jobs_are_linux_jobs_with_hooks_and_toolchains_in_the_image(self):
        # Review findings: podman jobs were treated as Windows jobs (runner.os, paths, $GITHUB_PATH),
        # hooks ran after the container was gone, and setup-* steps were checked against the distro.
        text = """\
on: push
jobs:
  linux:
    runs-on: ubuntu-latest
    container: docker.io/library/alpine:3.20
    steps:
      - run: mkdir -p sub && echo /opt/a >> "$GITHUB_PATH" && echo /opt/b >> "$GITHUB_PATH"
      - if: runner.os == 'Linux'
        working-directory: sub
        run: echo "os=$RUNNER_OS dir=$(basename "$PWD") path=$PATH"
      - id: c
        uses: actions/cache@v4
        with: {path: ~/.npm, key: container-key}
      - run: echo "container-cache-hit=${{ steps.c.outputs.cache-hit }}"
      - working-directory: /etc/apk
        run: echo "inside=$PWD"
  tools:
    runs-on: ubuntu-latest
    container: docker.io/library/alpine:3.20
    steps:
      - uses: actions/setup-python@v5
        with: {python-version: '3.12'}
      - run: echo unreachable
"""
        cfg = (f'container_backend: podman:{PODMAN}\njobs:\n  linux:\n'
               '    cleanup: echo "cleanup-in-alpine-$(cat /etc/alpine-release)"\n')
        repo = self.make_repo(text, {'easyci.yml': cfg})
        code, doc, _ = self.call('run', '--repo', repo, '--no-report')
        run_id = doc['data']['run_id']
        code, data = self.wait(run_id, timeout=900)
        jobs = {j['name']: j for j in data['jobs']}
        rd = store.find_run(run_id)
        log = (rd / 'jobs/linux/log.txt').read_text(encoding='utf-8')
        self.assertEqual(jobs['linux']['state'], 'passed', log)
        self.assertRegex(log, r'os=Linux dir=sub path=/opt/b:/opt/a:')
        self.assertRegex(log, r'cleanup-in-alpine-3\.20')
        # Review finding: the distro's cache store is not the container's filesystem; always a miss there.
        self.assertIn('cache not used inside a job container', log)
        self.assertIn('container-cache-hit=false', log)
        self.assertIn("inside=/etc/apk", log)  # exists only in the alpine image, not in the distro
        # The distro has python3; the image does not. The image decides, at the step.
        self.assertEqual((jobs['tools']['state'], jobs['tools']['failed_step']['index']), ('failed', 1), jobs['tools'])
        self.assertIn('the job container image docker.io/library/alpine:3.20',
                      (rd / 'jobs/tools/log.txt').read_text(encoding='utf-8'))
        self.assertEqual(self.containers(run_id), 0, 'containers removed')

    def test_on_kill_runs_in_the_container_and_a_dead_supervisor_leaves_no_containers(self):
        text = ('on: push\njobs:\n  slow:\n    runs-on: ubuntu-latest\n    container: docker.io/library/alpine:3.20\n'
                '    steps:\n      - run: echo started; sleep 600\n')
        cfg = (f'container_backend: podman:{PODMAN}\njobs:\n  slow:\n'
               '    on_kill: echo "on_kill-in-alpine-$(cat /etc/alpine-release)"\n')
        repo = self.make_repo(text, {'easyci.yml': cfg})
        code, doc, _ = self.call('run', '--repo', repo, '--no-report')
        run_id = doc['data']['run_id']
        log = lambda: store.find_run(run_id) / 'jobs/slow/log.txt'  # noqa: E731
        self.until(lambda: log().exists() and 'started' in log().read_text(encoding='utf-8'), timeout=300)
        self.call('cancel', run_id)
        self.wait(run_id)
        self.assertRegex(log().read_text(encoding='utf-8'), r'on_kill-in-alpine-3\.20')
        self.assertEqual(self.containers(run_id), 0)
        # Supervisor killed: the reaper must stop the containers, not only mark the run.
        repo = self.make_repo(text, {'easyci.yml': f'container_backend: podman:{PODMAN}\n'}, name='orphan')
        code, doc, _ = self.call('run', '--repo', repo, '--no-report')
        run_id = doc['data']['run_id']
        self.until(lambda: log().exists() and 'started' in log().read_text(encoding='utf-8'), timeout=300)
        self.assertGreater(self.containers(run_id), 0)
        rd = store.find_run(run_id)
        import subprocess
        subprocess.run(['taskkill', '/F', '/PID', str(store.read_json(rd / 'heartbeat.json')['pid'])], capture_output=True)
        time.sleep(float(os.environ['EASYCI_STALE_S']) + 1.5)
        code, doc, _ = self.call('status', run_id)
        self.assertEqual(doc['data']['state'], 'interrupted')
        self.assertEqual(self.containers(run_id), 0, 'the reaper removed the orphaned containers')

    def test_cancel_stops_the_job_container(self):
        text = ('on: push\njobs:\n  slow:\n    runs-on: ubuntu-latest\n    container: docker.io/library/alpine:3.20\n'
                '    steps:\n      - run: echo started; sleep 600\n')
        repo = self.make_repo(text, {'easyci.yml': f'container_backend: podman:{PODMAN}\n'})
        code, doc, _ = self.call('run', '--repo', repo, '--no-report')
        run_id = doc['data']['run_id']
        log = lambda: store.find_run(run_id) / 'jobs/slow/log.txt'  # noqa: E731
        self.until(lambda: log().exists() and 'started' in log().read_text(encoding='utf-8'), timeout=300)
        code, doc, _ = self.call('cancel', run_id)
        self.assertEqual(doc['data']['state'], 'cancelled')
        from easyci.backends import wsl
        code, text = wsl.wsl_run(PODMAN, f'podman ps -a --format "{{{{.Names}}}}" | grep -c easyci-{run_id.lower()} || true')
        self.assertEqual(text.strip(), '0', 'containers removed after cancel')


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


@unittest.skipUnless(ENABLED and DISTRO, 'set EASYCI_INTEGRATION=1 and EASYCI_TEST_DISTRO=<a disposable distro>')
class RealWslCache(EasyCITest):
    def setUp(self):
        super().setUp()
        os.environ.pop('EASYCI_WSL', None)

    def test_wsl_cache_entries(self):
        from easyci import cache
        from easyci.backends import wsl
        from test_runs import exercise_cache
        backend = wsl.Wsl(DISTRO)
        ws = f'/tmp/easyci-cache-test-{os.getpid()}'
        c = cache.WslCache(backend, f'cachetest-{os.getpid()}')
        run = lambda script: wsl.wsl_run(DISTRO, script)  # noqa: E731
        try:
            run(f'rm -rf {ws} {ws}-new; mkdir -p {ws}')
            write = lambda rel, text: run(f'mkdir -p "$(dirname {ws}/{rel})"; printf %s {text} > {ws}/{rel}')  # noqa: E731
            read = lambda rel: run(f'cat {ws}/{rel}')[1]  # noqa: E731
            exercise_cache(self, c, ws, write, read, link=lambda a, b: run(f'ln {ws}/{a} {ws}/{b}'))
            self.assertEqual(run(f'cat {ws}-new/linked/b.txt')[1], 'shared')
        finally:
            run(f'rm -rf {ws} {ws}-new {c.dir}')
