"""Importer: .github/workflows/*.yml plus optional easyci.yml -> an ordered, honest plan."""
import fnmatch
import re

import yaml

from easyci import expr, store
from easyci.backends import version_matches  # noqa: F401 - re-exported for callers
from easyci.expr import Unsupported


class ConfigError(Exception):
    pass


TOP_KEYS = {'version', 'backend', 'report', 'profiles', 'jobs', 'retention', 'gpu', 'busy_paths', 'busy_stale_minutes',
            'busy_wait_minutes'}
JOB_KEYS = {'skip', 'when', 'backend', 'command', 'timeout_minutes', 'stall_minutes', 'stall_action', 'skip_steps',
            'evidence', 'required_env', 'toolchains', 'on_kill', 'on_kill_minutes'}
PROFILE_KEYS = {'event', 'jobs', 'workflow'}
WORKFLOW_IGNORED = {'name', 'on', True, 'env', 'defaults', 'permissions', 'concurrency', 'jobs', 'run-name'}
JOB_IGNORED = {'permissions', 'concurrency'}
JOB_SUPPORTED = {'name', 'needs', 'if', 'runs-on', 'steps', 'env', 'defaults', 'outputs', 'timeout-minutes'} | JOB_IGNORED
STEP_KEYS = {'name', 'id', 'if', 'run', 'uses', 'with', 'env', 'shell', 'working-directory', 'continue-on-error',
             'timeout-minutes'}
SETUP = {'actions/setup-node': ('node', 'node-version'), 'actions/setup-python': ('python', 'python-version'),
         'julia-actions/setup-julia': ('julia', 'version'), 'actions/setup-java': ('java', 'java-version'),
         'actions/setup-go': ('go', 'go-version')}
CACHE = ('actions/cache', 'actions/cache/restore', 'actions/cache/save')
SHELLS = {'bash', 'sh', 'pwsh', 'powershell', 'cmd'}
BACKEND = re.compile(r'(native|fake|wsl(:[A-Za-z0-9._-]+)?)\Z')
LOCKFILES = ('package-lock.json', 'Project.toml', 'Manifest.toml', 'uv.lock', 'poetry.lock')


def backend_id(value, where):
    if not isinstance(value, str) or not BACKEND.match(value):
        raise ConfigError(f'{where}: backend must be native, wsl or wsl:<distro> (got {value!r})')
    return value


