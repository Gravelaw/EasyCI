"""Commit statuses through the user's own `gh` login. Failures are recorded, never fatal."""
import os
import re
import shutil
import socket
import subprocess
import sys

from easyci import store

REMOTE = re.compile(r'(?:https?://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)'
                    r'([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?\Z')
JOB_STATE = {'passed': 'success', 'skipped': 'success', 'failed': 'failure', 'pending': 'pending',
             'running': 'pending'}


def parse_remote(url):
    match = REMOTE.match((url or '').strip())
    return f'{match.group(1)}/{match.group(2)}' if match else None


def gh_command():
    value = os.environ.get('EASYCI_GH') or shutil.which('gh') or 'gh'
    return [sys.executable, value] if value.endswith('.py') else [value]


def gh(args, timeout=60):
    try:
        result = subprocess.run([*gh_command(), *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    text = (result.stdout + result.stderr).decode('utf-8', errors='replace')
    return result.returncode, text


def gh_json(args, timeout=60):
    """Parsed JSON stdout of a gh call, or None (gh() mixes stdout and stderr for messages)."""
    import json
    try:
        result = subprocess.run([*gh_command(), *args], capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                creationflags=0x08000000 if os.name == 'nt' else 0)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout.decode('utf-8', errors='replace') or 'null')
    except ValueError:
        return None


MARKER = '<!-- easyci:pr-comment -->'


def comment_body(rd, run):
    """The PR comment: the plain verdict, per-job lines, the failure explanation and metrics changes."""
    from easyci import explain
    lines = [MARKER, f'**EasyCI** on `{run.get("commit", "")[:7]}`: {explain.plain(run)}', '']
    for job in explain.counted(run):
        state = {'passed': 'passed', 'skipped': 'skipped'}.get(job.get('state'), job.get('state'))
        lines.append(f'- `{job["name"]}`: {state}' + (f' ({job["reason"]})' if job.get('reason') else ''))
    if run.get('state') in ('failed', 'unsupported', 'timed_out', 'interrupted'):
        try:
            why = explain.explain(rd, run, store.read_json(rd / 'plan.json') or {})
            lines += ['', why['summary']]
            if why.get('excerpt'):
                lines += ['', '```', *why['excerpt'][-15:], '```']
        except Exception:  # noqa: BLE001 - the comment still goes out without it
            pass
    delta = metrics_delta(rd, run)
    if delta:
        lines += ['', f'Metrics compared with the last passed run ({delta["against"]}):', '',
                  '| metric | before | now |', '|---|---|---|']
        lines += [f'| {k} | {b} | {n} |' for k, b, n in delta['rows']]
    lines += ['', f'<sub>Run `{run.get("run_id")}` on {socket.gethostname()}. '
              'Run `easyci explain` there for details.</sub>']
    return '\n'.join(lines)


def metrics_delta(rd, run):
    """Metric values now vs. the latest earlier passed run of the same project that has metrics."""
    now = {f'{j["name"]}.{k}': v for j in run.get('jobs', []) for k, v in (j.get('metrics') or {}).items()}
    if not now:
        return None
    for other in sorted(pathlib_parent(rd).glob('*'), reverse=True):
        if other.name >= rd.name:
            continue
        prev = store.read_json(other / 'run.json') or {}
        if prev.get('state') != 'passed':
            continue
        before = {f'{j["name"]}.{k}': v for j in prev.get('jobs', []) for k, v in (j.get('metrics') or {}).items()}
        if before:
            return {'against': f'{other.name}, commit {prev.get("commit", "")[:7]}',
                    'rows': [(k, before.get(k, '-'), v) for k, v in sorted(now.items())]}
    return None


def pathlib_parent(rd):
    import pathlib
    return pathlib.Path(rd).parent


def post_comment(rd, run):
    """Create or update the one EasyCI comment on each open PR whose head is this commit."""
    info = run.get('github') or {}
    if not info.get('enabled') or not info.get('repo'):
        return []
    repo, done = info['repo'], []
    pulls = gh_json(['api', f'repos/{repo}/commits/{run["commit"]}/pulls']) or []
    body = comment_body(rd, run)
    for pr in pulls:
        if pr.get('state') != 'open' or (pr.get('head') or {}).get('sha') != run['commit']:
            continue
        number = pr['number']
        existing = [c for c in gh_json(['api', f'repos/{repo}/issues/{number}/comments?per_page=100']) or []
                    if MARKER in (c.get('body') or '')]
        if existing:
            code, text = gh(['api', '--method', 'PATCH', f'repos/{repo}/issues/comments/{existing[-1]["id"]}',
                             '-f', f'body={body}'])
        else:
            code, text = gh(['api', '--method', 'POST', f'repos/{repo}/issues/{number}/comments', '-f', f'body={body}'])
        done.append({'pr': number, 'ok': code == 0, 'updated': bool(existing)})
        if code != 0:
            store.log(f'{run.get("run_id")}: PR comment on #{number} failed: {(text or "")[-200:]}')
    info['comments'] = done
    return done


def auth_status():
    """{'installed', 'logged_in', 'account', 'repo_scope'}; never prints or stores a token."""
    if not (os.environ.get('EASYCI_GH') or shutil.which('gh')):
        return {'installed': False, 'logged_in': False, 'account': None, 'repo_scope': False}
    code, text = gh(['auth', 'status', '--hostname', 'github.com'], timeout=30)
    account = re.search(r'account (\S+)', text or '')
    scopes = re.search(r'Token scopes:(.*)', text or '')
    return {'installed': code is not None, 'logged_in': code == 0, 'account': account.group(1) if account else None,
            'repo_scope': bool(scopes and re.search(r"'repo'", scopes.group(1))) or (code == 0 and not scopes)}


def duration(seconds):
    seconds = int(seconds or 0)
    return f'{seconds // 60}m {seconds % 60:02d}s'


def desired(run):
    """[(context, state, description)] for the run as it is now: job contexts first, overall last."""
    host, rid = socket.gethostname(), run.get('run_id')
    out = []
    for job in run.get('jobs', []):
        if not job.get('reported'):
            continue
        state = JOB_STATE.get(job.get('state'), 'error')
        if job.get('state') == 'pending':
            text = f'queued on {host} · run {rid}' if run.get('state') == 'queued' else f'waiting on {host} · run {rid}'
        elif job.get('state') == 'running':
            text = f'running on {host} · run {rid}'
        elif job.get('state') == 'passed':
            text = f'passed in {duration(job.get("elapsed_s"))} · run {rid} · {job.get("backend")}'
        elif job.get('state') == 'skipped':
            text = f'skipped ({job.get("reason") or "condition false"}) · run {rid}'
        elif job.get('state') == 'failed':
            step = job.get('failed_step') or {}
            text = f'failed at step {step.get("index", "?")}/{job.get("steps_total", "?")} {step.get("name", "")} · run {rid}'
        else:
            text = f'{job.get("state")}: {job.get("reason") or ""} · run {rid}'
        out.append((f'easyci/{job["name"]}', state, text))
    state = run.get('state')
    overall = {'passed': 'success', 'failed': 'failure'}.get(state, 'pending' if state in store.LIVE else 'error')
    if state in store.LIVE:
        text = f'{state} on {host} · run {rid}'
    elif state == 'passed':
        text = f'passed in {duration(store.elapsed(run))} · run {rid} · {host}'
    else:
        text = f'{state}{": " + run["reason"] if run.get("reason") else ""} · run {rid} · {host}'
    out.append(('easyci', overall, text))
    return [(c, s, d if len(d) <= 140 else d[:137] + '...') for c, s, d in out]


def dashboard_url():
    """`dashboard_url` from config.yml in EASYCI_HOME (spec Phase 2 target_url), or None."""
    from easyci import store
    path = store.home() / 'config.yml'
    if not path.exists():
        return None
    try:
        import yaml
        url = (yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('dashboard_url')
    except Exception:  # noqa: BLE001 - a broken config never blocks reporting
        return None
    return url.rstrip('/') if isinstance(url, str) and url.startswith(('http://127.0.0.1', 'http://localhost')) else None


def post(repo, sha, state, context, description, run_id=None):
    args = ['api', '--method', 'POST', f'repos/{repo}/statuses/{sha}', '-f', f'state={state}',
            '-f', f'context={context}', '-f', f'description={description}']
    base = dashboard_url()
    if base and run_id:  # the link opens the run on this PC's dashboard
        args += ['-f', f'target_url={base}/runs/{run_id}']
    code, text = gh(args, timeout=60)
    return code == 0, None if code == 0 else (text or 'gh failed').strip()[-300:]


def sync(rd, run, force=False):
    """Post every context whose desired state differs from what was last posted successfully."""
    info = run.setdefault('github', {})
    if not info.get('enabled'):
        return info
    posted, attempts = info.setdefault('contexts', {}), info.setdefault('attempts', [])
    failed = []
    for context, state, description in desired(run):
        if not force and posted.get(context) == state and context not in info.get('failed', []):
            continue
        ok, error = post(info['repo'], run['commit'], state, context, description, run.get('run_id'))
        attempts.append({'at': store.iso(), 'context': context, 'state': state, 'ok': ok, 'error': error})
        if ok:
            posted[context] = state
        else:
            failed.append(context)
            store.log(f'{run.get("run_id")}: status {context}={state} failed: {error}')
    del attempts[:-200]
    info['failed'] = failed
    info['reported'] = not failed and bool(posted)
    return info
