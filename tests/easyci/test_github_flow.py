"""Phase 2 GitHub flow: `easyci watch` queues new PR heads; the optional PR comment is kept up to date."""
import json
import os

from ecsupport import EasyCITest

QUICK = 'on: push\njobs:\n  quick:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n'
REMOTE = 'https://github.com/owner/repo.git'


def pull(number, sha, login='fake-user', repo='owner/repo', base='b' * 40):
    return {'number': number, 'state': 'open', 'user': {'login': login},
            'head': {'sha': sha, 'ref': f'branch-{number}', 'repo': {'full_name': repo} if repo else None},
            'base': {'sha': base, 'ref': 'main'}}


class Watch(EasyCITest):
    def test_new_heads_of_own_pull_requests_are_queued_once(self):
        repo = self.make_repo(QUICK, {'easyci.yml': 'backend: native\n'}, remote=REMOTE)
        head = self.git(repo, 'rev-parse', 'HEAD')
        os.environ['FAKE_GH_PULLS'] = json.dumps([pull(1, head, base=head), pull(2, head, repo='someone/fork'),
                                                  pull(3, head, login='colleague')])
        code, doc, _ = self.call('watch', '--repo', repo, '--once', '--no-report')
        self.assertEqual(code, 0, doc)
        started, skipped = doc['data']['started'], doc['data']['skipped']
        self.assertEqual([s['pr'] for s in started], [1])
        self.assertEqual({s['pr']: s['skipped'] for s in skipped},
                         {2: 'from a fork; never run automatically', 3: 'opened by colleague, not you'})
        code, data = self.wait(started[0]['run_id'])
        self.assertEqual((code, data['state'], data['event']), (0, 'passed', 'pull_request'))
        # Nothing new: nothing queued again.
        code, doc, _ = self.call('watch', '--repo', repo, '--once', '--no-report')
        self.assertEqual((doc['data']['started'], doc['data']['skipped']), ([], []))
        # A new commit on the PR is queued; --all-authors also takes the colleague's (never the fork's).
        (self.tmp / 'repo' / 'more.txt').write_text('x')
        self.git(repo, 'add', '-A')
        self.git(repo, 'commit', '-qm', 'more')
        new = self.git(repo, 'rev-parse', 'HEAD')
        os.environ['FAKE_GH_PULLS'] = json.dumps([pull(1, new, base=head), pull(2, new, repo='someone/fork'),
                                                  pull(3, new, login='colleague', base=head)])
        code, doc, _ = self.call('watch', '--repo', repo, '--once', '--no-report', '--all-authors')
        self.assertEqual(sorted(s['pr'] for s in doc['data']['started']), [1, 3])
        self.assertEqual([s['pr'] for s in doc['data']['skipped']], [2])
        for item in doc['data']['started']:
            self.wait(item['run_id'])

    def test_unknown_user_runs_nothing_unless_all_authors(self):
        # Review finding: a failed `gh api user` used to drop the author filter and run everyone's PRs.
        repo = self.make_repo(QUICK, {'easyci.yml': 'backend: native\n'}, remote=REMOTE)
        head = self.git(repo, 'rev-parse', 'HEAD')
        os.environ['FAKE_GH_PULLS'] = json.dumps([pull(3, head, login='colleague', base=head)])
        os.environ['FAKE_GH_USER_FAILS'] = '1'
        code, doc, _ = self.call('watch', '--repo', repo, '--once', '--no-report')
        self.assertEqual((code, doc['error']['code']), (3, 'GH_FAILED'))
        code, doc, _ = self.call('watch', '--repo', repo, '--once', '--no-report', '--all-authors')
        self.assertEqual([s['pr'] for s in doc['data']['started']], [3])
        self.wait(doc['data']['started'][0]['run_id'])

    def test_watch_needs_a_github_remote(self):
        repo = self.make_repo(QUICK)
        code, doc, _ = self.call('watch', '--repo', repo, '--once')
        self.assertEqual((code, doc['error']['code']), (2, 'NO_GITHUB_REMOTE'))


class PrComment(EasyCITest):
    def test_one_comment_per_pull_request_updated_in_place(self):
        failing = QUICK.replace('echo hi', 'echo broken-thing; exit 1')
        repo = self.make_repo(failing, {'easyci.yml': 'backend: native\npr_comment: true\n'}, remote=REMOTE)
        head = self.git(repo, 'rev-parse', 'HEAD')
        store_file = self.tmp / 'comments.json'
        os.environ.update(FAKE_GH_COMMENTS=str(store_file),
                          FAKE_GH_COMMIT_PULLS=json.dumps([{'number': 7, 'state': 'open', 'head': {'sha': head}}]))
        for _ in range(2):
            run = self.start(repo)
            code, data = self.wait(run['run_id'])
            self.assertEqual(data['state'], 'failed')
        comments = json.loads(store_file.read_text())
        self.assertEqual(len(comments), 1, comments)  # updated, not duplicated
        body = comments[0]['body']
        self.assertIn('<!-- easyci:pr-comment -->', body)
        self.assertIn('Checks failed', body)
        self.assertIn('- `quick`: failed', body)
        self.assertIn('broken-thing', body)
        self.assertIn(run['run_id'], body)  # the second run's id: the comment shows the latest run

    def test_no_comment_unless_enabled(self):
        repo = self.make_repo(QUICK, {'easyci.yml': 'backend: native\n'}, remote=REMOTE)
        store_file = self.tmp / 'comments.json'
        os.environ.update(FAKE_GH_COMMENTS=str(store_file),
                          FAKE_GH_COMMIT_PULLS=json.dumps([{'number': 7, 'state': 'open',
                                                            'head': {'sha': self.git(repo, 'rev-parse', 'HEAD')}}]))
        self.wait(self.start(repo)['run_id'])
        self.assertFalse(store_file.exists())
