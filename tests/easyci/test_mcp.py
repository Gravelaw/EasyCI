"""`easyci mcp`: a whole agent cycle through MCP tools only, over a real stdio subprocess."""
import json
import os
import subprocess
import sys

from ecsupport import EasyCITest

GOOD = 'on: push\njobs:\n  quick:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo hi\n'
BAD = 'on: push\njobs:\n  quick:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo about to fail; exit 3\n'


class Mcp(EasyCITest):
    def setUp(self):
        super().setUp()
        self.proc = subprocess.Popen([sys.executable, '-m', 'easyci', 'mcp'], cwd=str(self.tmp), env=dict(os.environ),
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                     encoding='utf-8')
        self.addCleanup(self.close)
        self.next_id = 0

    def close(self):
        if self.proc.poll() is None:
            self.proc.stdin.close()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc.stdout.close()
        self.proc.stderr.close()

    def send(self, method, params=None, notify=False, raw=None):
        if raw is not None:
            self.proc.stdin.write(raw + '\n')
        else:
            message = {'jsonrpc': '2.0', 'method': method, 'params': params or {}}
            if not notify:
                self.next_id += 1
                message['id'] = self.next_id
            self.proc.stdin.write(json.dumps(message) + '\n')
        self.proc.stdin.flush()
        if notify:
            return None
        return json.loads(self.proc.stdout.readline())

    def tool(self, name, **arguments):
        reply = self.send('tools/call', {'name': name, 'arguments': arguments})
        result = reply['result']
        return result, json.loads(result['content'][0]['text'])

    def test_agent_cycle_with_tools_only(self):
        init = self.send('initialize', {'protocolVersion': '2025-06-18', 'capabilities': {},
                                        'clientInfo': {'name': 'test', 'version': '1'}})
        self.assertEqual(init['result']['serverInfo']['name'], 'easyci')
        self.assertIn('tools', init['result']['capabilities'])
        self.assertIsNone(self.send('notifications/initialized', notify=True))
        tools = {t['name']: t for t in self.send('tools/list')['result']['tools']}
        self.assertEqual(len(tools), 11)
        self.assertEqual(tools['easyci_wait']['inputSchema']['required'], ['run_id'])
        # A passing run.
        good = self.make_repo(GOOD)
        result, doc = self.tool('easyci_run', repo=str(good), backend='native', no_report=True)
        self.assertFalse(result['isError'], doc)
        run_id = doc['data']['run_id']
        result, doc = self.tool('easyci_wait', run_id=run_id, timeout_minutes=1)
        self.assertEqual(doc['data']['state'], 'passed')
        # A failing run is a successful tool call whose result says failed, and explain says why.
        bad = self.make_repo(BAD, name='bad')
        _, doc = self.tool('easyci_run', repo=str(bad), backend='native', no_report=True)
        run_id = doc['data']['run_id']
        result, doc = self.tool('easyci_wait', run_id=run_id, timeout_minutes=1)
        self.assertEqual((result['isError'], doc['data']['state']), (False, 'failed'))
        _, doc = self.tool('easyci_explain', run_id=run_id)
        self.assertIn('quick', doc['data']['summary'])
        self.assertTrue(any('about to fail' in line for line in doc['data']['excerpt']))
        _, doc = self.tool('easyci_logs', run_id=run_id, tail=5)
        self.assertTrue(doc['ok'])
        _, doc = self.tool('easyci_list', limit=5)
        self.assertEqual(len(doc['data']['runs']), 2)
        # EasyCI errors are tool errors with the usual code and fix.
        result, doc = self.tool('easyci_status', run_id='20990101-000000-dead')
        self.assertEqual((result['isError'], doc['error']['code']), (True, 'RUN_NOT_FOUND'))

    def test_protocol_errors(self):
        self.assertEqual(self.send('nope/nope')['error']['code'], -32601)
        self.assertEqual(self.send(None, raw='{not json')['error']['code'], -32700)
        self.assertEqual(self.send('ping')['result'], {})
        reply = self.send('tools/call', {'name': 'easyci_cancel', 'arguments': {}})
        self.assertTrue(reply['result']['isError'])
