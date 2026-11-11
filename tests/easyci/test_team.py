"""Phase 4: two machines report on the same commit without overwriting each other's checks."""
import json
import os

from ecsupport import EasyCITest

QUICK = 'on: push\njobs:\n  web:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n'
REMOTE = 'https://github.com/owner/repo.git'


class TwoMachines(EasyCITest):
    def contexts(self):
        posted = {}
        for line in self.gh_log.read_text().splitlines():
            args = json.loads(line)
            if 'api' in args and any('/statuses/' in a for a in args):
                fields = dict(a.split('=', 1) for a in args if '=' in a and not a.startswith('repos/'))
                posted[fields['context']] = fields['state']
        return posted

    def test_machine_labels_keep_contexts_apart(self):
        repo = self.make_repo(QUICK, {'easyci.yml': 'backend: native\nmachine_label: true\n'}, remote=REMOTE)
        for machine in ('pc-a', 'pc-b'):  # each "machine" is its own EasyCI home with its own name
            home = self.tmp / f'home-{machine}'
            home.mkdir()
            (home / 'config.yml').write_text(f'machine_name: {machine}\n')
            os.environ['EASYCI_HOME'] = str(home)
            run = self.start(repo)
            code, _ = self.wait(run['run_id'])
            self.assertEqual(code, 0)
        self.assertEqual(self.contexts(), {'easyci/web@pc-a': 'success', 'easyci@pc-a': 'success',
                                           'easyci/web@pc-b': 'success', 'easyci@pc-b': 'success'})

    def test_labels_are_off_by_default_and_names_are_sanitised(self):
        from easyci import github
        self.assertEqual(github.machine_suffix({}, {}), '')
        self.assertEqual(github.machine_suffix({}, {'machine_label': True, 'machine_name': 'My PC (2)'}), '@My-PC-2')
        self.assertTrue(github.machine_suffix({'machine_label': True}, {}).startswith('@'))
