"""Opt-in checks against the real host: EASYCI_INTEGRATION=1 (plus EASYCI_TEST_DISTRO for WSL runs).

They use real WSL and the real `gh` login, so they never run by default. Nothing here posts a
GitHub status unless EASYCI_TEST_GH_REPO names a repository you own (owner/repo).
"""
import json
import os
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

    def test_toolchain_verified_in_distro(self):  # criterion 13, wsl half
        wanted = os.environ.get('EASYCI_TEST_NODE_MAJOR', '25')
        text = ('on: push\njobs:\n  stub:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/setup-node@v4\n'
                f'        with: {{node-version: {wanted}}}\n      - run: node --version\n')
        run_id = self.start_wsl(self.make_repo(text))
        code, data = self.wait(run_id, timeout=180)
        self.assertEqual(code, 0, json.dumps(data))
