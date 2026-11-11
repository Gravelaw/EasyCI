"""`easyci dashboard`: a page on this PC that shows what EasyCI is doing (spec section 7, Phase 2).

Standard library only. It listens on 127.0.0.1, refuses requests whose Host header is not local
(that blocks DNS-rebinding pages from reaching it), and every action that changes something
(start or cancel a run) must carry a token that only the served page knows. Nothing is loaded from
the internet.
"""
import argparse
import html
import json
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from easyci import explain, store

DEFAULT_PORT = 8765
MAX_BODY = 65536  # bytes; the page only ever posts a folder path


def projects():
    out = []
    root = store.home() / 'projects'
    for pdir in sorted(root.glob('*')) if root.exists() else []:
        info = store.read_json(pdir / 'project.json') or {}
        if info.get('repo'):
            out.append({'slug': pdir.name, 'repo': info['repo'], 'remote': info.get('remote')})
    return out


_REAP = {'lock': threading.Lock(), 'last': 0.0}


def reap_now(every_s=5.0):
    """Run the orphan reaper (as every CLI command does) at most every few seconds, one request at a
    time, so a run whose supervisor died shows as interrupted while only the dashboard is open."""
    import time
    from easyci import cli
    if not _REAP['lock'].acquire(blocking=False):
        return
    try:
        if time.monotonic() - _REAP['last'] >= every_s:
            cli.safe_reap()
            _REAP['last'] = time.monotonic()
    finally:
        _REAP['lock'].release()


def run_list(limit=30):
    from easyci import cli
    reap_now()
    dirs = sorted(store.run_dirs(), key=lambda p: p.name, reverse=True)[:limit]
    return [cli.view(rd) for rd in dirs]


def run_detail(run_id, tail=200):
    from easyci import cli
    reap_now()
    rd = store.find_run(run_id)
    if not rd:
        return None
    data = cli.view(rd)
    run = store.read_json(rd / 'run.json') or {}
    live = next((j['name'] for j in run.get('jobs', []) if j.get('state') == 'running'), None)
    job = live or next((j['name'] for j in reversed(run.get('jobs', []))
                        if (rd / 'jobs' / j['name'] / 'log.txt').exists()), None)
    log = ''
    if job:
        path = rd / 'jobs' / job / 'log.txt'
        lines = path.read_text(encoding='utf-8', errors='replace').splitlines() if path.exists() else []
        log = '\n'.join(lines[-tail:])
    data['log_job'], data['log'] = job, log
    if data['state'] in ('failed', 'unsupported', 'timed_out', 'interrupted'):
        try:
            data['explain'] = explain.explain(rd, run, store.read_json(rd / 'plan.json') or {})
        except Exception:  # noqa: BLE001 - the page still renders without it
            data['explain'] = None
    return data


def start_run(repo):
    from easyci import cli
    ns = argparse.Namespace(repo=repo, commit=None, ref=None, base=None, profile=None, event=None, job=None,
                            workflow=None, backend=None, config=None, wait=False, no_queue=False, no_report=False,
                            keep=False, json=True)
    return cli.cmd_run(ns)


