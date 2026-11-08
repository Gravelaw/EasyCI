"""`easyci` command line. Never does long work itself; the supervisor does.

With --json, stdout carries exactly one JSON document ({"schema": "easyci/1", ...}); human text
goes to stderr. Exit codes follow spec section 6.3.
"""
import argparse
import json
import os
import pathlib
import sys
import time
import traceback

from easyci import SCHEMA, VERSION, explain, github, launch, snapshot, store, workflow

EXIT = {'passed': 0, 'failed': 1, 'unsupported': 4, 'timed_out': 6, 'cancelled': 6, 'interrupted': 6}
GH_FIX = 'Run: gh auth login   (or pass --no-report to run without GitHub statuses)'


class Fail(Exception):
    def __init__(self, exit_code, code, message, fix, data=None):
        super().__init__(message)
        self.exit_code, self.code, self.message, self.fix, self.data = exit_code, code, message, fix, data


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Fail(2, 'USAGE', message, f'Run: easyci {self.prog.split(" ", 1)[-1]} --help')

    def exit(self, status=0, message=None):
        if message:
            sys.stderr.write(message)
        raise SystemExit(status)


def say(args, text):
    print(text, file=sys.stderr if args.json else sys.stdout, flush=True)


# ---- helpers -------------------------------------------------------------------------------

def global_config():
    path = store.home() / 'config.yml'
    if not path.exists():
        return {}
    import yaml
    try:
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    except yaml.YAMLError as error:
        raise Fail(2, 'CONFIG_INVALID', f'{path} is not valid YAML: {error}', f'Fix or delete {path}.') from None
    return data if isinstance(data, dict) else {}


def find_repo(path):
    root = snapshot.repo_root(path or os.getcwd())
    if not root:
        raise Fail(7, 'REPO_NOT_FOUND', f'{path or os.getcwd()} is not inside a git repository.',
                   'Pass --repo with the folder of your project.')
    return root


def get_run(run_id, repo=None):
    if run_id:
        rd = store.find_run(run_id)
        if not rd:
            raise Fail(7, 'RUN_NOT_FOUND', f'No run {run_id} found under {store.home()}.', 'Run: easyci list')
        return rd
    root = snapshot.repo_root(repo or os.getcwd())
    dirs = store.run_dirs(store.project_slug(root)) if root else []
    if not dirs and not repo:
        dirs = store.run_dirs()  # cwd is not a project with runs: fall back to the latest run anywhere
    if not dirs:
        raise Fail(7, 'RUN_NOT_FOUND', 'There are no runs yet.', 'Start one with: easyci run')
    return dirs[-1]


def view(rd):
    run = store.read_json(rd / 'run.json') or {}
    report = store.read_json(rd / 'report.json') or {}
    beat = store.read_json(rd / 'heartbeat.json') or {}
    gh = report.get('github') or run.get('github') or {}
    data = {k: run.get(k) for k in ('run_id', 'project', 'repo', 'commit', 'tree', 'base', 'event', 'profile', 'state',
                                    'stalled', 'launch', 'reason', 'created_at', 'started_at', 'finished_at')}
    data['launch'] = data['launch'] or (store.read_json(rd / 'launch.json') or {}).get('launch')
    data['elapsed_s'] = store.elapsed(run) if run.get('started_at') else 0
    data['heartbeat_at'] = beat.get('at')
    data['sealed'] = (rd / 'sha256.json').exists()
    jobs = []
    for job in run.get('jobs', []):
        item = {k: job.get(k) for k in ('name', 'state', 'backend', 'steps_total', 'steps_done', 'exit_code', 'reason',
                                        'stalled', 'last_output_at', 'fix', 'failed_step', 'metrics', 'reported', 'on_kill')}
        item['elapsed_s'] = job.get('elapsed_s') or (store.elapsed({'started_at': job['started_at']})
                                                     if job.get('state') == 'running' and job.get('started_at') else None)
        jobs.append({k: v for k, v in item.items() if v is not None})
    data['jobs'] = jobs
    data['github'] = {'enabled': bool(gh.get('enabled')), 'repo': gh.get('repo'), 'reported': bool(gh.get('reported')),
                      'contexts': gh.get('contexts', {}), 'failed': gh.get('failed', [])}
    data['paths'] = {'run_dir': str(rd), 'log_dir': str(rd / 'jobs')}
    behind = None
    if run.get('state') == 'queued' and run.get('queued_behind'):
        other = store.find_run(run['queued_behind'])
        other_run = store.read_json(other / 'run.json') if other else None
        behind = next((j['name'] for j in (other_run or {}).get('jobs', []) if j.get('state') == 'running'), None)
    data['plain'] = explain.plain(run, gh, behind)
    return data


