"""`easyci dashboard`: local-only page and JSON API; actions need the page token."""
import http.client
import json

from ecsupport import EasyCITest
from easyci import dashboard

QUICK = 'on: push\njobs:\n  quick:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hello-dashboard\n'


class Dashboard(EasyCITest):
    def setUp(self):
        super().setUp()
        self.url, self.server, self.token, _ = dashboard.start_in_thread()
        self.addCleanup(self.server.shutdown)
        self.port = int(self.url.rsplit(':', 1)[1])

    def request(self, method, path, body=None, token=True, host=None):
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        headers = {'Host': host or f'127.0.0.1:{self.port}'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
            if token:
                headers['X-EasyCI-Token'] = self.token if token is True else token
        conn.request(method, path, json.dumps(body) if body is not None else None, headers)
        resp = conn.getresponse()
        data = resp.read().decode('utf-8')
        conn.close()
        return resp.status, data

    def test_page_runs_and_actions(self):
        status, page = self.request('GET', '/')
        self.assertEqual(status, 200)
        self.assertIn(self.token, page)          # the page carries its token
        self.assertNotIn('http://', page.replace(self.url, ''))  # nothing loaded from elsewhere
        repo = self.make_repo(QUICK, {'easyci.yml': 'backend: native\nreport: false\n'})
        run = self.start(repo, '--no-report')     # makes the project known
        code, _ = self.wait(run['run_id'])
        self.assertEqual(code, 0)
        status, body = self.request('GET', '/api/projects')
        self.assertEqual(json.loads(body)['data']['projects'][0]['repo'], str(repo))
        status, body = self.request('GET', f'/api/runs/{run["run_id"]}')
        detail = json.loads(body)['data']
        self.assertEqual((detail['state'], detail['log_job']), ('passed', 'quick'))
        self.assertIn('hello-dashboard', detail['log'])
        # Starting a run needs the token and a known project.
        self.assertEqual(self.request('POST', '/api/run', {'repo': str(repo)}, token=False)[0], 403)
        self.assertEqual(self.request('POST', '/api/run', {'repo': str(repo)}, token='wrong')[0], 403)
        status, body = self.request('POST', '/api/run', {'repo': str(self.tmp)})
        self.assertEqual((status, json.loads(body)['error']['code']), (400, 'UNKNOWN_PROJECT'))
        status, body = self.request('POST', '/api/run', {'repo': str(repo)})
        self.assertEqual(status, 200, body)
        new = json.loads(body)['data']['run_id']
        code, data = self.wait(new)
        self.assertEqual((code, data['state']), (0, 'passed'))
        self.assertEqual(self.request('GET', '/api/runs/nope')[0], 404)

    def test_statuses_link_to_the_dashboard_when_configured(self):
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / 'config.yml').write_text('dashboard_url: http://127.0.0.1:8765/\n')
        repo = self.make_repo(QUICK, {'easyci.yml': 'backend: native\n'}, remote='https://github.com/owner/repo.git')
        run = self.start(repo)
        code, _ = self.wait(run['run_id'])
        self.assertEqual(code, 0)
        log = self.gh_log.read_text()
        self.assertIn(f'target_url=http://127.0.0.1:8765/runs/{run["run_id"]}', log)
        (self.home / 'config.yml').write_text('dashboard_url: https://example.com\n')  # only local URLs are used
        run = self.start(repo)
        self.wait(run['run_id'])
        self.assertNotIn('example.com', self.gh_log.read_text())

    def test_foreign_host_header_is_refused(self):
        # A page on another site that resolves its own name to 127.0.0.1 (DNS rebinding) is turned away.
        self.assertEqual(self.request('GET', '/api/runs', host='evil.example:80')[0], 403)
        self.assertEqual(self.request('POST', '/api/run', {'repo': 'x'}, host='evil.example')[0], 403)

    def test_refusals_read_the_body_first(self):
        # Refusing without reading the body made Windows reset the connection now and then,
        # so the client saw an abort instead of the 403. Repeat with a sizeable body.
        body = {'repo': 'x' * 30000}
        for _ in range(40):
            self.assertEqual(self.request('POST', '/api/run', body, host='evil.example')[0], 403)
            self.assertEqual(self.request('POST', '/api/run', body, token='wrong')[0], 403)
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=30)
        conn.putrequest('POST', '/api/run', skip_host=True)
        conn.putheader('Host', f'127.0.0.1:{self.port}')
        conn.putheader('Content-Length', str(dashboard.MAX_BODY + 1))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()
