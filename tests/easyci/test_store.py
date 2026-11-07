import os
import re
import stat
import subprocess
import sys
import time

from ecsupport import EasyCITest
from easyci import explain, github, store, supervisor

PLAIN = re.compile(r'^(Your tests are running|Waiting|Getting a clean copy|All checks passed|Checks failed|EasyCI could not'
                   r'|.* ran longer than|Stopped|The run was interrupted)')


def fabricate(run_id, state='running', pid=None, beat_age=100, github_enabled=True, jobs=None):
    rd = store.project_dir('proj-000000') / 'runs' / run_id
    rd.mkdir(parents=True)
    now = time.time()
    run = {'run_id': run_id, 'state': state, 'commit': 'c' * 40, 'base': 'b' * 40, 'created_at': store.iso(now - 200),
           'started_at': store.iso(now - 190), 'finished_at': None, 'launch': 'detached', 'project': 'proj-000000',
           'jobs': jobs or [{'name': 'web', 'state': 'running', 'reported': True, 'steps_total': 3, 'current_step': 2},
                            {'name': 'julia', 'state': 'pending', 'reported': True}],
           'github': {'enabled': github_enabled, 'repo': 'owner/repo', 'contexts': {}}}
    store.write_json(rd / 'run.json', run)
    store.write_json(rd / 'request.json', {'options': {}, 'repro': {'commit': run['commit']}})
    store.write_json(rd / 'heartbeat.json', {'at': store.iso(now - beat_age), 'pid': pid})
    (rd / 'jobs' / 'web').mkdir(parents=True)
    (rd / 'jobs' / 'web' / 'log.txt').write_text('line 1\n')
    return rd


class SealAndVerify(EasyCITest):
    def test_seal_covers_every_file_and_detects_tampering(self):
        rd = fabricate('20260101-000000-aaaa', state='passed')
        store.seal(rd)
        inventory = store.read_json(rd / 'sha256.json')['files']
        expected = {p.relative_to(rd).as_posix() for p in rd.rglob('*') if p.is_file() and p.name != 'sha256.json'}
        self.assertEqual(set(inventory), expected)
        self.assertEqual(store.verify(rd), [])
        code, doc, _ = self.call('verify', rd.name)
        self.assertEqual((code, doc['data']['ok']), (0, True))
        log = rd / 'jobs' / 'web' / 'log.txt'
        self.assertFalse(os.access(log, os.W_OK), 'sealed files are read-only')
        os.chmod(log, stat.S_IWRITE)
        with open(log, 'ab') as handle:
            handle.write(b'x')
        code, doc, _ = self.call('verify', rd.name)
        self.assertEqual(code, 1)
        self.assertEqual(doc['error']['code'], 'TAMPERED')
        self.assertIn('jobs/web/log.txt', doc['error']['message'])
        self.assertEqual(doc['data']['mismatches'], [{'path': 'jobs/web/log.txt', 'problem': 'changed'}])


class Reaper(EasyCITest):
    def test_dead_supervisor_is_marked_interrupted_and_releases_the_lock(self):
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        rd = fabricate('20260101-000000-bbbb', pid=dead.pid)
        self.assertTrue(store.try_lock(rd.name, dead.pid))
        store.add_ticket('20260101-000000-zzzz')  # ticket of a run that no longer exists
        code, doc, _ = self.call('status', rd.name)
        self.assertEqual(code, 0)
        self.assertEqual(doc['data']['state'], 'interrupted')
        self.assertTrue(PLAIN.match(doc['data']['plain']))
        self.assertFalse(store.lock_dir().exists())
        self.assertEqual(store.tickets(), [])
        self.assertTrue((rd / 'sha256.json').exists())
        self.assertEqual(store.verify(rd), [])
        statuses = self.final_statuses()
        self.assertEqual({k: v[0] for k, v in statuses.items()},
                         {'easyci/web': 'error', 'easyci/julia': 'error', 'easyci': 'error'})

    def test_live_or_fresh_runs_are_left_alone(self):
        alive = fabricate('20260101-000000-cccc', pid=os.getpid(), beat_age=100)
        fresh = fabricate('20260101-000000-dddd', pid=None, beat_age=0)
        store.reap()
        self.assertEqual(store.read_json(alive / 'run.json')['state'], 'running')
        self.assertEqual(store.read_json(fresh / 'run.json')['state'], 'running')

    def test_reporting_failure_is_recorded_not_fatal(self):
        os.environ['FAKE_GH_EXIT'] = '1'
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        dead.wait()
        rd = fabricate('20260101-000000-eeee', pid=dead.pid)
        store.reap()
        run = store.read_json(rd / 'run.json')
        self.assertEqual(run['state'], 'interrupted')
        self.assertFalse(run['github']['reported'])
        self.assertEqual(set(run['github']['failed']), {'easyci', 'easyci/web', 'easyci/julia'})
        self.assertIn('not yet; run `easyci report', explain.plain(run))
        os.environ['FAKE_GH_EXIT'] = '0'
        code, doc, _ = self.call('report', rd.name)
        self.assertEqual(code, 0, doc)
        self.assertEqual(doc['data']['failed'], [])
        self.assertEqual(store.verify(rd), [], 'report re-seals')


class Queue(EasyCITest):
    def test_lock_is_exclusive_and_busy_is_reported(self):
        self.assertIsNone(store.busy())
        self.assertTrue(store.try_lock('r1', 1))
        self.assertFalse(store.try_lock('r2', 2))
        self.assertEqual(store.busy(), 'r1')
        store.release_lock('r2')
        self.assertTrue(store.lock_dir().exists(), 'only the owner releases')
        store.release_lock('r1')
        self.assertIsNone(store.busy())