def safe_reap():
    try:
        return store.reap()
    except Exception:  # noqa: BLE001 - the reaper must never block a command
        store.log(f'reaper error {traceback.format_exc()}')
        return []


def run_exit(data):
    return EXIT.get(data['state'], 0)


def finish_run(data):
    """Map a terminal run object to (exit code, error or None)."""
    code = run_exit(data)
    if code == 0:
        return 0, None
    codes = {1: 'RUN_FAILED', 4: 'UNSUPPORTED', 6: 'RUN_INCOMPLETE'}
    fixes = {1: f'Run: easyci explain {data["run_id"]}', 4: 'See the job reason; add an override in easyci.yml.',
             6: 'Start it again with: easyci run'}
    return code, Fail(code, codes[code], data['plain'], fixes[code], data)


def wait_for(rd, timeout_s=None, until_sealed=True):
    deadline = time.time() + timeout_s if timeout_s else None
    while True:
        run = store.read_json(rd / 'run.json') or {}
        if run.get('state') in store.TERMINAL and (not until_sealed or (rd / 'sha256.json').exists()):
            return view(rd)
        if run.get('state') in store.LIVE or run.get('state') in store.TERMINAL:
            safe_reap()  # also finishes a run whose supervisor died before sealing it
        if deadline and time.time() > deadline:
            return None
        time.sleep(store.setting('EASYCI_POLL_S', 0.5) * 2)


# ---- plan inputs ---------------------------------------------------------------------------

def read_config(args, repo, commit):
    if getattr(args, 'config', None):
        path = pathlib.Path(args.config)
        try:
            data = path.read_bytes()
        except OSError as error:
            raise Fail(2, 'CONFIG_NOT_FOUND', f'Cannot read {path}: {error}', 'Check the --config path.') from None
        source = str(path.resolve())
    else:
        data, source = snapshot.show(repo, commit, 'easyci.yml'), 'easyci.yml at the tested commit'
    if data is None:
        return {}, None
    try:
        cfg = workflow.load_config(data.decode('utf-8'), source)
    except workflow.ConfigError as error:
        raise Fail(2, 'CONFIG_INVALID', str(error), 'Fix easyci.yml; see docs/usage.md for the keys.') from None
    return cfg, {'source': source, 'sha256': store.digest_bytes(data)}


def plan_inputs(args, require_report=False):
    repo = find_repo(args.repo)
    ref = args.commit or args.ref or 'HEAD'
    commit = snapshot.resolve(repo, ref)
    if not commit:
        raise Fail(7, 'COMMIT_NOT_FOUND', f'{ref} is not a commit in {repo}.', 'Check --commit/--ref.')
    branch = snapshot.current_branch(repo)
    if args.ref and snapshot.resolve(repo, f'refs/heads/{args.ref}'):
        ref_name = args.ref
    elif args.commit or (args.ref and args.ref != 'HEAD'):
        ref_name = args.ref or commit
    else:
        ref_name = branch or commit
    default = snapshot.default_branch(repo)
    if args.base:
        base = snapshot.resolve(repo, args.base)
        if not base:
            raise Fail(7, 'COMMIT_NOT_FOUND', f'--base {args.base} is not a commit.', 'Check --base.')
    else:
        base = snapshot.merge_base(repo, commit, default)
    event = args.event or ('push' if ref_name == default else 'pull_request')
    cfg, cfg_info = read_config(args, repo, commit)
    gcfg = global_config()
    paths = [p for p in snapshot.list_tree(repo, commit, '.github/workflows/')
             if p.endswith(('.yml', '.yaml')) and p.count('/') == 2]
    if not paths:
        raise Fail(2, 'NO_WORKFLOWS', f'No .github/workflows/*.yml at commit {commit[:7]}.',
                   'Commit a GitHub Actions workflow first.')
    files = [(p, snapshot.show(repo, commit, p)) for p in paths]
    repository = github.parse_remote(snapshot.origin_url(repo))
    inp = {'event': event, 'ref_name': ref_name, 'default_branch': default, 'commit': commit, 'base': base,
           'repository': repository or '', 'profile': args.profile, 'jobs': args.job, 'workflow': args.workflow,
           'backend': args.backend, 'default_backend': gcfg.get('backend'),
           'changed': snapshot.changed_files(repo, base, commit) if base and base != commit else None}
    try:
        plan = workflow.build_plan(files, cfg, inp)
    except workflow.ConfigError as error:
        raise Fail(2, 'CONFIG_INVALID', str(error), 'Fix the workflow or easyci.yml and try again.') from None
    plan['busy'] = {'paths': list(cfg.get('busy_paths', [])), 'stale_minutes': cfg.get('busy_stale_minutes', 15),
                    'wait_minutes': cfg.get('busy_wait_minutes', 120)}
    tree = snapshot.list_tree(repo, commit)
    repro = {'commit': commit, 'base': base, 'workflows': {w['file']: w['sha256'] for w in plan['workflows']},
             'easyci_yml': cfg_info, 'lockfiles': workflow.lockfile_hashes(tree, lambda p: snapshot.show(repo, commit, p))}
    report = not getattr(args, 'no_report', False) and cfg.get('report', True) and gcfg.get('report', True) is not False
    return {'repo': repo, 'commit': commit, 'base': base, 'ref_name': ref_name, 'event': plan['event'], 'cfg': cfg,
            'default_branch': default,
            'plan': plan, 'repository': repository, 'report': bool(report and repository), 'repro': repro,
            'retention': cfg.get('retention') or gcfg.get('retention')}


