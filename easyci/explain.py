"""Plain-language sentences (spec section 7) and the basic failure explainer."""
import re

from easyci import store

PATTERNS = [
    (r'command not found|is not recognized as an internal|No such file or directory.*(node|npm|julia|python)',
     'A program the step needs is not installed on this backend. Run `easyci doctor`.'),
    (r'ENOSPC|No space left on device', 'The disk is full. Free some space or run `easyci clean`.'),
    (r'\bKilled\b|out of memory|ENOMEM|OutOfMemoryError', 'The process ran out of memory. Close other programs and try again.'),
    (r'npm ERR!|npm error', 'npm failed. Run the same npm command in the project and read the first error.'),
    (r'ModuleNotFoundError|Cannot find module|ERR_MODULE_NOT_FOUND|Package \S+ not found',
     'A dependency is missing. Install dependencies first (npm ci, Pkg.instantiate(), pip install).'),
    (r'LoadError|UndefVarError|MethodError', 'Julia could not run the code; the lines above name the file and line.'),
    (r'Test Failed|tests? failed|\bFAIL\b|AssertionError|assert(ion)? failed', 'A test failed. Run that test in the project and look at the first failure.'),
    (r'Permission denied|EACCES|EPERM', 'A file permission problem. Check that no other program holds the file.'),
    (r'EADDRINUSE|address already in use', 'A network port is already in use. Stop the other server and run again.'),
]
REASONS = {'cancelled': 'you asked to stop it', 'stalled': 'nothing was printed for too long',
           'timeout': 'it ran past its time limit', 'supervisor_died': 'the EasyCI supervisor stopped',
           'busy': 'the machine was busy and --no-queue was given'}


def minutes_text(seconds):
    m = store.minutes(seconds)
    return f'{m} minute{"" if m == 1 else "s"}'


def counted(run):
    return [j for j in run.get('jobs', []) if j.get('reason') != 'skipped_by_config']


def why_unsupported(reason):
    reason = reason or ''
    if reason.startswith('unsupported_uses:'):
        return f'the workflow uses {reason.split(":", 1)[1]}, which EasyCI does not support yet'
    if reason.startswith('toolchain_mismatch:'):
        return f'the installed toolchain does not match ({reason.split(":", 1)[1]})'
    if reason.startswith('toolchain_missing:'):
        return f'{reason.split(":", 1)[1]} is not installed on that backend'
    if reason.startswith('missing_env:'):
        return f'the variable {reason.split(":", 1)[1]} is not set'
    if reason.startswith('unsupported_expression:'):
        return f'the workflow uses the expression `{reason.split(":", 1)[1]}`, which EasyCI does not support yet'
    return f'of {reason}'


def github_clause(run, gh):
    gh = gh or {}
    if not gh.get('enabled'):
        return 'Not reported to GitHub (reporting is off).'
    contexts = gh.get('contexts') or {}
    if gh.get('reported') and not gh.get('failed'):
        if run.get('state') == 'passed':
            return f'GitHub shows green on commit {run["commit"][:7]}.'
        return f'Reported to GitHub: yes ({len(contexts)} checks).'
    return f'Reported to GitHub: not yet; run `easyci report {run["run_id"]}`.'


def metrics_clause(jobs, limit=4):
    """' Metrics: accuracy 0.934, train_time_s 1812.' from the jobs' easyci-metrics.json, or ''."""
    items = [f'{k} {v}' for j in jobs for k, v in (j.get('metrics') or {}).items()
             if isinstance(v, (int, float, str)) and not isinstance(v, bool)]
    if not items:
        return ''
    more = f' (+{len(items) - limit} more)' if len(items) > limit else ''
    return f' Metrics: {", ".join(items[:limit])}{more}.'


