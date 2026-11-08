"""WSL backend command construction with WSL faked out (nothing is executed in any distro)."""
import pathlib
from unittest import mock

from ecsupport import EasyCITest
from easyci.backends import wsl


class FakeWsl(EasyCITest):
    def backend(self):
        b = wsl.Wsl.__new__(wsl.Wsl)
        b.distro, b.id, b._home = 'test-distro', 'wsl:test-distro', '/home/u'
        return b

    def test_step_runs_in_its_own_session_and_env_crosses_by_wslenv(self):
        job_dir = self.tmp / 'jobs' / 'j'
        with mock.patch('subprocess.Popen') as popen:
            popen.return_value.pid = 4242
            proc = self.backend().start('bash', 'echo "$SECRET_TOKEN"\n', {'SECRET_TOKEN': 'v4lue', 'CI': 'true'},
                                        '/home/u/.easyci/work/r/j', job_dir, 3)
        args, kwargs = popen.call_args
        wrapper = job_dir / 'steps' / '3.wrap.sh'
        self.assertEqual(args[0], [str(self.tmp / 'no-wsl.exe'), '-d', 'test-distro', '--exec', 'setsid', '--wait', 'bash',
                                   wsl.to_linux(wrapper)])
        self.assertEqual(set(kwargs['env']['WSLENV'].split(':')), {'SECRET_TOKEN', 'CI'})
        text = wrapper.read_text()
        self.assertIn('echo $$ > ', text)
        self.assertIn("cd /home/u/.easyci/work/r/j || exit 1", text)
        self.assertIn('exec bash --noprofile --norc -eo pipefail ', text)
        for path in job_dir.rglob('*'):
            if path.is_file():
                self.assertNotIn('v4lue', path.read_text(), 'env values never touch disk')
        self.assertEqual(proc.info['pgid_file'], str(job_dir / 'steps' / '3.pgid'))

    def test_kill_signals_the_process_group(self):
        pgid = self.tmp / 'x.pgid'
        pgid.write_text('777\n')
        calls = []
        with mock.patch.object(wsl, 'wsl_run', side_effect=lambda d, s, timeout=0: calls.append(s) or (1, '')):
            wsl.kill_group('test-distro', str(pgid))
        self.assertTrue(calls[0].startswith('kill -TERM -- -777'))
        self.assertTrue(calls[1].startswith('kill -0 -- -777'))

    def test_end_job_ends_every_step_group_of_the_job(self):
        # A service started with `&` in step 1 must not survive the job (review finding).
        b, job_dir = self.backend(), self.tmp / 'jobs' / 'j'
        with mock.patch('subprocess.Popen') as popen:
            popen.return_value.pid = 1
            for index in (1, 2):
                b.start('bash', 'true\n', {}, '/w', job_dir, index)
        for index, pgid in ((1, '701'), (2, '702')):
            (job_dir / 'steps' / f'{index}.pgid').write_text(pgid + '\n')
        calls = []
        with mock.patch.object(wsl, 'wsl_run', side_effect=lambda d, s, timeout=0: calls.append(s) or (1, '')):
            b.end_job()
            b.end_job()  # idempotent: the groups are forgotten once ended
        self.assertEqual([c.split(' 2>')[0] for c in calls if c.startswith('kill -TERM')],
                         ['kill -TERM -- -701', 'kill -TERM -- -702'])

    def test_paths_and_distro_listing(self):
        self.assertEqual(wsl.to_linux(pathlib.Path(r'C:\Users\a b\EasyCI\x.bundle')), '/mnt/c/Users/a b/EasyCI/x.bundle')
        sample = ('  NAME                     STATE           VERSION\n* ubn                      Stopped         2\n'
                  '  juliaflow-ci-ubuntu24    Running         2\n')
        with mock.patch.object(wsl, 'run_quiet', return_value=(0, sample)):
            found = wsl.distros()
        self.assertEqual(found, [{'name': 'ubn', 'state': 'Stopped', 'version': '2', 'default': True},
                                 {'name': 'juliaflow-ci-ubuntu24', 'state': 'Running', 'version': '2', 'default': False}])


class Collector(EasyCITest):
    def tree(self, *files):
        root = self.tmp / 'ws'
        for rel in files:
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(rel)
        return root

    def collect(self, root, patterns, layout='artifact'):
        from easyci.backends import collect
        dest = self.tmp / f'out-{len(list(self.tmp.glob("out-*")))}'
        return sorted(collect(root, patterns, dest, layout)), dest

    def test_exclusions_are_applied(self):
        root = self.tree('dist/app.js', 'dist/private.txt', 'dist/keys/id.pem', 'dist/sub/x.js')
        got, dest = self.collect(root, ['dist/**', '!dist/private.txt', '!dist/keys'])
        self.assertEqual(got, ['app.js', 'sub/x.js'])
        self.assertFalse((dest / 'private.txt').exists())
        got, _ = self.collect(root, ['dist', '!**/*.js'])
        self.assertEqual(got, ['keys/id.pem', 'private.txt'])

    def test_same_basenames_in_different_folders_stay_distinct(self):
        root = self.tree('a/result.json', 'b/result.json', 'a/deep/m.csv')
        got, dest = self.collect(root, ['a/result.json', 'b/result.json'])
        self.assertEqual(got, ['a/result.json', 'b/result.json'])
        self.assertEqual((dest / 'b/result.json').read_text(), 'b/result.json')
        self.assertEqual(self.collect(root, ['a/result.json'])[0], ['result.json'])  # single file: its own name
        self.assertEqual(self.collect(root, ['a/deep', 'a/result.json'])[0], ['deep/m.csv', 'result.json'])
        self.assertEqual(self.collect(root, ['a/deep'], layout='workspace')[0], ['a/deep/m.csv'])

    def test_unsafe_patterns_are_ignored(self):
        root = self.tree('ok.txt')
        self.assertEqual(self.collect(root, ['../x', '/etc/passwd', '', 'ok.txt'])[0], ['ok.txt'])