def plan_summary(plan):
    lines = []
    for job in plan['jobs']:
        state = job['predicted'] + (f' ({job["predicted_reason"]})' if job['predicted_reason'] else '')
        lines.append(f'{job["id"]}: {state}; {job["run_steps"]} run step(s) on {job["backend"]}'
                     + (f'; mapped {", ".join(job["mapped"])}' if job['mapped'] else ''))
    return lines


def check_backends(plan):
    for job in plan['jobs']:
        if job['skip'] or job['unsupported']:
            continue
        backend = job['backend']
        if backend == 'native' and os.name == 'nt':
            from easyci.backends import native
            if not native.git_bash():
                raise Fail(3, 'BASH_NOT_FOUND', 'Git Bash was not found for the native backend.',
                           'Install Git for Windows, or set EASYCI_BASH to its bash.exe.')
        if backend.startswith('wsl'):
            from easyci.backends import wsl
            found = wsl.distros()
            if found is None:
                raise Fail(3, 'WSL_NOT_FOUND', 'WSL is not available on this PC.', 'Install WSL: wsl --install')
            distro = backend.partition(':')[2]
            if distro and distro not in {d['name'] for d in found}:
                raise Fail(3, 'DISTRO_NOT_FOUND', f'WSL distro {distro} was not found.',
                           'Create the distro, or change backend: in easyci.yml. Run: easyci doctor')
            if not distro and not any(d['default'] for d in found):
                raise Fail(3, 'DISTRO_NOT_FOUND', 'There is no default WSL distro.', 'Set backend: wsl:<distro> in easyci.yml.')


# ---- commands ------------------------------------------------------------------------------

