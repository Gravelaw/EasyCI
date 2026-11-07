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


def post(repo, sha, state, context, description):
    code, text = gh(['api', '--method', 'POST', f'repos/{repo}/statuses/{sha}', '-f', f'state={state}',
                     '-f', f'context={context}', '-f', f'description={description}'], timeout=60)
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
        ok, error = post(info['repo'], run['commit'], state, context, description)
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
