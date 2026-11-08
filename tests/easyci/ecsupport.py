"""Shared helpers: temp EASYCI_HOME, fake gh, temp git repos, CLI calls. No WSL, no real gh."""
import contextlib
import io
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
HERE = pathlib.Path(__file__).resolve().parent
FIXTURES = HERE / 'fixtures'
FAKE_GH = HERE / 'fake_gh.py'

from easyci import cli, store  # noqa: E402

GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_AUTHOR_NAME': 'T',
           'GIT_AUTHOR_EMAIL': 't@example.invalid', 'GIT_COMMITTER_NAME': 'T', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def rmtree(path):
    def onexc(func, target, _exc):
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=onexc)
    else:
        shutil.rmtree(path, onerror=onexc)


class EasyCITest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix='easyci-test-'))
        self.home = self.tmp / 'home'
        self.gh_log = self.tmp / 'gh.log'
        self.env = {'EASYCI_HOME': str(self.home), 'EASYCI_GH': str(FAKE_GH), 'FAKE_GH_LOG': str(self.gh_log),
                    'EASYCI_WSL': str(self.tmp / 'no-wsl.exe'), 'EASYCI_HEARTBEAT_S': '1', 'EASYCI_POLL_S': '0.1',
                    'EASYCI_STALE_S': '3', 'PYTHONPATH': str(ROOT), 'GIT_CEILING_DIRECTORIES': str(self.tmp), 'FAKE_GH_AUTH': '0', 'FAKE_GH_EXIT': '0'}
        patcher = mock.patch.dict(os.environ, self.env)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.stop_live_runs)

    def tearDown(self):
        pass

    def stop_live_runs(self):
        """Cancel runs whose real supervisor is still alive, wait for their seal, then delete everything."""
        live = []
        for rd in store.run_dirs():
            beat = store.read_json(rd / 'heartbeat.json') or {}
            if not (rd / 'sha256.json').exists() and store.pid_alive(beat.get('pid'), 'python')                     and beat.get('pid') != os.getpid():
                live.append(rd)
                try:
                    (rd / 'cancel').write_text('teardown')
                except OSError:
                    pass
        deadline = time.time() + 20
        while time.time() < deadline and any(not (rd / 'sha256.json').exists() for rd in live):
            time.sleep(0.2)
        rmtree(self.tmp)

    # ---- repositories ------------------------------------------------------------------
    def git(self, repo, *args):
        return subprocess.run(['git', *args], cwd=repo, env={**os.environ, **GIT_ENV}, check=True,
                              capture_output=True, text=True).stdout.strip()

    def make_repo(self, workflow, files=None, name='repo', remote=None):
        repo = self.tmp / name
        (repo / '.github' / 'workflows').mkdir(parents=True)
        (repo / '.github' / 'workflows' / 'ci.yml').write_bytes(workflow.encode())
        for path, text in (files or {}).items():
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_bytes(text.encode())
        self.git(repo, 'init', '-q', '-b', 'main')
        if remote:
            self.git(repo, 'remote', 'add', 'origin', remote)
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-q', '-m', 'fixture')
        return repo

    # ---- CLI ---------------------------------------------------------------------------
    def call(self, *args, cwd=None):
        """Run the CLI in-process with --json; return (exit code, document, stderr text)."""
        out, err = io.StringIO(), io.StringIO()
        old = os.getcwd()
        os.chdir(cwd or self.tmp)
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main([*map(str, args), '--json'])
        finally:
            os.chdir(old)
        text = out.getvalue()
        doc = json.loads(text)  # exactly one JSON document, or this raises
        self.assertEqual(doc['schema'], 'easyci/1')
        if code:
            self.assertFalse(doc['ok'])
            self.assertTrue({'code', 'message', 'fix'} <= set(doc['error']), doc)
        return code, doc, err.getvalue()

    def subprocess_cli(self, *args, cwd=None, extra_env=None, timeout=60):
        result = subprocess.run([sys.executable, '-m', 'easyci', *map(str, args)], cwd=cwd or self.tmp,
                                env={**os.environ, **(extra_env or {})}, capture_output=True, text=True,
                                timeout=timeout)
        return result

    def start(self, repo, *extra):
        code, doc, err = self.call('run', '--repo', repo, '--backend', 'native', *extra)
        self.assertIn(code, (0, 4), (doc, err))
        return doc['data']

    def wait(self, run_id, timeout=40):
        code, doc, _ = self.call('wait', run_id, '--timeout', timeout / 60)
        return code, doc['data']

    def run_dir(self, run_id):
        return store.find_run(run_id)

    def until(self, predicate, timeout=20, interval=0.2):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(interval)
        self.fail('condition not reached in time')

    def gh_calls(self):
        if not self.gh_log.exists():
            return []
        return [json.loads(line) for line in self.gh_log.read_text().splitlines()]

    def final_statuses(self):
        out = {}
        for args in self.gh_calls():
            fields = dict(a.split('=', 1) for a in args if '=' in a)
            out[fields['context']] = (fields['state'], fields['description'])
        return out