def cmd_run(args):
    info = plan_inputs(args)
    plan = info['plan']
    if info['report']:
        auth = github.auth_status()
        if not auth['logged_in']:
            raise Fail(3, 'GH_NOT_LOGGED_IN', 'gh is installed but not logged in.' if auth['installed']
                       else 'GitHub CLI (gh) is not installed.', GH_FIX)
    check_backends(plan)
    if args.no_queue and store.busy():
        raise Fail(5, 'BUSY', f'The machine is busy with run {store.busy()}.',
                   'Wait for it (easyci wait), or run again without --no-queue to queue behind it.')
    slug = store.project_slug(info['repo'])
    pdir = store.project_dir(slug)
    run_id = store.new_run_id()
    rd = pdir / 'runs' / run_id
    rd.mkdir(parents=True)
    store.write_json(pdir / 'project.json', {'repo': str(info['repo']), 'remote': info['repository'],
                                             'default_branch': info['default_branch'],
                                             'last_import': plan_summary(plan), 'updated_at': store.iso(),
                                             'retention': info['cfg'].get('retention')})
    options = {'no_queue': args.no_queue, 'report': info['report'], 'keep': args.keep, 'wait': args.wait}
    store.write_json(rd / 'request.json', {
        'schema': SCHEMA, 'run_id': run_id, 'repo': str(info['repo']), 'commit': info['commit'], 'base': info['base'],
        'ref_name': info['ref_name'], 'event': info['event'], 'profile': args.profile, 'jobs': args.job,
        'workflow': args.workflow, 'backend': args.backend, 'options': options, 'config': info['repro']['easyci_yml'],
        'repro': info['repro'], 'created_at': store.iso(), 'easyci_version': VERSION})
    store.write_json(rd / 'plan.json', plan)
    jobs = [{'name': j['id'], 'state': 'pending', 'backend': j['backend'], 'reported': j['reported'],
             'steps_total': len(j['steps']), 'steps_done': 0, 'timeout_minutes': j['timeout_minutes'],
             'stall_minutes': j['stall_minutes']} for j in plan['jobs']]
    store.write_json(rd / 'run.json', {
        'schema': SCHEMA, 'run_id': run_id, 'project': slug, 'repo': str(info['repo']), 'commit': info['commit'],
        'base': info['base'], 'tree': None, 'event': info['event'], 'profile': args.profile, 'state': 'queued',
        'stalled': False, 'launch': None, 'created_at': store.iso(), 'started_at': None, 'finished_at': None,
        'jobs': jobs, 'github': {'enabled': info['report'], 'repo': info['repository'], 'contexts': {},
                                 'reported': False}})
    store.add_ticket(run_id)
    try:
        path, pid = launch.start(rd, run_id)
    except OSError as error:
        store.remove_ticket(run_id)
        raise Fail(3, 'LAUNCH_FAILED', f'Could not start the supervisor: {error}', 'Run: easyci doctor') from None
    store.write_json(rd / 'launch.json', {'launch': path, 'pid': pid, 'at': store.iso()})
    deadline = time.time() + 5
    while time.time() < deadline and not (rd / 'heartbeat.json').exists():
        time.sleep(0.1)
    if not (rd / 'heartbeat.json').exists() and pid and not store.pid_alive(pid):
        err = (rd / 'supervisor.err').read_text(encoding='utf-8', errors='replace')[-500:]
        store.log(f'{run_id}: supervisor exited at once: {err}')
        raise Fail(10, 'SUPERVISOR_FAILED', f'The supervisor stopped right after starting: {err.strip()[-200:]}',
                   f'See {rd / "supervisor.err"}; the run will be marked interrupted.')
    data = view(rd)
    if args.wait:
        data = wait_for(rd)
        code, fail = finish_run(data)
        if fail:
            raise fail
        return data
    if all(j['predicted'] == 'unsupported' for j in plan['jobs'] if j['reported']) and plan['jobs']:
        raise Fail(4, 'UNSUPPORTED', 'Every selected job uses something EasyCI cannot run; nothing will execute.',
                   'Run: easyci plan   to see the reasons, then add overrides in easyci.yml.', data)
    return data


def cmd_status(args):
    if args.watch and args.json:
        raise Fail(2, 'USAGE', '--watch prints sentences; use easyci wait --json instead.', 'Drop --json or --watch.')
    rd = get_run(args.run_id, args.repo)
    if args.watch:
        last = None
        while True:
            data = view(rd)
            if data['plain'] != last:
                say(args, data['plain'] + f' (run {data["run_id"]})')
                last = data['plain']
            if data['state'] in store.TERMINAL:
                return None
            time.sleep(5)
            safe_reap()
    return view(rd)


def cmd_list(args):
    root = snapshot.repo_root(args.repo or os.getcwd())
    if args.repo and not root:
        raise Fail(7, 'REPO_NOT_FOUND', f'{args.repo} is not a git repository.', 'Pass --repo with your project folder.')
    dirs = store.run_dirs(store.project_slug(root)) if root else []
    if not dirs and not args.repo:
        dirs = store.run_dirs()
    runs = [view(rd) for rd in reversed(dirs)]
    if args.state:
        runs = [r for r in runs if r['state'] == args.state]
    return {'runs': runs[:args.limit]}