def cancel_run(run_id):
    rd = store.find_run(run_id)
    if not rd:
        return None
    run = store.read_json(rd / 'run.json') or {}
    if run.get('state') not in store.TERMINAL:
        (rd / 'cancel').write_text(store.iso(), encoding='ascii')
    return {'run_id': run_id, 'cancel_requested': run.get('state') not in store.TERMINAL}


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>EasyCI</title>
<style>
:root{--bg:#f7f7f5;--fg:#1c1c1a;--muted:#6b6b66;--card:#fff;--line:#e3e2dd;--ok:#1a7f37;--bad:#c62828;--run:#9a6700;--accent:#3355cc}
@media (prefers-color-scheme: dark){:root{--bg:#161615;--fg:#ecebe6;--muted:#a3a29b;--card:#20201e;--line:#34332f;--ok:#4ac26b;--bad:#ff7b72;--run:#d4a72c;--accent:#8fa8ff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:980px;margin:0 auto;padding:20px 16px 40px}h1{font-size:22px;margin:0 0 4px}h2{font-size:17px;margin:24px 0 8px}
.muted{color:var(--muted)}.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:8px 0}
.row{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.grow{flex:1;min-width:200px}
.state{font-weight:600;text-transform:uppercase;font-size:12px;letter-spacing:.04em}
.passed{color:var(--ok)}.failed,.unsupported,.timed_out,.interrupted{color:var(--bad)}.running,.queued,.preparing{color:var(--run)}
button{font:inherit;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:8px;padding:6px 12px;cursor:pointer}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}a{color:var(--accent)}
pre{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;overflow:auto;max-height:460px;font:12.5px/1.45 ui-monospace,Consolas,monospace;white-space:pre-wrap}
table{border-collapse:collapse;width:100%}td{padding:4px 8px;border-top:1px solid var(--line)}
</style></head><body><main>
<h1>EasyCI</h1><div class="muted">Tests running on this PC. This page updates by itself.</div>
<div id="app" aria-live="polite">Loading…</div>
</main>
<script>
const TOKEN = "__TOKEN__";
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(path, body) {
  const opt = body ? {method:'POST', headers:{'Content-Type':'application/json','X-EasyCI-Token':TOKEN}, body:JSON.stringify(body)} : {};
  const r = await fetch(path, opt); return r.json();
}
async function home() {
  const [p, r] = await Promise.all([api('/api/projects'), api('/api/runs')]);
  if (!p.ok || !r.ok) {  // every API answer is {ok, schema, data | error}
    const e = (p.ok ? r : p).error || {};
    document.getElementById('app').innerHTML = `<div class="card">Could not load: ${esc(e.message)}</div>`;
    return;
  }
  const projects = p.data.projects, runs = r.data.runs;
  let h = '<h2>Projects</h2>';
  h += projects.length ? projects.map(x => `<div class="card row"><div class="grow"><b>${esc(x.slug)}</b><div class="muted">${esc(x.repo)}</div></div>
    <button class="primary" data-repo="${esc(x.repo)}" onclick="runChecks(this.dataset.repo)">Run checks</button></div>`).join('')
    : '<div class="card muted">No projects yet. Run <code>easyci setup</code> in a project folder.</div>';
  h += '<h2>Recent runs</h2>';
  h += runs.length ? runs.map(x => `<div class="card"><div class="row"><span class="state ${esc(x.state)}">${esc(x.state)}</span>
    <a href="/runs/${esc(x.run_id)}">${esc(x.run_id)}</a><span class="muted">${esc(x.project)} · ${esc((x.commit||'').slice(0,7))}</span></div>
    <div>${esc(x.plain)}</div></div>`).join('') : '<div class="card muted">No runs yet.</div>';
  document.getElementById('app').innerHTML = h;
}
async function runChecks(repo) {
  const r = await api('/api/run', {repo});
  if (r.ok) location.href = '/runs/' + r.data.run_id; else alert(r.error.message + '\\n' + (r.error.fix || ''));
}
async function detail(id) {
  const d = await api('/api/runs/' + id);
  if (!d.ok) { document.getElementById('app').innerHTML = '<div class="card">' + esc(d.error.message) + '</div>'; return; }
  const x = d.data, live = ['queued','preparing','running'].includes(x.state);
  let h = `<p><a href="/">← all runs</a></p><div class="card"><div class="row"><span class="state ${esc(x.state)}">${esc(x.state)}</span>
    <b class="grow">${esc(x.run_id)}</b>${live ? '<button onclick="cancelRun()">Cancel</button>' : ''}</div><div style="margin-top:6px">${esc(x.plain)}</div>
    <div class="muted">${esc(x.project)} · commit ${esc((x.commit||'').slice(0,7))}</div></div>`;
  h += '<table>' + (x.jobs||[]).map(j => `<tr><td><span class="state ${esc(j.state)}">${esc(j.state)}</span></td><td>${esc(j.name)}</td>
    <td class="muted">${esc(j.reason||'')}</td></tr>`).join('') + '</table>';
  if (x.explain) h += `<h2>Why it failed</h2><div class="card">${esc(x.explain.summary)}</div>` +
    (x.explain.suggestions||[]).map(s => `<div class="card muted">Suggestion: ${esc(s)}</div>`).join('');
  if (x.log_job) h += `<h2>Log: ${esc(x.log_job)}</h2><pre id="log">${esc(x.log)}</pre>`;
  document.getElementById('app').innerHTML = h;
  const pre = document.getElementById('log'); if (pre) pre.scrollTop = pre.scrollHeight;
  if (live) setTimeout(() => detail(id), 3000);
}
async function cancelRun() { await api(location.pathname.replace('/runs/','/api/runs/') + '/cancel', {}); }
const m = location.pathname.match(/^\\/runs\\/([\\w-]+)$/);
if (m) detail(m[1]); else { home(); setInterval(home, 5000); }
</script></body></html>"""


def make_handler(token, port):
    allowed_hosts = {f'127.0.0.1:{port}', f'localhost:{port}'}

    class Handler(BaseHTTPRequestHandler):
        server_version = 'EasyCI'

        def log_message(self, fmt, *args):  # keep the console quiet
            return

        def send(self, status, body, ctype='application/json; charset=utf-8'):
            data = body if isinstance(body, bytes) else body.encode('utf-8')
            self.send_response(status)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'unsafe-inline'; "
                                                        "style-src 'unsafe-inline'; frame-ancestors 'none'")
            self.end_headers()
            self.wfile.write(data)

        def doc(self, data=None, error=None, status=HTTPStatus.OK):
            body = {'ok': error is None, 'schema': 'easyci/1'}
            body.update({'data': data} if error is None else {'error': error})
            self.send(status, json.dumps(body))

        def guard(self):
            if self.headers.get('Host') not in allowed_hosts:
                self.send(HTTPStatus.FORBIDDEN, 'forbidden host', 'text/plain')
                return False
            return True

        def do_GET(self):  # noqa: N802 - http.server API
            if not self.guard():
                return
            url = urlparse(self.path)
            parts = [p for p in url.path.split('/') if p]
            if url.path == '/' or (len(parts) == 2 and parts[0] == 'runs'):
                return self.send(HTTPStatus.OK, PAGE.replace('__TOKEN__', token), 'text/html; charset=utf-8')
            if url.path == '/api/projects':
                return self.doc({'projects': projects()})
            if url.path == '/api/runs':
                return self.doc({'runs': run_list()})
            if len(parts) == 3 and parts[:2] == ['api', 'runs']:
                tail = int((parse_qs(url.query).get('tail') or ['200'])[0])
                data = run_detail(parts[2], tail=min(max(tail, 1), 5000))
                return self.doc(data) if data else self.doc(error={'code': 'RUN_NOT_FOUND', 'message':
                                                                    f'No run {parts[2]}.'}, status=HTTPStatus.NOT_FOUND)
            self.send(HTTPStatus.NOT_FOUND, 'not found', 'text/plain')

        def do_POST(self):  # noqa: N802
            # Read the body before any refusal: on Windows, closing a socket that
            # still holds unread request bytes resets the connection, and the
            # client sees an abort instead of the 403.
            try:
                length = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                length = -1
            if not 0 <= length <= MAX_BODY:
                self.close_connection = True
                return self.send(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, 'request body too large', 'text/plain')
            raw = self.rfile.read(length) if length else b''
            if not self.guard():
                return
            if not secrets.compare_digest(self.headers.get('X-EasyCI-Token', ''), token):
                return self.doc(error={'code': 'BAD_TOKEN', 'message': 'Missing or wrong page token.'},
                                status=HTTPStatus.FORBIDDEN)
            try:
                body = json.loads(raw or b'{}')
            except ValueError:
                body = {}
            if not isinstance(body, dict):
                body = {}
            parts = [p for p in urlparse(self.path).path.split('/') if p]
            from easyci import cli
            try:
                if parts == ['api', 'run']:
                    repo = str(body.get('repo') or '')
                    if repo not in {p['repo'] for p in projects()}:  # only projects EasyCI already knows
                        return self.doc(error={'code': 'UNKNOWN_PROJECT', 'message': 'That folder is not a known project.',
                                               'fix': 'Run easyci setup or easyci run in it once.'},
                                        status=HTTPStatus.BAD_REQUEST)
                    return self.doc(start_run(repo))
                if len(parts) == 4 and parts[:2] == ['api', 'runs'] and parts[3] == 'cancel':
                    data = cancel_run(parts[2])
                    return self.doc(data) if data else self.doc(error={'code': 'RUN_NOT_FOUND', 'message': 'No such run.'},
                                                                status=HTTPStatus.NOT_FOUND)
            except cli.Fail as error:
                return self.doc(error={'code': error.code, 'message': error.message, 'fix': error.fix},
                                status=HTTPStatus.BAD_REQUEST)
            self.send(HTTPStatus.NOT_FOUND, 'not found', 'text/plain')

    return Handler


def serve(port=DEFAULT_PORT, ready=None):
    """Serve until interrupted. `ready(url, server)` is called once listening (tests)."""
    token = secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(token, port))
    server.daemon_threads = True
    url = f'http://127.0.0.1:{server.server_address[1]}'
    if ready:
        ready(url, server, token)
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        server.server_close()


def free_port():
    import socket
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def start_in_thread(port=None):
    """For tests: (url, server, token, thread)."""
    port = port or free_port()
    box = {}
    event = threading.Event()

    def ready(url, server, token):
        box.update(url=url, server=server, token=token)
        event.set()
    thread = threading.Thread(target=serve, args=(port, ready), daemon=True)
    thread.start()
    event.wait(10)
    return box['url'], box['server'], box['token'], thread