def number(value, where, minimum=0.0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= minimum:
        raise ConfigError(f'{where} must be a positive number of minutes')
    return value


def load_config(text, source='easyci.yml'):
    """Parse and validate easyci.yml; raise ConfigError with a precise message."""
    try:
        cfg = yaml.safe_load(text) or {}
    except yaml.YAMLError as error:
        raise ConfigError(f'{source} is not valid YAML: {error}') from None
    if not isinstance(cfg, dict):
        raise ConfigError(f'{source} must be a mapping')
    unknown = set(cfg) - TOP_KEYS
    if unknown:
        raise ConfigError(f'{source}: unknown key(s) {sorted(map(str, unknown))}')
    if cfg.get('version', 1) != 1:
        raise ConfigError(f'{source}: only version 1 is supported')
    if 'backend' in cfg:
        backend_id(cfg['backend'], source)
    if not isinstance(cfg.get('report', True), bool):
        raise ConfigError(f'{source}: report must be true or false')
    if cfg.get('gpu', 'optional') not in ('required', 'optional'):
        raise ConfigError(f'{source}: gpu must be required or optional')
    if not isinstance(cfg.get('busy_paths', []), list) or not all(isinstance(p, str) and p.strip()
                                                                  for p in cfg.get('busy_paths', [])):
        raise ConfigError(f'{source}: busy_paths must be a list of glob strings')
    for key in ('busy_stale_minutes', 'busy_wait_minutes'):
        if key in cfg:
            number(cfg[key], f'{source}: {key}')
    if 'retention' in cfg and (type(cfg['retention']) is not int or cfg['retention'] < 1):
        raise ConfigError(f'{source}: retention must be a positive integer')
    for name, prof in (cfg.get('profiles') or {}).items():
        if not isinstance(prof, dict) or set(prof) - PROFILE_KEYS:
            raise ConfigError(f'{source}: profile {name} may only have {sorted(PROFILE_KEYS)}')
        if prof.get('event', 'pull_request') not in ('push', 'pull_request', 'workflow_dispatch'):
            raise ConfigError(f'{source}: profile {name} event must be push, pull_request or workflow_dispatch')
        if not isinstance(prof.get('jobs', []), list):
            raise ConfigError(f'{source}: profile {name} jobs must be a list')
    for name, job in (cfg.get('jobs') or {}).items():
        where = f'{source}: jobs.{name}'
        if not isinstance(job, dict) or set(job) - JOB_KEYS:
            raise ConfigError(f'{where} may only have {sorted(JOB_KEYS)}')
        if job.get('when', 'workflow') not in ('always', 'workflow'):
            raise ConfigError(f'{where}.when must be always or workflow')
        if job.get('stall_action', 'mark') not in ('mark', 'kill'):
            raise ConfigError(f'{where}.stall_action must be mark or kill')
        if 'backend' in job:
            backend_id(job['backend'], where)
        for key in ('timeout_minutes', 'stall_minutes', 'on_kill_minutes'):
            if key in job:
                number(job[key], f'{where}.{key}')
        for key in ('command', 'on_kill'):
            if key in job and (not isinstance(job[key], str) or not job[key].strip()):
                raise ConfigError(f'{where}.{key} must be a non-empty string')
        for key in ('evidence', 'required_env', 'skip_steps'):
            if not isinstance(job.get(key, []), list):
                raise ConfigError(f'{where}.{key} must be a list')
        for name_ in job.get('required_env', []):
            if not isinstance(name_, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name_):
                raise ConfigError(f'{where}.required_env has an invalid variable name {name_!r}')
        tools = job.get('toolchains', {})
        if not isinstance(tools, dict) or any(v not in ('ignore', 'verify') for v in tools.values()):
            raise ConfigError(f'{where}.toolchains values must be ignore or verify')
    return cfg


def glob_regex(pattern):
    """GitHub's filter pattern syntax (workflow-syntax "Filter pattern cheat sheet"): `*` stays within
    one folder, `**` crosses folders and `**/` may match no folder at all (so `**/*.py` matches
    `main.py`), `?` makes the preceding character optional, `+` repeats it, `[...]` is a character
    class, and anything else is literal."""
    out, i, p = [], 0, str(pattern)
    while i < len(p):
        c = p[i]
        if p.startswith('**/', i):
            out.append('(?:.*/)?')
            i += 3
        elif p.startswith('**', i):
            out.append('.*')
            i += 2
        elif c == '*':
            out.append('[^/]*')
            i += 1
        elif c in '?+' and out and out[-1][-1:] not in ('?', '+', '*'):
            out[-1] = f'(?:{out[-1]}){c}'  # zero or one / one or more of the preceding character
            i += 1
        elif c == '[' and ']' in p[i + 2:]:
            end = p.index(']', i + 2)
            body = p[i + 1:end]
            negate = body.startswith('!')
            body = re.escape(body[1:] if negate else body).replace(r'\-', '-')  # keep ranges like a-z
            out.append('[' + ('^' if negate else '') + body + ']')
            i = end + 1
        else:
            out.append(re.escape(c))
            i += 1
    return ''.join(out)


def glob_match(pattern, value):
    return re.fullmatch(glob_regex(pattern), value) is not None


def filter_match(patterns, value):
    """GitHub filter semantics: patterns apply in order and a `!pattern` that matches excludes the
    value again, so `['**', '!release/**']` matches everything except release branches."""
    matched = False
    for pattern in patterns or []:
        pattern = str(pattern)
        if pattern.startswith('!'):
            if glob_match(pattern[1:], value):
                matched = False
        elif glob_match(pattern, value):
            matched = True
    return matched


def triggers(doc):
    on = doc.get('on', doc.get(True))
    if isinstance(on, str):
        return {on: {}}
    if isinstance(on, list):
        return {str(k): {} for k in on}
    if isinstance(on, dict):
        return {str(k): (v if isinstance(v, dict) else {}) for k, v in on.items()}
    return {}


def applies(doc, path, inp):
    """(applies, note) for this workflow and the requested event."""
    trig, event = triggers(doc), inp['event']
    wanted = inp.get('workflow')
    if wanted and wanted not in (path.rsplit('/', 1)[-1], doc.get('name')):
        return False, f'not the selected workflow {wanted}'
    if event == 'workflow_dispatch':
        return 'workflow_dispatch' in trig and bool(wanted), 'workflow_dispatch: selectable by name only'
    if event not in trig:
        return False, f'not triggered by {event}'
    filt, notes = trig[event], []
    branch = inp['ref_name'] if event == 'push' else inp['default_branch']
    if event == 'push' and ('tags' in filt or 'tags-ignore' in filt) and not ('branches' in filt or 'branches-ignore' in filt):
        return False, 'push filter only matches tags'
    if 'branches' in filt and not filter_match(filt['branches'], branch):
        return False, f'branch {branch} not in branches filter'
    if filter_match(filt.get('branches-ignore'), branch):
        return False, f'branch {branch} in branches-ignore'
    if 'paths' in filt or 'paths-ignore' in filt:
        changed = inp.get('changed')
        if changed is None:
            notes.append('paths filters not evaluated (no base to compare with); workflow included')
        elif 'paths' in filt and not any(filter_match(filt['paths'], f) for f in changed):
            return False, 'no changed file matches the paths filter'
        elif 'paths-ignore' in filt and changed and all(filter_match(filt['paths-ignore'], f) for f in changed):
            return False, 'every changed file is in paths-ignore'
    return True, '; '.join(notes) or f'triggered by {event}'


def github_context(inp):
    ref_name = inp['ref_name']
    gh = {'sha': inp['commit'], 'ref': ref_name if ref_name.startswith('refs/') else f'refs/heads/{ref_name}',
          'ref_name': ref_name, 'repository': inp.get('repository') or '', 'event_name': inp['event'], 'event': {}}
    if inp['event'] == 'pull_request':
        gh['event'] = {'pull_request': {'head': {'sha': inp['commit']}, 'base': {'sha': inp['base']}}}
    return gh


def version_spec(requested, tool):
    text = str(requested).strip().lstrip('v')
    parts = text.split('.')
    if not text or not all(p.isdigit() or p in ('x', '*') for p in parts):
        raise Unsupported(f'toolchain_spec_unsupported:{tool} {requested}')
    return text


def resolve_shell(step_shell, defaults, backend, runs_on):
    shell = step_shell or defaults.get('shell')
    if shell is None:
        shell = 'pwsh' if backend == 'native' and 'windows' in runs_on else 'bash'
    if shell not in SHELLS:
        raise Unsupported(f'unsupported_shell:{shell}')
    if backend.startswith('wsl') and shell in ('pwsh', 'powershell', 'cmd'):
        raise Unsupported(f'unsupported_shell:{shell} on {backend}')
    return shell


def map_backend(job, jcfg, cfg, inp):
    if inp.get('backend'):
        return backend_id(inp['backend'], '--backend'), 'from --backend'
    if 'backend' in jcfg:
        return jcfg['backend'], 'from easyci.yml jobs.' + job
    return None, None


def runs_on_backend(runs_on, cfg, inp):
    labels = [runs_on] if isinstance(runs_on, str) else runs_on if isinstance(runs_on, list) else []
    labels = [str(x) for x in labels]
    if not labels or any('${{' in x for x in labels):
        raise Unsupported(f'unsupported_runs_on:{runs_on}')
    joined = ' '.join(labels)
    if any(x.startswith('ubuntu') for x in labels):
        return cfg.get('backend') or inp.get('default_backend') or 'wsl', f'runs-on {joined}'
    if any(x.startswith('windows') for x in labels):
        return 'native', f'runs-on {joined}'
    raise Unsupported(f'unsupported_runs_on:{joined}')


def step_name(step):
    if step.get('name'):
        return str(step['name'])
    if step.get('run'):
        return 'Run ' + str(step['run']).strip().splitlines()[0][:80]
    return 'Run ' + str(step.get('uses', 'step'))


def plan_step(index, step, ctx):
    """Map one workflow step; raise Unsupported with a precise reason."""
    unknown = set(step) - STEP_KEYS
    if unknown:
        raise Unsupported(f'unsupported_step_key:{sorted(unknown)[0]}')
    for key in ('name', 'run', 'env', 'with', 'working-directory'):
        expr.check_template(step.get(key))
    if 'if' in step:
        expr.parse_condition(step['if'])
    out = {'index': index, 'name': step_name(step), 'id': step.get('id'), 'if': step.get('if'),
           'env': {k: expr.to_str(v) for k, v in (step.get('env') or {}).items()},
           'continue_on_error': step.get('continue-on-error', False),
           'timeout_minutes': step.get('timeout-minutes'),
           'working_directory': step.get('working-directory') or ctx['defaults'].get('working-directory')}
    if not isinstance(out['continue_on_error'], bool):
        raise Unsupported('unsupported_expression:continue-on-error')
    if 'run' in step:
        out.update(kind='run', run=str(step['run']),
                   shell=resolve_shell(step.get('shell'), ctx['defaults'], ctx['backend'], ctx['runs_on']),
                   decision='run step')
        return out
    uses, with_ = str(step.get('uses', '')), step.get('with') or {}
    name, _, ver = uses.partition('@')
    out.update(uses=uses, **{'with': {k: expr.to_str(v) if not isinstance(v, str) else v for k, v in with_.items()}})
    if name == 'actions/checkout':
        major = re.fullmatch(r'v(\d+)(\.\d+)*', ver)
        if not major or not 3 <= int(major.group(1)) <= 5:
            raise Unsupported(f'unsupported_uses:{uses}')
        if str(with_.get('submodules', 'false')).lower() not in ('false', ''):
            raise Unsupported('unsupported_checkout:submodules')
        if with_.get('repository') not in (None, '', ctx['github']['repository'], '${{ github.repository }}'):
            raise Unsupported(f'unsupported_checkout:repository {with_["repository"]}')
        ref = expr.interpolate(str(with_.get('ref', '')), {'github': ctx['github']})
        if ref not in ('', ctx['github']['sha'], ctx['github']['ref_name'], ctx['github']['ref']):
            raise Unsupported(f'unsupported_checkout:ref {ref}')
        out.update(kind='checkout', path=with_.get('path') or '', decision='clean clone of the run commit from the bundle')
    elif name in SETUP:
        tool, key = SETUP[name]
        if any(k.endswith('-file') for k in with_):
            raise Unsupported(f'toolchain_spec_unsupported:{tool} version file')
        requested = with_.get(key)
        if requested is not None:
            version_spec(requested, tool)
        out.update(kind='toolchain', tool=tool, requested=None if requested is None else str(requested),
                   decision=f'verify {tool} {requested or "(any)"} on the backend; nothing is installed')
    elif name == 'actions/upload-artifact' and ver[:2] in ('v3', 'v4'):
        if not with_.get('path'):
            raise Unsupported('unsupported_upload_artifact:path missing')
        out.update(kind='upload-artifact', decision='copy files into the run evidence')
    elif name in CACHE:
        out.update(kind='cache', decision='cache step skipped (local)')
    else:
        raise Unsupported(f'unsupported_uses:{uses or "?"}')
    return out


def plan_job(wf_path, wf, jid, job, jcfg, cfg, inp, gh):
    reasons, notes = [], []
    if not isinstance(job, dict):
        raise ConfigError(f'{wf_path}: job {jid} must be a mapping')
    needs = job.get('needs') or []
    needs = [needs] if isinstance(needs, str) else [str(n) for n in needs]
    out = {'id': jid, 'workflow': wf_path, 'name': str(job.get('name') or jid), 'needs': needs,
           'if': job.get('if'), 'when': jcfg.get('when', 'workflow'), 'skip': bool(jcfg.get('skip')),
           'runs_on': job.get('runs-on'), 'outputs': job.get('outputs') or {}, 'steps': [], 'toolchains': [],
           'timeout_minutes': jcfg.get('timeout_minutes') or job.get('timeout-minutes') or 360,
           'stall_minutes': jcfg.get('stall_minutes', 20), 'stall_action': jcfg.get('stall_action', 'mark'),
           'env': {**{k: expr.to_str(v) for k, v in (wf.get('env') or {}).items()},
                   **{k: expr.to_str(v) for k, v in (job.get('env') or {}).items()}},
           # Kept apart so the workflow layer is resolved before job values that reference it.
           'env_layers': [{k: expr.to_str(v) for k, v in (layer or {}).items()} for layer in (wf.get('env'), job.get('env'))],
           'command': jcfg.get('command'), 'on_kill': jcfg.get('on_kill'),
           'on_kill_minutes': jcfg.get('on_kill_minutes', 10), 'evidence': list(jcfg.get('evidence', [])),
           'required_env': list(jcfg.get('required_env', [])), 'toolchain_policy': dict(jcfg.get('toolchains', {})),
           'unsupported': reasons, 'notes': notes}
    if isinstance(out['timeout_minutes'], str):
        reasons.append('unsupported_expression:timeout-minutes')
        out['timeout_minutes'] = 360
    backend, why = map_backend(jid, jcfg, cfg, inp)
    try:
        if backend is None:
            backend, why = runs_on_backend(job.get('runs-on'), cfg, inp)
    except Unsupported as error:
        reasons.append(str(error))
        backend = backend or 'native'
    out['backend'], out['backend_reason'] = backend, why
    if out['skip']:
        notes.append('skipped by easyci.yml')
        return out
    if out['when'] != 'always' and out['if'] not in (None, ''):
        try:
            expr.parse_condition(out['if'])
        except Unsupported as error:
            reasons.append(str(error))
    elif out['when'] == 'always' and out['if']:
        notes.append(f'workflow if ignored (when: always): {out["if"]}')
    try:
        expr.check_template(out['env'])
        expr.check_template(out['outputs'])
    except Unsupported as error:
        reasons.append(str(error))
    if out['command']:
        out['steps'] = [{'index': 1, 'name': 'Run delegated command', 'id': None, 'if': None, 'env': {},
                         'continue_on_error': False, 'timeout_minutes': None, 'working_directory': None,
                         'kind': 'run', 'run': out['command'], 'shell': 'bash',
                         'decision': 'easyci.yml command replaces every workflow step'}]
        notes.append('steps replaced by easyci.yml command; uses: not evaluated')
        return out
    for key in sorted(set(job) - JOB_SUPPORTED):
        if key == 'strategy' and not (job[key] or {}).get('matrix'):
            continue
        reasons.append(f'unsupported_job_key:{key}' + (' (matrix is Phase 1)' if key == 'strategy' else ''))
    defaults = {**((wf.get('defaults') or {}).get('run') or {}), **((job.get('defaults') or {}).get('run') or {})}
    skip_steps = jcfg.get('skip_steps', [])
    ctx = {'defaults': defaults, 'backend': backend, 'runs_on': str(job.get('runs-on')), 'github': gh}
    for index, step in enumerate(job.get('steps') or [], 1):
        if not isinstance(step, dict):
            raise ConfigError(f'{wf_path}: job {jid} step {index} must be a mapping')
        if index in skip_steps or step_name(step) in skip_steps or step.get('name') in skip_steps:
            out['steps'].append({'index': index, 'name': step_name(step), 'kind': 'skipped', 'if': None,
                                 'decision': 'skipped by easyci.yml skip_steps'})
            continue
        try:
            mapped = plan_step(index, step, ctx)
        except Unsupported as error:
            reasons.append(str(error))
            mapped = {'index': index, 'name': step_name(step), 'kind': 'unsupported', 'if': step.get('if'),
                      'uses': step.get('uses'), 'decision': str(error)}
        out['steps'].append(mapped)
        if mapped['kind'] == 'toolchain' and out['toolchain_policy'].get(mapped['tool']) != 'ignore':
            out['toolchains'].append({'tool': mapped['tool'], 'requested': mapped['requested'], 'step': index,
                                      'if': mapped.get('if')})
    if not out['steps']:
        reasons.append('unsupported_job:no steps')
    return out


def order_jobs(jobs):
    by_id, ordered, state = {j['id']: j for j in jobs}, [], {}

    def visit(jid, chain):
        if state.get(jid) == 'done':
            return
        if state.get(jid) == 'active':
            raise ConfigError(f'needs: cycle {" -> ".join(chain + [jid])}')
        state[jid] = 'active'
        for need in by_id[jid]['needs']:
            if need not in by_id:
                raise ConfigError(f'job {jid} needs unknown job {need}')
            visit(need, chain + [jid])
        state[jid] = 'done'
        ordered.append(by_id[jid])
    for job in jobs:
        visit(job['id'], [])
    return ordered


def parse_workflow(path, data):
    try:
        doc = yaml.safe_load(data)
    except yaml.YAMLError as error:
        raise ConfigError(f'{path} is not valid YAML: {error}') from None
    if not isinstance(doc, dict) or not isinstance(doc.get('jobs'), dict):
        raise ConfigError(f'{path} has no jobs mapping')
    return doc


def build_plan(workflows, cfg, inp):
    """workflows: [(path, bytes)]; cfg: validated easyci.yml dict or {}; inp: run inputs (see cli)."""
    cfg = cfg or {}
    inp = dict(inp)
    profile = inp.get('profile')
    selection = inp.get('jobs') or None
    if profile:
        prof = (cfg.get('profiles') or {}).get(profile)
        if prof is None:
            raise ConfigError(f'profile {profile} is not defined in easyci.yml')
        inp['event'] = prof.get('event', inp['event'])
        selection = selection or prof.get('jobs') or None
        inp['workflow'] = inp.get('workflow') or prof.get('workflow')
    gh = github_context(inp)
    docs, plan_wfs, jobs, all_ids = [], [], [], set()
    for path, data in workflows:
        doc = parse_workflow(path, data)
        ok, note = applies(doc, path, inp)
        plan_wfs.append({'file': path, 'name': doc.get('name') or path, 'sha256': store.digest_bytes(data),
                         'applies': ok, 'note': note})
        all_ids.update(map(str, doc['jobs']))
        if ok:
            docs.append((path, doc))
    for jid in (cfg.get('jobs') or {}):
        if jid not in all_ids:
            raise ConfigError(f'easyci.yml names job {jid}, which is not in any workflow')
    seen = {}
    for path, doc in docs:
        for jid in doc['jobs']:
            seen.setdefault(str(jid), []).append(path)
    for path, doc in docs:
        for jid, job in doc['jobs'].items():
            jid = str(jid)
            if len(seen[jid]) > 1:
                raise ConfigError(f'job {jid} exists in several workflows ({", ".join(seen[jid])}); '
                                  'pick one with --workflow or a profile workflow:')
            jcfg = (cfg.get('jobs') or {}).get(jid, {})
            jobs.append(plan_job(path, doc, jid, job, jcfg, cfg, inp, gh))
    ordered = order_jobs(jobs)
    if selection:
        by_id = {j['id']: j for j in ordered}
        missing = [s for s in selection if s not in by_id]
        if missing:
            raise ConfigError(f'selected job(s) not found for event {inp["event"]}: {", ".join(missing)}')
        include = set()

        def add(jid):
            if jid not in include:
                include.add(jid)
                for need in by_id[jid]['needs']:
                    add(need)
        for jid in selection:
            add(jid)
        ordered = [j for j in ordered if j['id'] in include]
    for job in ordered:
        job['reported'] = (job['id'] in selection) if selection else not job['skip']
    plan = {'event': inp['event'], 'profile': profile, 'workflow': inp.get('workflow'), 'github': gh,
            'workflows': plan_wfs, 'jobs': ordered, 'gpu': cfg.get('gpu', 'optional'),
            'report': cfg.get('report', True)}
    predict(plan)
    return plan


def predict(plan):
    """Literal plan-time reading: outputs of earlier jobs are unknown (empty) until run time."""
    results = {}
    for job in plan['jobs']:
        needs = {n: {'result': results.get(n, 'skipped'), 'outputs': {}} for n in job['needs']}
        results_ = [v['result'] for v in needs.values()]
        status = ('success' if all(r == 'success' for r in results_)
                  else 'failure' if any(r == 'failure' for r in results_) else 'skipped')  # same rule as the supervisor
        if job['skip']:
            state, reason = 'skipped', 'skipped_by_config'
        elif job['unsupported']:
            state, reason = 'unsupported', job['unsupported'][0]
        elif job['when'] == 'always':
            state, reason = 'run', None
        else:
            ok = expr.condition(job['if'], {'github': plan['github'], 'needs': needs, 'status': status,
                                            'runner': {'os': 'Linux'}, 'env': {}})
            state, reason = ('run', None) if ok else ('skipped', 'condition_false')
        job['predicted'], job['predicted_reason'] = state, reason
        if reason == 'condition_false' and 'needs.' in str(job['if']):
            job['predicted_note'] = 'outputs of earlier jobs are only known at run time and read as empty here'
        job['run_steps'] = sum(1 for s in job['steps'] if s['kind'] == 'run')
        job['mapped'] = [f'{s["kind"]}:{s.get("uses")}' for s in job['steps'] if s['kind'] not in ('run', 'skipped')]
        results[job['id']] = 'success' if state == 'run' else 'skipped' if state == 'skipped' else 'failure'
    return plan


def lockfile_hashes(paths, read):
    out = {}
    for path in paths:
        base = path.rsplit('/', 1)[-1]
        if base in LOCKFILES or fnmatch.fnmatch(base, 'requirements*.txt'):
            data = read(path)
            if data is not None and '/node_modules/' not in f'/{path}':
                out[path] = store.digest_bytes(data)
    return out