def pick_job(rd, run, name):
    jobs = run.get('jobs', [])
    if name:
        if not any(j['name'] == name for j in jobs):
            raise Fail(7, 'JOB_NOT_FOUND', f'Run {run["run_id"]} has no job {name}.', 'See: easyci status ' + run['run_id'])
        return name
    for states in (('failed', 'timed_out', 'cancelled', 'interrupted'), ('running',)):
        for job in jobs:
            if job['state'] in states and (rd / 'jobs' / job['name'] / 'log.txt').exists():
                return job['name']
    with_logs = [j['name'] for j in jobs if (rd / 'jobs' / j['name'] / 'log.txt').exists()]
    return with_logs[-1] if with_logs else (jobs[0]['name'] if jobs else None)


def cmd_logs(args):
    if args.follow and args.json:
        raise Fail(2, 'USAGE', '--follow streams text; it cannot be combined with --json.', 'Drop --json or --follow.')
    rd = get_run(args.run_id, args.repo)
    run = store.read_json(rd / 'run.json')
    job = pick_job(rd, run, args.job)
    path = rd / 'jobs' / (job or '_') / 'log.txt'
    if args.follow:
        if args.json:
            raise Fail(2, 'USAGE', '--follow streams text; it cannot be combined with --json.', 'Drop --json or --follow.')
        pos = 0
        while True:
            if path.exists():
                with open(path, 'rb') as handle:
                    handle.seek(pos)
                    chunk = handle.read()
                    pos += len(chunk)
                if chunk:
                    sys.stdout.write(chunk.decode('utf-8', errors='replace'))
                    sys.stdout.flush()
            if (store.read_json(rd / 'run.json') or {}).get('state') in store.TERMINAL:
                return None
            time.sleep(1)
    lines = path.read_text(encoding='utf-8', errors='replace').splitlines() if path.exists() else []
    tail = lines[-args.tail:] if args.tail else lines
    data = {'run_id': run['run_id'], 'job': job, 'lines': tail, 'truncated': len(tail) < len(lines)}
    if not args.json:
        print('\n'.join(tail))
        return None
    return data


def cmd_wait(args):
    rd = get_run(args.run_id, args.repo)
    data = wait_for(rd, args.timeout * 60 if args.timeout else None)
    if data is None:
        raise Fail(6, 'WAIT_TIMEOUT', f'The run is still going after {args.timeout} minutes.',
                   'Wait again, or check: easyci status', view(rd))
    code, fail = finish_run(data)
    if fail:
        raise fail
    return data


def cmd_cancel(args):
    rd = get_run(args.run_id, args.repo)
    run = store.read_json(rd / 'run.json')
    if run.get('state') in store.TERMINAL:
        return view(rd)
    (rd / 'cancel').write_text(store.iso(), encoding='ascii')
    data = wait_for(rd, 30)
    if data is None:
        raise Fail(6, 'CANCEL_PENDING', 'Cancellation was requested but the run has not stopped within 30 seconds.',
                   'Check again with: easyci status', view(rd))
    return data


def cmd_explain(args):
    rd = get_run(args.run_id, args.repo)
    run = store.read_json(rd / 'run.json')
    data = {'run_id': run['run_id'], 'state': run['state'],
            **explain.explain(rd, run, store.read_json(rd / 'plan.json') or {})}
    if not args.json:
        say(args, data['summary'])
        for line in data['excerpt'][-10:]:
            say(args, '    ' + line)
        for tip in data['suggestions']:
            say(args, 'Suggestion: ' + tip)
        return None
    return data


def cmd_plan(args):
    info = plan_inputs(args)
    plan = info['plan']
    data = {**plan, 'repo': str(info['repo']), 'commit': info['commit'], 'base': info['base'],
            'easyci_yml': info['repro']['easyci_yml'], 'summary': plan_summary(plan)}
    if not args.json:
        say(args, f'Plan for {info["repo"]} at {info["commit"][:7]} (event {plan["event"]}'
                  + (f', profile {plan["profile"]}' if plan['profile'] else '') + '):')
        for line in data['summary']:
            say(args, '  ' + line)
    bad = [j for j in plan['jobs'] if j['predicted'] == 'unsupported' and j['reported']]
    if bad:
        raise Fail(4, 'UNSUPPORTED', f'{len(bad)} job(s) use workflow content EasyCI cannot run: '
                   + '; '.join(f'{j["id"]}: {j["predicted_reason"]}' for j in bad),
                   'Add an override in easyci.yml (skip, command, toolchains, skip_steps).', data)
    return data if args.json else None


