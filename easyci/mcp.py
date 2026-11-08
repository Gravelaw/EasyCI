"""`easyci mcp`: a Model Context Protocol server over stdio (spec section 6, Phase 2).

An agent can drive a whole cycle with tools only: easyci_setup, easyci_plan, easyci_run,
easyci_status, easyci_wait, easyci_logs, easyci_explain, easyci_cancel, easyci_list, easyci_doctor,
easyci_verify. Each tool runs the matching `easyci ... --json` command in-process and returns its
`easyci/1` document as text, so the answers are exactly what the CLI's JSON contract promises.
Messages are newline-delimited JSON-RPC 2.0 on stdin/stdout; nothing else is ever written to stdout.
"""
import contextlib
import io
import json
import sys

from easyci import VERSION

PROTOCOL = '2025-06-18'
VERDICTS = {'RUN_FAILED', 'UNSUPPORTED', 'RUN_INCOMPLETE', 'WAIT_TIMEOUT'}  # outcomes, not errors

STR = {'type': 'string'}
TOOLS = [
    ('easyci_setup', 'Detect a project, pick where its tests run, and plan (or write with write=true) a starter '
                     'workflow and easyci.yml. commit=true commits only those files.',
     {'repo': STR, 'write': {'type': 'boolean'}, 'commit': {'type': 'boolean'}}, []),
    ('easyci_plan', 'Show which jobs and steps EasyCI would run for a commit, without running anything.',
     {'repo': STR, 'commit': STR, 'base': STR, 'profile': STR, 'event': STR}, []),
    ('easyci_run', 'Start a run in the background; returns its run_id at once. Use easyci_wait or easyci_status after.',
     {'repo': STR, 'commit': STR, 'base': STR, 'profile': STR, 'event': STR, 'backend': STR,
      'jobs': {'type': 'array', 'items': STR}, 'no_report': {'type': 'boolean'}}, []),
    ('easyci_status', 'State of a run (default: the latest) with a one-line plain-English summary.',
     {'run_id': STR, 'repo': STR}, []),
    ('easyci_wait', 'Wait for a run to finish, at most timeout_minutes (default 10, max 60). Call again if it is '
                    'still running.', {'run_id': STR, 'timeout_minutes': {'type': 'number'}}, ['run_id']),
    ('easyci_logs', 'The last lines of a job log.', {'run_id': STR, 'job': STR, 'tail': {'type': 'integer'}}, []),
    ('easyci_explain', 'Why a run failed, in plain words, with the failing step and log excerpt.', {'run_id': STR}, []),
    ('easyci_cancel', 'Stop a running run.', {'run_id': STR}, ['run_id']),
    ('easyci_list', 'Recent runs (of one repository when repo is given).',
     {'repo': STR, 'limit': {'type': 'integer'}}, []),
    ('easyci_doctor', 'Check that this PC (and optionally a repository) is ready to run tests.', {'repo': STR}, []),
    ('easyci_verify', 'Check that a finished run\'s sealed evidence is unchanged.', {'run_id': STR}, []),
]


def argv_for(name, a):
    def opt(flag, key):
        return [flag, str(a[key])] if a.get(key) not in (None, '') else []
    if name == 'easyci_setup':
        return ['setup', *opt('--repo', 'repo'), *(['--write'] if a.get('write') else []),
                *(['--commit'] if a.get('commit') else [])]
    if name in ('easyci_plan', 'easyci_run'):
        out = ['plan' if name == 'easyci_plan' else 'run', *opt('--repo', 'repo'), *opt('--commit', 'commit'),
               *opt('--base', 'base'), *opt('--profile', 'profile'), *opt('--event', 'event'),
               *opt('--backend', 'backend')]
        for job in a.get('jobs') or []:
            out += ['--job', str(job)]
        return out + (['--no-report'] if a.get('no_report') else [])
    if name == 'easyci_wait':
        minutes = min(max(float(a.get('timeout_minutes') or 10), 0.05), 60)
        return ['wait', str(a['run_id']), '--timeout', str(minutes)]
    if name == 'easyci_logs':
        return ['logs', *([str(a['run_id'])] if a.get('run_id') else []), *opt('--job', 'job'), *opt('--tail', 'tail')]
    if name == 'easyci_list':
        return ['list', *opt('--repo', 'repo'), *opt('--limit', 'limit')]
    if name == 'easyci_doctor':
        return ['doctor', *opt('--repo', 'repo')]
    simple = {'easyci_status': 'status', 'easyci_explain': 'explain', 'easyci_cancel': 'cancel', 'easyci_verify': 'verify'}
    return [simple[name], *([str(a['run_id'])] if a.get('run_id') else []),
            *(opt('--repo', 'repo') if name == 'easyci_status' else [])]


def call_tool(name, arguments):
    from easyci import cli
    if name not in {t[0] for t in TOOLS}:
        return {'content': [{'type': 'text', 'text': f'unknown tool {name}'}], 'isError': True}
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([*argv_for(name, arguments or {}), '--json'])
    except KeyError as error:
        return {'content': [{'type': 'text', 'text': f'missing argument {error}'}], 'isError': True}
    text = out.getvalue().strip()
    try:
        doc = json.loads(text)
    except ValueError:
        doc = {'ok': False, 'error': {'code': 'INTERNAL', 'message': text[-500:] or err.getvalue()[-500:]}}
    # A run that failed (or is still going) is a successful tool call: the document says so. Only
    # problems with the request or EasyCI itself are tool errors.
    is_error = not doc.get('ok', False) and (doc.get('error') or {}).get('code') not in VERDICTS
    return {'content': [{'type': 'text', 'text': json.dumps(doc, indent=1)}], 'structuredContent': doc,
            'isError': is_error, '_exit_code': code}


def handle(message):
    """One JSON-RPC message -> response dict, or None for notifications."""
    mid, method = message.get('id'), message.get('method')
    params = message.get('params') or {}
    if mid is None:  # notifications (initialized, cancelled) need no answer
        return None
    if method == 'initialize':
        result = {'protocolVersion': params.get('protocolVersion') or PROTOCOL,
                  'capabilities': {'tools': {'listChanged': False}},
                  'serverInfo': {'name': 'easyci', 'version': VERSION},
                  'instructions': 'Local CI on this PC. Start with easyci_doctor or easyci_setup, then easyci_run, '
                                  'easyci_wait and easyci_explain. Runs keep going after this server exits.'}
    elif method == 'ping':
        result = {}
    elif method == 'tools/list':
        result = {'tools': [{'name': n, 'description': d,
                             'inputSchema': {'type': 'object', 'properties': props, 'required': req}}
                            for n, d, props, req in TOOLS]}
    elif method == 'tools/call':
        result = call_tool(params.get('name'), params.get('arguments'))
        result.pop('_exit_code', None)
    else:
        return {'jsonrpc': '2.0', 'id': mid, 'error': {'code': -32601, 'message': f'method not found: {method}'}}
    return {'jsonrpc': '2.0', 'id': mid, 'result': result}


def serve(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'parse error'}}
        else:
            try:
                response = handle(message)
            except Exception as error:  # noqa: BLE001 - one bad request must not end the session
                response = {'jsonrpc': '2.0', 'id': message.get('id'),
                            'error': {'code': -32603, 'message': f'internal error: {error}'}}
        if response is not None:
            stdout.write(json.dumps(response) + '\n')
            stdout.flush()