def plain(run, gh=None, behind_job=None):
    state, rid, jobs = run.get('state'), run.get('run_id'), counted(run)
    so_far = minutes_text(store.elapsed(run))
    gh = gh if gh is not None else run.get('github')
    if state == 'queued':
        return (f'Waiting for the previous run to finish (it is on job {behind_job}).' if behind_job
                else 'Waiting for the previous run to finish.')
    if state == 'preparing':
        if run.get('waiting_for'):
            return (f'Waiting for another test run on this project to finish ({run["waiting_for"]}); '
                    'EasyCI starts by itself once it is done.')
        return 'Getting a clean copy of your code ready.'
    if state == 'running':
        live = next((j for j in jobs if j.get('state') == 'running'), None)
        if not live:
            return f'Your tests are running: {so_far} so far.'
        i = jobs.index(live) + 1
        text = (f'Your tests are running: {live["name"]} (job {i} of {len(jobs)}), step {live.get("current_step") or 1} '
                f'of {live.get("steps_total") or "?"}, {so_far} so far.')
        if live.get('stalled'):
            quiet = store.elapsed({'started_at': live.get('last_output_at') or live.get('started_at')})
            text = (text[:-1] + f', but nothing printed for {minutes_text(quiet)}; this can be normal for long quiet '
                    f'steps. EasyCI will stop it at {live.get("timeout_minutes")} minutes if it never finishes.')
        return text
    first = lambda *states: next((j for j in jobs if j.get('state') in states), None)  # noqa: E731
    if state == 'passed':
        return f'All checks passed in {so_far}. {github_clause(run, gh)}{metrics_clause(jobs)}'
    if state == 'failed':
        job = first('failed', 'interrupted')
        if job and job.get('reason') == 'stalled':
            return (f'Checks failed: {job["name"]} was stopped because nothing was printed for '
                    f'{job.get("stall_minutes")} minutes. {github_clause(run, gh)}')
        if job and job.get('failed_step'):
            step = job['failed_step']
            return (f'Checks failed: {job["name"]} stopped at step {step["index"]} (`{step["name"]}`). '
                    f'Run `easyci explain {rid}` for details. {github_clause(run, gh)}')
        return f'Checks failed: {(job or {}).get("reason") or run.get("reason")}. Run `easyci explain {rid}` for details.'
    if state == 'unsupported':
        job = first('unsupported') or {}
        return (f'EasyCI could not run {job.get("name", "a job")} because {why_unsupported(job.get("reason"))}. '
                f'Nothing was run for that job. {github_clause(run, gh)}')
    if state == 'timed_out':
        job = first('timed_out') or {}
        return (f'{job.get("name", "A job")} ran longer than {job.get("timeout_minutes")} minutes and was stopped. '
                f'{github_clause(run, gh)}')
    if state == 'cancelled':
        return ('Stopped: the machine was busy and --no-queue was given.' if run.get('reason') == 'busy'
                else f'Stopped because you asked. {github_clause(run, gh)}')
    if state == 'interrupted':
        return ('The run was interrupted (the computer may have restarted or the process was killed). '
                f'Start it again with `easyci run`. {github_clause(run, gh)}')
    return f'Unknown state {state}.'


def log_section(text, index):
    """Lines printed by step `index` (between its marker and the next step marker)."""
    out, inside = [], False
    for line in text.splitlines():
        match = re.match(r'##\[easyci\] step (\d+)(/| )', line)
        if match:
            inside = int(match.group(1)) == index and match.group(2) == '/'
            continue
        if inside:
            out.append(line)
    return out


def explain(rd, run, plan):
    bad = next((j for j in run.get('jobs', []) if j.get('state') in
                ('failed', 'timed_out', 'unsupported', 'cancelled', 'interrupted')), None)
    if not bad:
        summary = ('Nothing failed: all checks passed.' if run.get('state') == 'passed'
                   else f'Nothing has failed so far. {plain(run)}')
        return {'summary': summary, 'failing_job': None, 'failing_step': None, 'excerpt': [], 'suggestions': []}
    step = bad.get('failed_step')
    try:
        text = (rd / 'jobs' / bad['name'] / 'log.txt').read_text(encoding='utf-8', errors='replace')
    except OSError:
        text = ''
    section = log_section(text, step['index']) if step else text.splitlines()
    excerpt = [line for line in section if line.strip()][-20:]
    pjob = next((j for j in plan.get('jobs', []) if j['id'] == bad['name']), {})
    pstep = next((s for s in pjob.get('steps', []) if step and s['index'] == step['index']), {})
    command = (pstep.get('run') or pstep.get('uses') or '').strip().splitlines()[0] if pstep else ''
    suggestions = [tip for pattern, tip in PATTERNS if re.search(pattern, '\n'.join(excerpt), re.I)]
    if bad['state'] == 'unsupported':
        summary = f'EasyCI could not run {bad["name"]} because {why_unsupported(bad.get("reason"))}. Nothing was run.'
        suggestions = [bad['fix']] if bad.get('fix') else ['Add an override for this job in easyci.yml.']
    elif step:
        last = excerpt[-1] if excerpt else '(no output)'
        how = REASONS.get(bad.get('reason'), '')
        summary = (f'The job {bad["name"]} {"stopped" if how else "failed"} at step {step["index"]} of '
                   f'{bad.get("steps_total", "?")} (`{command or step["name"]}`){" because " + how if how else ""}. '
                   f'The last line says: {last}')
        if command and pstep.get('kind') == 'run':
            suggestions.append(f'Run `{command}` in the project and look at the first red line.')
    else:
        summary = f'The job {bad["name"]} ended {bad["state"]} ({REASONS.get(bad.get("reason"), bad.get("reason"))}).'
    return {'summary': summary, 'failing_job': bad['name'], 'failing_step': step, 'excerpt': excerpt,
            'suggestions': suggestions or ['Read the full log with `easyci logs`.']}