def cmd_doctor(args):
    from easyci import doctor
    repo_info = None
    if args.repo:
        repo = find_repo(args.repo)
        checks = [doctor.check('repo', True, f'Repository: {repo}')]
        backends = []
        try:
            ns = argparse.Namespace(repo=str(repo), commit=None, ref=None, base=None, event=None, profile=None,
                                    job=None, workflow=None, backend=None, config=args.config, no_report=False)
            info = plan_inputs(ns)
            backends = [j['backend'] for j in info['plan']['jobs'] if not j['skip']]
            checks.append(doctor.check('workflows', True, f'{len(info["plan"]["workflows"])} workflow file(s); '
                                       + ', '.join(f'{j["id"]}->{j["backend"]}' for j in info['plan']['jobs'])))
            checks.append(doctor.check('github_remote', bool(info['repository']),
                                       f'GitHub repository: {info["repository"]}' if info['repository'] else
                                       'origin is not a GitHub remote; statuses will not be posted',
                                       'Add a GitHub origin remote to get statuses.', required=False))
        except Fail as error:
            checks.append(doctor.check('workflows', False, error.message, error.fix))
        repo_info = {'checks': checks, 'backends': backends}
    checks = doctor.run_checks(repo_info)
    problems = [c for c in checks if c['required'] and not c['ok']]
    if not args.json:
        for item in checks:
            say(args, doctor.sentence(item))
        say(args, 'Everything needed is ready.' if not problems else f'{len(problems)} problem(s) to fix.')
    data = {'checks': checks, 'ok': not problems}
    if problems:
        raise Fail(3, 'ENVIRONMENT', f'{len(problems)} required check(s) failed: ' + ', '.join(c['id'] for c in problems),
                   problems[0]['fix'], data)
    return data if args.json else None


def cmd_report(args):
    rd = get_run(args.run_id, args.repo)
    run = store.read_json(rd / 'run.json')
    if run.get('state') not in store.TERMINAL:
        raise Fail(6, 'RUN_LIVE', 'The run is still going; its supervisor reports as it goes.', 'Wait for it first.')
    report = store.read_json(rd / 'report.json') or {}
    info = report.get('github') or run.get('github') or {}
    if not info.get('enabled'):
        raise Fail(2, 'REPORTING_OFF', 'Reporting was off for this run (no GitHub remote, --no-report or report: false).',
                   'Start a new run with reporting on.')
    work = {**run, 'github': info}
    github.sync(rd, work, force=True)
    store.unseal_for_update(rd, ['report.json'])
    store.write_json(rd / 'report.json', {**report, 'github': work['github']})
    store.seal(rd)
    data = {'run_id': run['run_id'], 'posted': sorted(work['github']['contexts']), 'failed': work['github']['failed']}
    if data['failed']:
        raise Fail(3, 'REPORT_FAILED', f'Could not post {len(data["failed"])} status(es).',
                   'Check: gh auth status, then run easyci report again.', data)
    return data


def cmd_verify(args):
    rd = get_run(args.run_id, args.repo)
    mismatches = store.verify(rd)
    if mismatches is None:
        raise Fail(6, 'NOT_SEALED', 'This run has no sha256.json yet (it is still going or was never finished).',
                   'Wait for the run to finish.')
    data = {'run_id': rd.name, 'ok': not mismatches, 'mismatches': mismatches}
    if mismatches:
        raise Fail(1, 'TAMPERED', 'Evidence changed after sealing: ' + ', '.join(
            f'{m["path"]} ({m["problem"]})' for m in mismatches[:10]), 'Treat this run as untrustworthy; re-run it.', data)
    return data


