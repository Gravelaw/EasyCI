"""`easyci register`: a project remembers an easyci.yml kept outside it and a profile, so runs started
without flags (the dashboard's Run checks button, the Desktop launcher, `easyci watch`) use them."""
import os
import pathlib
import subprocess
import sys

from ecsupport import EasyCITest
from easyci import dashboard, store

TWO = ('on: [push, pull_request]\njobs:\n'
       '  a:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo job-a\n'
       '  b:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo job-b\n')
OUTSIDE = ('backend: native\nreport: false\nprofiles:\n'
           '  only-a: {event: push, jobs: [a]}\n'
           '  pr-b: {event: pull_request, jobs: [b]}\n')


class Register(EasyCITest):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo(TWO)
        self.cfg = self.tmp / 'outside' / 'easyci.yml'
        self.cfg.parent.mkdir()
        self.cfg.write_text(OUTSIDE)

    def register(self, *args, code=0):
        got, doc, err = self.call('register', '--repo', self.repo, *args)
        self.assertEqual(got, code, (doc, err))
        return doc

    def planned(self, *args):
        code, doc, err = self.call('plan', '--repo', self.repo, *args)
        self.assertEqual(code, 0, (doc, err))
        data = doc['data']
        return data['profile'], data['event'], sorted(j['id'] for j in data['jobs']), data['easyci_yml']

    def test_saved_config_and_profile_are_used_when_a_run_chooses_none(self):
        data = self.register('--config', self.cfg, '--profile', 'only-a')['data']
        self.assertEqual((data['config'], data['profile']), (str(self.cfg.resolve()), 'only-a'))
        profile, event, jobs, cfg = self.planned()
        self.assertEqual((profile, event, jobs), ('only-a', 'push', ['a']))
        self.assertEqual(cfg['source'], str(self.cfg.resolve()))
        # Anything the command chooses itself wins over the saved profile.
        self.assertEqual(self.planned('--profile', 'pr-b')[:3], ('pr-b', 'pull_request', ['b']))
        self.assertEqual(self.planned('--job', 'b')[0], None)
        self.assertEqual(self.planned('--event', 'pull_request')[0], None)  # only-a would replace the event
        self.assertEqual(self.planned('--event', 'push')[0], 'only-a')     # same event: the profile still applies
        # Nothing was written into the repository.
        self.assertEqual(self.git(self.repo, 'status', '--porcelain'), '')

    def test_dashboard_run_checks_uses_them(self):
        self.register('--config', self.cfg, '--profile', 'only-a')
        _, body, _ = self.call_dashboard_projects()
        self.assertEqual((body['profile'], body['config']), ('only-a', str(self.cfg.resolve())))
        run = dashboard.start_run(str(self.repo))
        code, data = self.wait(run['run_id'])
        self.assertEqual((code, data['state'], data['profile']), (0, 'passed', 'only-a'), data)
        self.assertEqual([j['name'] for j in data['jobs']], ['a'])
        request = store.read_json(self.run_dir(run['run_id']) / 'request.json')
        self.assertEqual(request['config']['source'], str(self.cfg.resolve()))

    def call_dashboard_projects(self):
        projects = [p for p in dashboard.projects() if p['repo'] == str(self.repo)]
        self.assertEqual(len(projects), 1, dashboard.projects())
        return None, projects[0], None

    def test_profile_alone_checks_the_repository_config(self):
        doc = self.register('--profile', 'only-a', code=2)
        self.assertEqual(doc['error']['code'], 'CONFIG_INVALID')
        self.assertIn('profiles: none', doc['error']['message'])
        doc = self.register('--config', self.cfg, '--profile', 'nope', code=2)
        self.assertIn('profiles: only-a, pr-b', doc['error']['message'])
        self.assertEqual(self.planned()[0], None)  # a refused register saves nothing

    def test_bad_paths_clear_and_moved_config(self):
        doc = self.register('--config', self.tmp / 'missing.yml', code=2)
        self.assertEqual(doc['error']['code'], 'CONFIG_NOT_FOUND')
        bad = self.tmp / 'bad.yml'
        bad.write_text('jobs: {a: {when: sometimes}}\n')
        self.assertEqual(self.register('--config', bad, code=2)['error']['code'], 'CONFIG_INVALID')
        self.register('--config', self.cfg, '--profile', 'only-a')
        # Changing only the profile keeps the saved config; "" forgets the profile.
        self.assertEqual(self.register('--profile', 'pr-b')['data']['config'], str(self.cfg.resolve()))
        self.assertEqual(self.planned()[0], 'pr-b')
        self.assertIsNone(self.register('--profile', '')['data']['profile'])
        self.assertEqual(self.planned()[0], None)
        # A saved config that has gone says how to fix the saved path.
        self.register('--profile', 'only-a')
        self.cfg.rename(self.cfg.with_name('moved.yml'))
        code, doc, _ = self.call('plan', '--repo', self.repo)
        self.assertEqual((code, doc['error']['code']), (2, 'CONFIG_NOT_FOUND'))
        self.assertIn('easyci register --clear', doc['error']['fix'])
        data = self.register('--clear')['data']
        self.assertEqual((data['config'], data['profile']), (None, None))
        self.assertEqual(self.planned()[:3], (None, 'push', ['a', 'b']))

    def test_a_saved_profile_the_config_lost_is_reported(self):
        self.register('--config', self.cfg, '--profile', 'pr-b')
        self.cfg.write_text(OUTSIDE.replace('pr-b', 'pr-c'))
        code, doc, _ = self.call('plan', '--repo', self.repo)
        self.assertEqual((code, doc['error']['code']), (2, 'CONFIG_INVALID'))
        self.assertIn('easyci register --profile', doc['error']['fix'])

    def test_desktop_launchers_run_this_easyci_without_an_install(self):
        desktop = self.tmp / 'desktop'
        desktop.mkdir()
        os.environ['EASYCI_DESKTOP'] = str(desktop)
        data = self.register('--config', self.cfg, '--desktop')['data']
        root = str(pathlib.Path(__file__).resolve().parents[2])
        for key in ('launcher', 'dashboard_launcher'):
            text = pathlib.Path(data[key]).read_text()
            self.assertEqual(pathlib.Path(data[key]).parent, desktop)
            self.assertIn(f'set "PYTHONPATH={root};%PYTHONPATH%"', text)
        self.assertIn(f'cd /d "{self.repo}"', pathlib.Path(data['launcher']).read_text())
        # That PYTHONPATH is enough for `python -m easyci` from any folder.
        env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
        env['PYTHONPATH'] = root
        out = subprocess.run([sys.executable, '-m', 'easyci', '--version'], cwd=self.tmp, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('easyci', out.stdout)
