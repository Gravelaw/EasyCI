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
        self.assertEqual(args[0], [str(self.tmp / 'no-wsl.exe'), '-d', 'test-distro', '--exec', 'setsid', 'bash',
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

    def test_paths_and_distro_listing(self):
        self.assertEqual(wsl.to_linux(pathlib.Path(r'C:\Users\a b\EasyCI\x.bundle')), '/mnt/c/Users/a b/EasyCI/x.bundle')
        sample = ('  NAME                     STATE           VERSION\n* ubn                      Stopped         2\n'
                  '  juliaflow-ci-ubuntu24    Running         2\n')
        with mock.patch.object(wsl, 'run_quiet', return_value=(0, sample)):
            found = wsl.distros()
        self.assertEqual(found, [{'name': 'ubn', 'state': 'Stopped', 'version': '2', 'default': True},
                                 {'name': 'juliaflow-ci-ubuntu24', 'state': 'Running', 'version': '2', 'default': False}])