def cmd_setup(args):
    """Get a repository ready: detect stacks, pick a backend, write a starter workflow and easyci.yml."""
    from easyci import onboard
    repo = find_repo(args.repo)
    result = onboard.plan(repo)
    if not args.json:
        for line in onboard.sentences(result):
            say(args, line)
    write = args.write or args.commit
    if result['files'] and not write and not args.json and sys.stdin.isatty():
        answer = input('Write these files now? [Y/n] ').strip().lower()
        write = answer in ('', 'y', 'yes')
    written = onboard.write(repo, result['files']) if write else []
    for item in result['files']:
        item['written'] = item['path'] in written
    result['written'] = written
    if written and not args.json:
        say(args, 'Wrote ' + ', '.join(written) + '.')
    if args.commit and written:
        try:
            result['commit'] = onboard.commit(repo, written)
        except onboard.CommitError as error:
            raise Fail(3, 'GIT_COMMIT_FAILED', f'The files were written but git could not commit them: {error}',
                       'Set your git name and email (git config --global user.name "..." and user.email "..."), '
                       'then run: easyci setup --commit', data=result) from None
        say(args, f'Committed them as {result["commit"][:7]} ("Add EasyCI checks").')
    elif written:
        say(args, 'Next: commit these files (EasyCI tests committed code), then run `easyci run`. '
                  'Or run `easyci setup --commit` to commit just these files.')
    if args.desktop:
        result['launcher'] = onboard.write_launcher(repo, sys.executable)
        say(args, f'Made a double-click launcher: {result["launcher"]}')
    if args.run:
        if written and not args.commit:
            raise Fail(2, 'UNCOMMITTED_SETUP', 'The new files are not committed yet, so a run would not see them.',
                       'Run: easyci setup --commit --run', data=result)
        ns = argparse.Namespace(**{**vars(args), 'commit': None, 'ref': None, 'base': None, 'profile': None,
                                   'event': None, 'job': None, 'workflow': None, 'backend': None, 'config': None,
                                   'wait': False, 'no_queue': False, 'no_report': False, 'keep': False})
        result['run'] = cmd_run(ns)
    return result


def cmd_clean(args):
    default = global_config().get('retention') or 20
    deleted, kept = store.clean(args.keep, default)
    return {'deleted': deleted, 'kept_per_project': kept}


# ---- parser and main -----------------------------------------------------------------------

def build_parser():
    top = Parser(add_help=False)
    top.add_argument('--json', action='store_true', help='print one JSON document on stdout')
    # Accepted after the command too. SUPPRESS keeps the subparser from resetting a `--json` given
    # before the command (`easyci --json plan`) back to False.
    common = Parser(add_help=False)
    common.add_argument('--json', action='store_true', default=argparse.SUPPRESS,
                        help='print one JSON document on stdout')
    parser = Parser(prog='easyci', description='Local-first CI for your own PC.', parents=[top])
    parser.add_argument('--version', action='version', version=f'easyci {VERSION}')
    sub = parser.add_subparsers(dest='command', parser_class=Parser)

    def add(name, func, help_):
        p = sub.add_parser(name, parents=[common], help=help_, description=help_)
        p.set_defaults(func=func)
        return p

    def selection(p):
        p.add_argument('--repo')
        p.add_argument('--commit')
        p.add_argument('--ref')
        p.add_argument('--base')
        p.add_argument('--profile')
        p.add_argument('--event', choices=['push', 'pull_request', 'workflow_dispatch'])
        p.add_argument('--job', action='append')
        p.add_argument('--workflow')
        p.add_argument('--backend')
        p.add_argument('--config', help='easyci.yml to use instead of the one at the tested commit')
    p = add('run', cmd_run, 'Start a run in the background and return its id.')
    selection(p)
    p.add_argument('--wait', action='store_true')
    p.add_argument('--no-queue', action='store_true')
    p.add_argument('--no-report', action='store_true')
    p.add_argument('--keep', action='store_true')
    for name, func, help_ in (('status', cmd_status, 'Show the state of a run (default: latest).'),
                              ('wait', cmd_wait, 'Block until a run ends; the exit code is the result.'),
                              ('cancel', cmd_cancel, 'Stop a run.'),
                              ('explain', cmd_explain, 'Explain in plain words why a run failed.'),
                              ('report', cmd_report, 'Post GitHub statuses again for a finished run.'),
                              ('verify', cmd_verify, 'Check that a finished run\'s evidence is unchanged.'),
                              ('logs', cmd_logs, 'Show a job log.')):
        p = add(name, func, help_)
        p.add_argument('run_id', nargs='?')
        p.add_argument('--repo')
        if name == 'status':
            p.add_argument('--watch', action='store_true')
        if name == 'wait':
            p.add_argument('--timeout', type=float, help='minutes')
        if name == 'logs':
            p.add_argument('--job')
            p.add_argument('--tail', type=int, default=200)
            p.add_argument('--follow', action='store_true')
    p = add('list', cmd_list, 'List recent runs.')
    p.add_argument('--repo')
    p.add_argument('--limit', type=int, default=20)
    p.add_argument('--state', choices=store.LIVE + store.TERMINAL)
    p = add('plan', cmd_plan, 'Show what a run would do, without running anything.')
    selection(p)
    p = add('doctor', cmd_doctor, 'Check that this PC (and optionally a repo) is ready.')
    p.add_argument('--repo')
    p.add_argument('--config')
    p = add('setup', cmd_setup, 'Get a project ready: detect it, pick where tests run, write a starter workflow.')
    p.add_argument('--repo')
    p.add_argument('--write', action='store_true', help='write the planned files (never overwrites)')
    p.add_argument('--commit', action='store_true', help='write and commit only those files')
    p.add_argument('--desktop', action='store_true', help='put a "Run checks" launcher on the Desktop')
    p.add_argument('--run', action='store_true', help='start a run afterwards')
    p = add('clean', cmd_clean, 'Delete old run evidence beyond retention.')
    p.add_argument('--keep', type=int)
    return parser