class Sentences(EasyCITest):
    def run_obj(self, state, **extra):
        jobs = [{'name': 'web', 'state': 'passed', 'steps_total': 8},
                {'name': 'julia', 'state': 'running', 'steps_total': 2, 'current_step': 2, 'timeout_minutes': 360,
                 'started_at': store.iso(time.time() - 300), 'last_output_at': store.iso(time.time() - 1400)},
                {'name': 'gate', 'state': 'pending', 'timeout_minutes': 180}]
        run = {'run_id': 'r', 'state': state, 'commit': 'abc1234def', 'jobs': jobs,
               'started_at': store.iso(time.time() - 744), 'github': {'enabled': True, 'reported': True,
                                                                     'contexts': {'easyci': 'x'}}}
        run.update(extra)
        return run

    def test_every_state_has_one_plain_line(self):
        self.assertEqual(explain.plain(self.run_obj('running')),
                         'Your tests are running: julia (job 2 of 3), step 2 of 2, 12 minutes so far.')
        stalled = self.run_obj('running')
        stalled['jobs'][1]['stalled'] = True
        self.assertIn('nothing printed for 23 minutes', explain.plain(stalled))
        self.assertIn('stop it at 360 minutes', explain.plain(stalled))
        self.assertEqual(explain.plain(self.run_obj('queued'), behind_job='julia'),
                         'Waiting for the previous run to finish (it is on job julia).')
        self.assertEqual(explain.plain(self.run_obj('preparing')), 'Getting a clean copy of your code ready.')
        done = self.run_obj('passed', finished_at=store.iso())
        self.assertEqual(explain.plain(done), 'All checks passed in 12 minutes. GitHub shows green on commit abc1234.')
        failed = self.run_obj('failed')
        failed['jobs'][1].update(state='failed', failed_step={'index': 2, 'name': 'npm run test'})
        self.assertTrue(explain.plain(failed).startswith('Checks failed: julia stopped at step 2 (`npm run test`).'))
        unsup = self.run_obj('unsupported')
        unsup['jobs'][2].update(state='unsupported', reason='unsupported_uses:docker/build-push-action@v5')
        self.assertTrue(explain.plain(unsup).startswith('EasyCI could not run gate because the workflow uses '
                                                        'docker/build-push-action@v5, which EasyCI does not support yet.'))
        timed = self.run_obj('timed_out')
        timed['jobs'][2]['state'] = 'timed_out'
        self.assertTrue(explain.plain(timed).startswith('gate ran longer than 180 minutes and was stopped.'))
        self.assertTrue(explain.plain(self.run_obj('cancelled')).startswith('Stopped because you asked.'))
        self.assertTrue(explain.plain(self.run_obj('interrupted')).startswith('The run was interrupted'))
        for state in store.LIVE + store.TERMINAL:
            line = explain.plain(self.run_obj(state))
            self.assertTrue(PLAIN.match(line) and '\n' not in line, line)


class GitHubStatuses(EasyCITest):
    def test_remote_parsing(self):
        for url in ('https://github.com/Gravelaw/EasyCI.git', 'git@github.com:Gravelaw/EasyCI.git',
                    'ssh://git@github.com/Gravelaw/EasyCI', 'https://user@github.com/Gravelaw/EasyCI/'):
            self.assertEqual(github.parse_remote(url), 'Gravelaw/EasyCI', url)
        self.assertIsNone(github.parse_remote('https://gitlab.com/a/b.git'))
        self.assertIsNone(github.parse_remote(None))

    def test_desired_contexts_and_descriptions(self):
        run = {'run_id': '20261107-141205-7f3a', 'state': 'failed', 'commit': 'c' * 40, 'reason': None,
               'started_at': store.iso(time.time() - 60), 'finished_at': store.iso(),
               'jobs': [{'name': 'web', 'state': 'passed', 'reported': True, 'elapsed_s': 842, 'backend': 'native'},
                        {'name': 'julia', 'state': 'failed', 'reported': True, 'steps_total': 8,
                         'failed_step': {'index': 5, 'name': 'npm run test ' + 'x' * 200}},
                        {'name': 'policy', 'state': 'skipped', 'reported': False}]}
        out = github.desired(run)
        self.assertEqual([c for c, _, _ in out], ['easyci/web', 'easyci/julia', 'easyci'])
        self.assertEqual([s for _, s, _ in out], ['success', 'failure', 'failure'])
        self.assertTrue(out[0][2].startswith('passed in 14m 02s · run 20261107-141205-7f3a'))
        self.assertTrue(all(len(d) <= 140 and (run['run_id'] in d or d.endswith('...')) for _, _, d in out))


class Redaction(EasyCITest):
    def test_values_and_token_shapes_are_masked(self):
        sup = supervisor.Supervisor.__new__(supervisor.Supervisor)
        sup.secrets = [b'hunter2-value']
        line = b'token=hunter2-value gh=ghp_' + b'A' * 36 + b' ok'
        self.assertEqual(sup.redact(line), b'token=*** gh=*** ok')

    def test_parse_kv_handles_heredoc(self):
        text = 'a=1\nmulti<<EOF\nline1\nline2\nEOF\nb=x=y\n'
        self.assertEqual(supervisor.parse_kv(text), {'a': '1', 'multi': 'line1\nline2', 'b': 'x=y'})