def emit(args_json, ok, data=None, error=None):
    doc = {'schema': SCHEMA, 'ok': ok}
    if data is not None:
        doc['data'] = data
    if error:
        doc['error'] = error
    if args_json:
        sys.stdout.write(json.dumps(doc, indent=2, sort_keys=True) + '\n')
        sys.stdout.flush()


def human(data):
    if isinstance(data, dict) and 'stacks' in data and 'files' in data:  # setup already spoke line by line
        run = data.get('run') or {}
        return f'{run["plain"]} (run {run["run_id"]})' if run.get('run_id') else 'Setup check done.'
    if isinstance(data, dict) and 'plain' in data and 'run_id' in data:
        return f'{data["plain"]} (run {data["run_id"]})'
    if isinstance(data, dict) and 'runs' in data:
        return '\n'.join(f'{r["run_id"]}  {r["state"]:<11} {r["commit"][:7]}  {r["plain"]}' for r in data['runs']) or 'No runs.'
    if isinstance(data, dict) and 'deleted' in data:
        return f'Deleted {len(data["deleted"])} old run(s).'
    if isinstance(data, dict) and 'mismatches' in data:
        return f'Evidence of run {data["run_id"]} is intact.'
    if isinstance(data, dict) and 'posted' in data:
        return f'Posted {len(data["posted"])} GitHub status(es).'
    return json.dumps(data, indent=2)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    as_json = '--json' in argv
    real_stdout = sys.stdout
    if as_json:
        sys.stdout = sys.stderr  # nothing but the final document may reach stdout
    try:
        args = build_parser().parse_args(argv)
        if not getattr(args, 'func', None):
            raise Fail(2, 'USAGE', 'No command given.', 'Run: easyci --help')
        safe_reap()
        data = args.func(args)
        sys.stdout = real_stdout
        if data is not None:
            if as_json:
                emit(True, True, data)
            else:
                print(human(data))
        elif as_json:
            emit(True, True, {})
        return 0
    except Fail as fail:
        sys.stdout = real_stdout
        error = {'code': fail.code, 'message': fail.message, 'fix': fail.fix}
        if as_json:
            emit(True, False, fail.data, error)
        else:
            if fail.data and isinstance(fail.data, dict) and 'plain' in fail.data:
                print(human(fail.data), file=sys.stderr)
            print(f'{fail.message}\nFix: {fail.fix}', file=sys.stderr)
        return fail.exit_code
    except SystemExit as exit_:
        sys.stdout = real_stdout
        code = exit_.code if isinstance(exit_.code, int) else 0
        if as_json:
            emit(True, code == 0, {} if code == 0 else None,
                 None if code == 0 else {'code': 'USAGE', 'message': 'invalid arguments', 'fix': 'Run: easyci --help'})
        return code
    except KeyboardInterrupt:
        sys.stdout = real_stdout
        return 130
    except Exception as error:  # noqa: BLE001 - exit 10 with a pointer to the log
        sys.stdout = real_stdout
        store.log(f'internal error: {traceback.format_exc()}')
        err = {'code': 'INTERNAL', 'message': f'EasyCI hit a bug: {error}',
               'fix': f'See {store.home() / "easyci.log"} and report the last lines.'}
        if as_json:
            emit(True, False, None, err)
        else:
            print(f'{err["message"]}\nFix: {err["fix"]}', file=sys.stderr)
        return 10
