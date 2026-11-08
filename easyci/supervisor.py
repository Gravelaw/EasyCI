"""The detached per-run supervisor: queue, snapshot, jobs, heartbeat, stall/timeout/cancel, seal.

It is the only process that writes run.json while the run is live (the reaper writes it only
after this process is gone).
"""
import os
import pathlib
import posixpath
import shutil
import re
import signal
import sys
import threading
import time
import traceback

from easyci import backends, expr, github, launch, snapshot, store, workflow

PATH_SEP = {True: ':', False: os.pathsep}  # wsl: Linux PATH; native: Windows PATH
RESULT = {'passed': 'success', 'skipped': 'skipped', 'cancelled': 'cancelled'}
SECRET_NAME = re.compile(r'TOKEN|SECRET|PASSWORD|KEY', re.I)
TOKEN_PATTERN = re.compile(rb'\b(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})')


def poll_s():
    return store.setting('EASYCI_POLL_S', 0.5)


def keep_awake(on):
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000000 | (0x1 if on else 0))  # CONTINUOUS|SYSTEM_REQUIRED


def ignore_console_signals():
    for name in ('SIGINT', 'SIGBREAK', 'SIGHUP'):
        if hasattr(signal, name):
            try:
                signal.signal(getattr(signal, name), signal.SIG_IGN)
            except (OSError, ValueError):
                pass
    if os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetConsoleCtrlHandler(None, True)


def toolchain_problem(job, tool, probe):
    """(reason, fix) when the backend lacks the requested toolchain, else None."""
    where = job['backend'].replace('wsl:', 'WSL distro ')
    actual = probe.get(tool['tool'])
    if not actual:
        return (f'toolchain_missing:{tool["tool"]}',
                f'Install {tool["tool"]} on {where}, or set toolchains: {{{tool["tool"]}: ignore}} for job {job["id"]} in easyci.yml.')
    if tool['requested'] and not backends.version_matches(tool['requested'], actual):
        have = re.search(r'\d+(?:\.\d+)*', actual).group(0)
        return (f'toolchain_mismatch:{tool["tool"]} {have} != {tool["requested"]}',
                f'{where} has {tool["tool"]} {have} but the workflow asks for {tool["requested"]}. Install '
                f'{tool["tool"]} {tool["requested"]} there, pick another backend, or set toolchains: '
                f'{{{tool["tool"]}: ignore}} for job {job["id"]} in easyci.yml.')
    return None


def inside_workspace(path):
    """True for a relative path that cannot climb out of the workspace (no drive, root or `..`)."""
    norm = str(path).replace('\\', '/')
    if not norm.strip() or norm.startswith('/') or re.match(r'[A-Za-z]:', norm):
        return False
    return '..' not in norm.split('/')


def parse_kv(text):
    out, lines, i = {}, text.splitlines(), 0
    while i < len(lines):
        line = lines[i]
        if '<<' in line and ('=' not in line or line.index('<<') < line.index('=')):
            key, delim = line.split('<<', 1)
            buf, i = [], i + 1
            while i < len(lines) and lines[i] != delim:
                buf.append(lines[i])
                i += 1
            out[key] = '\n'.join(buf)
        elif '=' in line:
            key, value = line.split('=', 1)
            out[key] = value
        i += 1
    return out


class Cancelled(Exception):
    pass


class Supervisor:
    def __init__(self, rd):
        self.rd = pathlib.Path(rd)
        self.request = store.read_json(self.rd / 'request.json')
        self.plan = store.read_json(self.rd / 'plan.json')
        self.run = store.read_json(self.rd / 'run.json')
        self.run_id = self.run['run_id']
        self.beat = {'phase': 'queued', 'job': None, 'step': None, 'child': None}
        self.stop = threading.Event()
        self.cancel_requested = False
        self.locked = False
        self.backends, self.probes = {}, {}
        self.outputs = {}
        self.secrets = [os.environ[n].encode() for n in ('GH_TOKEN', 'GITHUB_TOKEN') if os.environ.get(n)]
        self.last_save = 0

    # ---- bookkeeping -----------------------------------------------------------------------
    def save(self):
        self.last_save = time.time()
        store.write_json(self.rd / 'run.json', self.run)

    def write_beat(self):
        store.write_json(self.rd / 'heartbeat.json', {'at': store.iso(), 'pid': os.getpid(), **self.beat})

    def heartbeat_loop(self):
        while not self.stop.wait(store.heartbeat_s()):
            try:
                self.write_beat()
            except OSError as error:
                self.log(f'{self.run_id}: heartbeat write failed: {error}')

    def job_rec(self, jid):
        return next(j for j in self.run['jobs'] if j['name'] == jid)

    def set_state(self, state):
        self.run['state'] = state
        self.beat['phase'] = state
        if state == 'preparing':
            self.run['started_at'] = store.iso()
        self.save()
        self.write_beat()

    def cancelled(self):
        if not self.cancel_requested and (self.rd / 'cancel').exists():
            self.cancel_requested = True
        return self.cancel_requested

    def report(self):
        try:
            github.sync(self.rd, self.run)
        except Exception as error:  # noqa: BLE001 - reporting never fails a run
            self.log(f'{self.run_id}: reporting error {error}')
        self.save()

    def mask(self, text):
        return self.redact(str(text).encode('utf-8')).decode('utf-8', errors='replace')

    def log(self, text):
        store.log(self.mask(text))

    def redact(self, data):
        # Whole values first, then (like GitHub) each line of a multiline secret such as a private
        # key, because the log is redacted line by line and no single line holds the whole value.
        parts = [v for v in self.secrets if v]
        parts += [line.strip() for v in parts if b'\n' in v for line in v.splitlines() if len(line.strip()) >= 4]
        for value in sorted(set(parts), key=len, reverse=True):
            data = data.replace(value, b'***')
        return TOKEN_PATTERN.sub(b'***', data)

    # ---- lifecycle -------------------------------------------------------------------------
    def execute(self):
        (self.rd / 'supervisor.pid').write_text(str(os.getpid()), encoding='ascii')
        self.run['supervisor_pid'] = os.getpid()
        for _ in range(50):  # the CLI writes launch.json right after spawning us
            info = store.read_json(self.rd / 'launch.json')
            if info:
                self.run['launch'] = info.get('launch')
                break
            time.sleep(0.1)
        self.save()
        self.write_beat()
        self.hb_thread = threading.Thread(target=self.heartbeat_loop, daemon=True)
        self.hb_thread.start()
        if not self.wait_queue():
            return self.finalize()
        self.set_state('preparing')
        keep_awake(True)
        self.report()
        try:
            self.prepare()
        except Cancelled:
            return self.finalize()
        self.set_state('running')
        for job in self.plan['jobs']:
            self.run_job(job)
        self.finalize()

    def wait_queue(self):
        no_queue = self.request['options'].get('no_queue')
        while True:
            if self.cancelled():
                return False
            store.reap(exclude=self.run_id)
            mine = [t for t in store.tickets() if store.ticket_run(t) == self.run_id]
            if not mine:
                store.add_ticket(self.run_id)
                continue
            first = store.tickets()[0]
            if store.ticket_run(first) == self.run_id and store.try_lock(self.run_id, os.getpid()):
                self.locked = True
                store.remove_ticket(self.run_id)
                self.run.pop('queued_behind', None)
                return True
            if no_queue:
                self.run['reason'] = 'busy'
                self.cancel_requested = True
                return False
            owner = (store.lock_owner() or {}).get('run_id') or store.ticket_run(first)
            if self.run.get('queued_behind') != owner:
                self.run['queued_behind'] = owner
                self.save()
            time.sleep(poll_s())

    def backend(self, backend_id):
        if backend_id not in self.backends:
            self.backends[backend_id] = backends.get(backend_id)
        return self.backends[backend_id]

    def wait_external(self):
        """Wait while a runner started outside EasyCI is busy on this repository (busy_paths).

        A matching file is busy if it is JSON with `status: running` and something in its folder
        changed within busy_stale_minutes; old 'running' summaries left by a crash do not block.
        """
        busy = self.plan.get('busy') or {}
        if not busy.get('paths'):
            return
        deadline = time.time() + float(busy.get('wait_minutes', 120)) * 60
        while True:
            found = store.busy_file(self.request['repo'], busy['paths'], float(busy.get('stale_minutes', 15)) * 60)
            if not found:
                if self.run.pop('waiting_for', None):
                    self.save()
                return
            if self.run.get('waiting_for') != found:
                self.run['waiting_for'] = found
                self.log(f'{self.run_id}: waiting for another runner ({found})')
                self.save()
            if self.cancelled():
                raise Cancelled
            if time.time() > deadline:
                self.run['reason'] = 'busy_timeout'
                self.cancel_requested = True
                raise Cancelled
            time.sleep(max(poll_s(), min(30.0, store.setting('EASYCI_BUSY_POLL_S', 30.0))))

    def prepare(self):
        self.wait_external()
        repo, commit, base = self.request['repo'], self.run['commit'], self.run['base']
        self.beat['step'] = 'snapshot'
        try:
            self.run['tree'] = snapshot.make_bundle(repo, commit, base, self.rd / 'source.bundle')
            self.run['bundle_sha256'] = store.digest(self.rd / 'source.bundle')
            if self.plan.get('data'):
                self.run['data'] = store.data_hashes(repo, self.plan['data'])
        except (snapshot.GitError, OSError) as error:
            self.fail_all('failed', f'snapshot_failed:{error}')
            raise Cancelled from None
        links = snapshot.links_in_tree(repo, commit)
        if links:
            self.fail_all('unsupported', f'unsupported_tree:symlink or submodule {links[0]}')
            raise Cancelled
        tools = {}
        for job in self.plan['jobs']:
            if job['skip'] or job['unsupported']:
                continue
            need = tools.setdefault(job['backend'], {'git'})
            need.update(t['tool'] for t in job['toolchains'])
            if self.plan.get('gpu') == 'required':
                need.add('nvidia-smi')
        self.run.setdefault('toolchains', {})
        self.run.setdefault('platforms', {})
        for backend_id, need in tools.items():
            if self.cancelled():
                raise Cancelled
            try:
                backend = self.backend(backend_id)
                self.probes[backend_id] = backend.probe(sorted(need))
                self.run['platforms'][backend_id] = backend.label()
            except Exception as error:  # noqa: BLE001 - recorded per job
                self.probes[backend_id] = {'error': str(error)}
            self.run['toolchains'][backend_id] = self.probes[backend_id]
        self.save()

    def fail_all(self, state, reason):
        for job in self.run['jobs']:
            if job['state'] == 'pending':
                job['state'], job['reason'] = state, reason
        self.run['reason'] = reason

    # ---- jobs ------------------------------------------------------------------------------
    def end_job(self, jr, state, reason=None, fix=None):
        jr.update(state=state, reason=reason, finished_at=store.iso(), stalled=False)
        if fix:
            jr['fix'] = fix
        if jr.get('started_at'):
            jr['elapsed_s'] = int(time.time() - store.parse_iso(jr['started_at']))
        self.run['stalled'] = False
        self.beat.update(job=None, step=None, child=None)
        self.save()
        self.report()

    def check_job(self, job, jr, needs):
        """(state, reason, fix) if the job cannot run, else None."""
        if job['skip']:
            return 'skipped', 'skipped_by_config', None
        if job['unsupported']:
            return 'unsupported', job['unsupported'][0], 'Change the workflow or add an override in easyci.yml.'
        # As on GitHub: success() needs every dependency to have succeeded, failure() needs one to have
        # failed. A skipped or cancelled dependency makes both false, so an `if: failure()` rollback
        # does not run just because an optional upstream job was disabled.
        results = [n['result'] for n in needs.values()]
        status = ('success' if all(r == 'success' for r in results)
                  else 'failure' if any(r == 'failure' for r in results) else 'skipped')
        if job['when'] != 'always':
            ctx = {'github': self.plan['github'], 'needs': needs, 'status': status, 'env': job['env'],
                   'matrix': job.get('matrix') or {}, 'strategy': job.get('strategy') or {},
                   'runner': {'os': 'Windows' if job['backend'] == 'native' else 'Linux'}}
            if not expr.condition(job['if'], ctx):
                return 'skipped', 'condition_false' if job['if'] else 'dependency_not_passed', None
        probe = self.probes.get(job['backend'], {})
        if 'error' in probe:
            return 'failed', f'backend_unavailable:{probe["error"][:120]}', 'Run easyci doctor.'
        missing = [n for n in job['required_env'] if n not in os.environ]
        if missing:
            return 'unsupported', f'missing_env:{missing[0]}', f'Set {missing[0]} before running easyci run.'
        for tool in job['toolchains']:
            if tool.get('if') not in (None, ''):
                continue  # a conditional setup step is checked when (and if) it actually runs
            problem = toolchain_problem(job, tool, probe)
            if problem:
                return ('unsupported', *problem)
        if self.plan.get('gpu') == 'required' and not probe.get('nvidia-smi'):
            return 'unsupported', 'gpu_missing', f'nvidia-smi was not found on {where}; install the GPU driver.'
        return None

    def run_job(self, job):
        jid, jr = job['id'], self.job_rec(job['id'])
        needs = {}
        for n in job['needs']:  # a matrix job's dependents see all its combinations as one result
            group = workflow.members(self.plan['jobs'], n)
            outputs = {}
            for member in group:
                outputs.update(self.outputs.get(member['id'], {}))
            needs[n] = {'result': workflow.group_result([RESULT.get(self.job_rec(m['id'])['state'], 'failure')
                                                         for m in group]), 'outputs': outputs}
        if job.get('strategy', {}).get('fail-fast') and any(
                self.job_rec(m['id'])['state'] in ('failed', 'timed_out', 'interrupted')
                for m in workflow.members(self.plan['jobs'], job.get('group', job['id'])) if m['id'] != job['id']):
            return self.end_job(jr, 'cancelled', 'fail_fast')
        if self.cancelled():
            return self.end_job(jr, 'cancelled', 'cancelled')
        verdict = self.check_job(job, jr, needs)
        if verdict:
            return self.end_job(jr, *verdict)
        backend = self.backend(job['backend'])
        job_dir = self.rd / 'jobs' / jid
        (job_dir / 'tmp').mkdir(parents=True, exist_ok=True)
        jr.update(state='running', started_at=store.iso(), steps_done=0, stalled=False)
        self.beat.update(job=jid, step=None)
        self.save()
        self.report()
        self.log_handle = open(job_dir / 'log.txt', 'ab')
        steps_record, outcome = [], None
        self.kill_reason = self.step_env = None
        self.pending_caches = []
        self.last_output = time.time()
        deadline = time.time() + float(job['timeout_minutes']) * 60
        ws = backend.workspace(self.rd, self.run_id, jid)
        try:
            self.marker(f'job {jid} on {job["backend"]} ({self.run.get("platforms", {}).get(job["backend"], "")})')
            self.service_env = {}
            self.job_services = {}
            if job.get('container') or job.get('gh_services'):
                self.marker('starting containers: ' + ', '.join(
                    ([f'job container {job["container"]["image"]}'] if job.get('container') else []) +
                    [f'{sid} ({spec["image"]})' for sid, spec in job.get('gh_services', {}).items()]))
                self.job_services = backend.start_job(self.run_id, jid, ws, job_dir, job.get('container'),
                                                      job.get('gh_services'))
            if job.get('services'):
                self.service_env = backend.start_services(job['services'], self.run_id, jid)
                # The generated password is masked everywhere, including inside DATABASE_URL.
                self.secrets.append(self.service_env.get('PGPASSWORD', '').encode())
                self.marker(f'services ready: {", ".join(job["services"])} '
                            f'(DATABASE_URL on port {self.service_env.get("PGPORT")})')
            if job['command']:
                self.marker('cloning the run commit into a clean workspace')
                backend.checkout(ws, self.rd / 'source.bundle', self.run['commit'], self.run['tree'], self.origin_refs())
            else:
                backend.mkdirs(ws)
            outcome = self.run_steps(job, jr, backend, ws, job_dir, needs, deadline, steps_record)
        except Exception as error:  # noqa: BLE001 - becomes a job failure with a reason
            self.log(f'{self.run_id}: job {jid} error {traceback.format_exc()}')
            self.marker(f'EasyCI error: {error}')
            outcome = outcome or ('failed', f'internal_error:{self.mask(error)[:120]}')
        finally:
            backend.end_job()
            if job.get('services'):
                try:
                    backend.stop_services(self.run_id, jid)
                    self.marker('services stopped')
                except Exception as error:  # noqa: BLE001 - recorded; cleanup also runs at the end of the run
                    self.marker(f'services stop error: {error}')
        if outcome and outcome[0] == 'passed':
            for store_, cache_ws, paths, key in self.pending_caches:  # actions/cache saves after a passing job
                try:
                    saved = store_.save(cache_ws, paths, key)
                    self.marker(f'cache {"saved" if saved else "not saved (key exists or nothing to store)"}: {key}')
                except Exception as error:  # noqa: BLE001 - a cache failure never fails the job
                    self.marker(f'cache save failed: {error}')
        if self.kill_reason and job.get('on_kill') and self.step_env is not None:
            self.run_on_kill(job, jr, backend, ws, job_dir)
        try:
            if job['evidence']:
                copied = backend.collect(ws, job['evidence'], job_dir / 'artifacts' / 'evidence', layout='workspace')
                self.marker(f'evidence: copied {len(copied)} file(s)')
            metrics = backend.metrics(ws) if backend.exists(ws) else None
            if metrics is not None:
                store.write_json(job_dir / 'metrics.json', metrics)
                jr['metrics'] = metrics
        except Exception as error:  # noqa: BLE001
            self.marker(f'evidence collection error: {error}')
        self.log_handle.close()
        state, reason = outcome
        jr['steps'] = [{k: s.get(k) for k in ('index', 'name', 'outcome', 'exit_code', 'elapsed_s')} for s in steps_record]
        self.end_job(jr, state, reason)
        store.write_json(job_dir / 'job.json', {'name': jid, 'steps': steps_record, **jr})

    def run_on_kill(self, job, jr, backend, ws, job_dir):
        """After EasyCI stopped a step (cancel, timeout, stall kill), run the job's on_kill command in the
        same workspace and environment, so a delegated runner can release what it set up outside
        EasyCI's process tree (for JuliaFlow: the WSL job's PostgreSQL cluster and swap file). It runs
        before evidence is collected and the workspace is removed, is not itself cancellable, has its
        own time limit, and never changes the job's state."""
        reason = self.kill_reason
        limit_min = float(job.get('on_kill_minutes') or 10)
        self.marker(f'running on_kill cleanup (job stopped: {reason})')
        env = {**self.step_env, 'EASYCI_KILL_REASON': reason}
        started, timed_out, code = time.time(), False, None
        try:
            proc = backend.start('bash', job['on_kill'], env, ws, job_dir, 'on_kill')
            thread = self.stream(proc)
            while proc.popen.poll() is None and time.time() < started + limit_min * 60:
                time.sleep(poll_s())
            if proc.popen.poll() is None:
                timed_out = True
                self.marker(f'on_kill exceeded {limit_min:g} minutes; stopping it')
                backend.kill(proc)
                try:
                    proc.popen.wait(timeout=30)
                except Exception:  # noqa: BLE001
                    pass
            thread.join(timeout=10)
            code = proc.popen.returncode
        except Exception as error:  # noqa: BLE001 - reported, never fatal
            self.marker(f'on_kill error: {error}')
        finally:
            backend.end_job()
        elapsed = round(time.time() - started, 1)
        self.scrub_scripts(job_dir)
        jr['on_kill'] = {'reason': reason, 'exit_code': code, 'timed_out': timed_out, 'elapsed_s': elapsed}
        exit_text = '' if code is None else f' (exit {code})'
        self.marker(f'on_kill {"passed" if code == 0 else "failed"}{exit_text} after {elapsed}s')
        self.save()

    def marker(self, text):
        self.log_handle.write(self.redact(f'##[easyci] {text}\n'.encode('utf-8')))
        self.log_handle.flush()
        self.last_output = time.time()

    def run_steps(self, job, jr, backend, ws, job_dir, needs, deadline, record):
        is_wsl = job['backend'].startswith('wsl')
        join = posixpath.join if is_wsl else os.path.join
        gh = self.plan['github']
        runner_os = 'Linux' if is_wsl else 'Windows'
        tmp = job_dir / 'tmp'
        base_env = {'CI': 'true', 'EASYCI': '1', 'EASYCI_RUN_ID': self.run_id, 'EASYCI_JOB': job['id'], 'EASYCI_SHA': self.run['commit'],
                    'EASYCI_BASE': self.run['base'], 'EASYCI_EVENT': self.run['event'],
                    'EASYCI_PROFILE': self.run.get('profile') or '', 'GITHUB_SHA': gh['sha'], 'GITHUB_REF': gh['ref'],
                    'GITHUB_REF_NAME': gh['ref_name'], 'GITHUB_REPOSITORY': gh['repository'],
                    'GITHUB_WORKSPACE': ws, 'GITHUB_EVENT_NAME': gh['event_name'], 'RUNNER_OS': runner_os,
                    'RUNNER_TEMP': backend.to_backend_path(tmp)}
        for name in job['required_env']:
            base_env[name] = os.environ[name]
            if SECRET_NAME.search(name) and os.environ[name]:
                self.secrets.append(os.environ[name].encode())
        # required_env values are the lowest layer of the `env` context, so `if:`, names and `with:`
        # see what run-script interpolation sees; workflow/job env and exported values win.
        passed = {name: base_env[name] for name in job['required_env']}
        passed.update(getattr(self, 'service_env', {}) or {})  # DATABASE_URL, PG* from services
        base_env.update(getattr(self, 'service_env', {}) or {})
        ctx = {'github': gh, 'needs': needs, 'runner': {'os': runner_os, 'arch': 'X64', 'temp': base_env['RUNNER_TEMP']},
               'steps': {}, 'env': dict(passed), 'matrix': job.get('matrix') or {}, 'strategy': job.get('strategy') or {},
               'job': {'status': 'success', 'services': getattr(self, 'job_services', {}) or {}}, '__hash_files__': lambda patterns: backend.hash_files(ws, patterns)}
        job_env = dict(passed)
        for layer in job.get('env_layers') or [job['env']]:  # workflow env first, then job env
            job_env.update(expr.interpolate(layer, {**ctx, 'env': dict(job_env)}))
        ctx['env'] = dict(job_env)
        extra_env, extra_path, status, total = {}, [], 'success', len(job['steps'])
        jr['steps_total'] = total
        failed = None
        for step in job['steps']:
            index = step['index']
            rec = {'index': index, 'name': step['name'], 'kind': step['kind'], 'started_at': store.iso()}
            record.append(rec)
            ctx.update(status=status, env={**job_env, **extra_env},
                       job={'status': status, 'services': getattr(self, 'job_services', {}) or {}})  # job_env holds `passed`
            if self.cancelled():
                rec['outcome'] = 'cancelled'
                return 'cancelled', 'cancelled'
            # The step's own `env:` is visible to its `if:` (requested by the maintainer on PR #1).
            step_env = expr.interpolate(step.get('env') or {}, ctx)
            cond_ctx = {**ctx, 'env': {**ctx['env'], **step_env}} if step_env else ctx
            if step['kind'] == 'skipped' or not expr.condition(step.get('if'), cond_ctx):
                rec['outcome'] = 'skipped'
                jr['steps_done'] = index
                continue
            name = self.redact(expr.to_str(expr.interpolate(step['name'], ctx)).encode('utf-8')).decode(
                'utf-8', errors='replace')
            rec['name'] = name
            self.beat.update(step=index)
            jr.update(current_step=index, current_step_name=name)
            self.save()
            self.marker(f'step {index}/{total}: {name}')
            started = time.time()
            ok, result = self.run_step(step, ctx, backend, ws, job, job_dir, join, deadline,
                                       {**base_env, **job_env, **extra_env,
                                        **({'EASYCI_PATH_PREPEND': PATH_SEP[is_wsl].join(extra_path)} if extra_path else {})},
                                       tmp, index, jr)
            rec.update(outcome='success' if ok else result.get('outcome', 'failure'), exit_code=result.get('exit_code'),
                       finished_at=store.iso(), elapsed_s=round(time.time() - started, 1))
            code = '' if rec['exit_code'] is None else f' (exit {rec["exit_code"]})'
            self.marker(f'step {index} {rec["outcome"]}{code} after {rec["elapsed_s"]}s')
            ctx['steps'][step.get('id') or f'__{index}'] = {'outputs': result.get('outputs', {}),
                                                             'outcome': rec['outcome']}
            extra_env.update(result.get('env', {}))
            extra_path[:0] = list(reversed(result.get('path', [])))  # later additions come first, as on GitHub
            jr['steps_done'] = index
            if result.get('outcome') in ('timed_out', 'cancelled', 'stalled'):
                jr['failed_step'] = {'index': index, 'name': name}
                return {'timed_out': ('timed_out', 'timeout'), 'cancelled': ('cancelled', 'cancelled'),
                        'stalled': ('failed', 'stalled')}[result['outcome']]
            if not ok:
                if step.get('continue_on_error'):
                    rec['conclusion'] = 'success'
                    continue
                status = 'failure'
                failed = failed or {'index': index, 'name': name, 'exit_code': rec['exit_code']}
                jr['failed_step'] = failed
        ctx.update(status=status, env={**job_env, **extra_env})  # include the last step's exports
        self.outputs[job['id']] = {k: expr.to_str(expr.interpolate(v, ctx)) for k, v in job['outputs'].items()}
        jr['outputs'] = {k: self.redact(v.encode('utf-8')).decode('utf-8', errors='replace')
                         for k, v in self.outputs[job['id']].items()}
        return ('passed', None) if status == 'success' else ('failed', 'step_failed')

    def run_step(self, step, ctx, backend, ws, job, job_dir, join, deadline, env, tmp, index, jr):
        kind = step['kind']
        if kind == 'checkout':
            step_env = expr.interpolate(step.get('env') or {}, {**ctx, 'env': env})
            path_ctx = {**ctx, 'env': {**env, **step_env}}
            path = expr.to_str(expr.interpolate(step['path'], path_ctx)) if step.get('path') else ''
            if path and not inside_workspace(path):
                # checkout replaces its destination, so a path outside the workspace could delete
                # unrelated folders (even the developer's checkout). Refuse it.
                self.marker(f'checkout path {path!r} must be a relative path inside the workspace; refusing it')
                return False, {'exit_code': 1}
            target = join(ws, path) if path else ws
            backend.checkout(target, self.rd / 'source.bundle', self.run['commit'], self.run['tree'], self.origin_refs())
            self.marker(f'checked out {self.run["commit"]} (tree {self.run["tree"]})')
            return True, {}
        if kind == 'toolchain':
            probe = self.probes.get(job['backend'], {})
            entry = next((t for t in job['toolchains'] if t.get('step') == step['index']), None)
            problem = toolchain_problem(job, entry, probe) if entry and entry.get('if') not in (None, '') else None
            if problem:  # a conditional setup step that is now enabled: enforce it here
                self.marker(f'{problem[0]}. {problem[1]}')
                return False, {'exit_code': 1}
            actual = probe.get(step['tool'])
            self.marker(f'{step["tool"]} {actual} verified (requested {step["requested"] or "any"})')
            return True, {}
        if kind == 'cache':
            return self.cache_step(step, ctx, backend, ws, env)
        if kind == 'download-artifact':
            return self.download_step(step, ctx, backend, ws, env)
        if kind == 'upload-artifact':
            # Inputs see the job env plus this step's own `env:`, as run scripts do.
            step_env = expr.interpolate(step.get('env') or {}, {**ctx, 'env': env})
            with_ = expr.interpolate(step['with'], {**ctx, 'env': {**ctx['env'], **step_env}})
            name = re.sub(r'[^A-Za-z0-9._-]', '_', with_.get('name') or 'artifact')
            paths = [p for p in str(with_.get('path', '')).splitlines() if p.strip()]
            copied = backend.collect(ws, paths, job_dir / 'artifacts' / name)
            self.marker(f'artifact {name}: {len(copied)} file(s)')
            if copied:
                self.offload_artifact(job_dir, name)
            if not copied and with_.get('if-no-files-found', 'warn') == 'error':
                self.marker(f'no files found for {paths} (if-no-files-found: error)')
                return False, {'exit_code': 1}
            return True, {}
        files = {n: tmp / f'{n.lower()}_{index}' for n in ('GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_STEP_SUMMARY',
                                                           'GITHUB_PATH')}
        for path in files.values():
            path.write_bytes(b'')
        env = {**env, **expr.interpolate(step['env'], {**ctx, 'env': env}),
               **{n: backend.to_backend_path(p) for n, p in files.items()}}
        script = expr.interpolate(step['run'], {**ctx, 'env': env})
        workdir = expr.to_str(expr.interpolate(step['working_directory'], {**ctx, 'env': env})) \
            if step.get('working_directory') else ''
        cwd = join(ws, workdir) if workdir else ws
        step_deadline = time.time() + float(step['timeout_minutes']) * 60 if step.get('timeout_minutes') else None
        self.step_env = env
        proc = backend.start(step['shell'], script, env, cwd, job_dir, index)
        self.beat['child'] = proc.info
        outcome = self.watch(proc, backend, job, jr, deadline, step_deadline)
        self.beat['child'] = None
        self.scrub_scripts(job_dir)
        # Values handed to later steps must be the real ones; only persisted copies are masked.
        result = {'exit_code': proc.popen.returncode, 'outputs': parse_kv(self.read(files['GITHUB_OUTPUT'], raw=True)),
                  'env': parse_kv(self.read(files['GITHUB_ENV'], raw=True)),
                  'path': [line.strip() for line in self.read(files['GITHUB_PATH'], raw=True).splitlines() if line.strip()]}
        summary = self.read(files['GITHUB_STEP_SUMMARY'])
        if summary:
            with open(job_dir / 'summary.md', 'a', encoding='utf-8') as handle:
                handle.write(summary + '\n')
        for path in files.values():
            path.unlink(missing_ok=True)
        if outcome:
            result['outcome'] = outcome
            return False, result
        return proc.popen.returncode == 0, result

    def offload_artifact(self, job_dir, name):
        """Move an artifact over artifact_threshold_mb out of the run folder into the artifact store
        (%EASYCI_HOME%/artifacts/<project>/<run>/<job>/<name>, DVC-friendly), leaving a pointer with
        every file's sha256. The pointer is sealed with the run; `easyci verify` checks the files."""
        folder = job_dir / 'artifacts' / name
        files = [p for p in folder.rglob('*') if p.is_file()]
        size = sum(p.stat().st_size for p in files)
        if size <= float(self.plan.get('artifact_threshold_mb', 100)) * 2 ** 20:
            return
        target = store.artifact_store(self.run['project']) / self.run_id / job_dir.name / name
        target.parent.mkdir(parents=True, exist_ok=True)
        hashes = {p.relative_to(folder).as_posix(): store.digest(p) for p in files}
        shutil.move(str(folder), str(target))
        store.write_json(job_dir / 'artifacts' / f'{name}.pointer.json',
                         {'schema': 'easyci/1', 'kind': 'artifact-pointer', 'name': name, 'location': str(target),
                          'size_bytes': size, 'files': hashes})
        self.marker(f'artifact {name} is {size / 2 ** 20:.0f} MB; stored outside the run folder: {target}')

    def origin_refs(self):
        """refs/remotes/origin/<default branch> at the base commit (which the bundle always holds)."""
        default = ((self.plan.get('github') or {}).get('event') or {}).get('repository', {}).get('default_branch')
        base = self.run.get('base')
        if default and base and re.fullmatch(r'[A-Za-z0-9._/-]+', default):
            return {f'refs/remotes/origin/{default}': base}
        return {}

    def step_inputs(self, step, ctx, env):
        step_env = expr.interpolate(step.get('env') or {}, {**ctx, 'env': env})
        return expr.interpolate(step.get('with') or {}, {**ctx, 'env': {**ctx['env'], **step_env}})

    def cache_step(self, step, ctx, backend, ws, env):
        from easyci import cache as cache_mod
        with_ = self.step_inputs(step, ctx, env)
        key, paths = with_.get('key', '').strip(), cache_mod.split_lines(with_.get('path'))
        if not key:
            self.marker('cache key is empty after evaluation; skipping the cache')
            return True, {'outputs': {'cache-hit': 'false'}}
        store_ = backend.cache(self.run['project'])
        if step['variant'] == 'save':
            saved = store_.save(ws, paths, key)
            self.marker(f'cache {"saved" if saved else "not saved (key exists or nothing to store)"}: {key}')
            return True, {}
        matched, exact = store_.restore(ws, paths, key, cache_mod.split_lines(with_.get('restore-keys')))
        self.marker(f'cache {"hit" if exact else "restored from " + matched if matched else "miss"}: {key}')
        if step['variant'] == 'cache' and not exact:
            self.pending_caches.append((store_, ws, paths, key))
        if not matched and str(with_.get('fail-on-cache-miss', 'false')).lower() == 'true':
            return False, {'exit_code': 1, 'outputs': {'cache-hit': 'false'}}
        return True, {'outputs': {'cache-hit': 'true' if exact else 'false', 'cache-primary-key': key,
                                  'cache-matched-key': matched or ''}}

    def download_step(self, step, ctx, backend, ws, env):
        with_ = self.step_inputs(step, ctx, env)
        name, path = with_.get('name', '').strip(), with_.get('path', '').strip()
        if path and not inside_workspace(path):
            self.marker(f'download path {path!r} must be a relative path inside the workspace; refusing it')
            return False, {'exit_code': 1}
        roots = sorted(p for p in (self.rd / 'jobs').glob('*/artifacts/*') if p.is_dir() and p.name != 'evidence')
        for pointer in sorted((self.rd / 'jobs').glob('*/artifacts/*.pointer.json')):  # offloaded big artifacts
            location = pathlib.Path((store.read_json(pointer) or {}).get('location', ''))
            if location.is_dir():
                roots.append(location)
        chosen = [p for p in roots if p.name == name] if name else roots
        if name and not chosen:
            self.marker(f'artifact {name} was not uploaded earlier in this run')
            return False, {'exit_code': 1}
        total = 0
        for artifact in chosen:  # without a name every artifact lands in its own folder, as on GitHub
            total += backend.copy_in(artifact, ws, path if name else posixpath.join(path, artifact.name) if path
                                     else artifact.name)
        self.marker(f'downloaded {len(chosen)} artifact(s), {total} file(s)')
        return True, {'outputs': {'download-path': path or '.'}}

    def read(self, path, raw=False):
        try:
            data = pathlib.Path(path).read_bytes()
            return (data if raw else self.redact(data)).decode('utf-8', errors='replace')
        except OSError:
            return ''

    def scrub_scripts(self, job_dir):
        """Step scripts may contain secret values interpolated from `${{ env.X }}`; the running process
        needs them, the evidence must not keep them. Mask them once the process has ended."""
        for path in (pathlib.Path(job_dir) / 'steps').glob('*'):
            try:
                data = path.read_bytes()
                masked = self.redact(data)
                if masked != data:
                    path.write_bytes(masked)
            except OSError as error:
                self.log(f'{self.run_id}: could not mask {path.name}: {error}')

    def stream(self, proc):
        """Copy a process's output into the job log (redacted) on a daemon thread; return the thread."""
        handle = self.log_handle

        def write(line):
            handle.write(self.redact(line.rstrip(b'\r')) + b'\n')
            handle.flush()

        def reader():
            # Read whatever has arrived (not whole lines), so progress output without newlines
            # (dots, \r progress bars) still counts as activity for stall detection. Only complete
            # lines are redacted and written; a very long unterminated line is flushed in pieces.
            pending = b''
            try:
                while True:
                    chunk = proc.popen.stdout.read1(65536)
                    if not chunk:
                        break
                    self.last_output = time.time()
                    pending += chunk
                    *lines, pending = pending.split(b'\n')
                    for line in lines:
                        write(line)
                    if len(pending) > 1 << 20:
                        write(pending)
                        pending = b''
            except (OSError, ValueError):
                pass  # the job ended while a background child still held the pipe
            if pending:
                write(pending)
        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        return thread

    def watch(self, proc, backend, job, jr, deadline, step_deadline):
        """Stream output; enforce cancel, job timeout, step timeout and stall. Return a kill reason or None."""
        thread = self.stream(proc)
        stall_s = float(job['stall_minutes']) * 60
        reason = None
        while proc.popen.poll() is None or thread.is_alive():
            now = time.time()
            if proc.popen.poll() is None:
                if self.cancelled():
                    reason = 'cancelled'
                elif now > deadline:
                    reason = 'timed_out'
                elif step_deadline and now > step_deadline:
                    reason = 'step_timeout'
                silent = now - self.last_output
                if silent > stall_s and not jr.get('stalled'):
                    jr['stalled'] = self.run['stalled'] = True
                    jr['stalled_since'] = store.iso(self.last_output)
                    self.save()
                    self.log(f'{self.run_id}: job {job["id"]} stalled ({int(silent)} s silent)')
                    if job['stall_action'] == 'kill':
                        reason = 'stalled'
                elif silent < stall_s and jr.get('stalled'):
                    jr['stalled'] = self.run['stalled'] = False
                    self.save()
                if reason:
                    self.marker({'cancelled': 'cancelled on request', 'timed_out':
                                 f'job exceeded timeout of {job["timeout_minutes"]} minutes; stopping it',
                                 'step_timeout': 'step exceeded its timeout; stopping it',
                                 'stalled': f'no output for {job["stall_minutes"]} minutes (stall_action: kill)'}[reason])
                    self.kill_reason = reason
                    backend.kill(proc)
                    try:
                        proc.popen.wait(timeout=30)
                    except Exception:  # noqa: BLE001
                        pass
                    thread.join(timeout=10)
                    break
            else:
                thread.join(timeout=5)  # a background child may keep the pipe open; do not wait on it
                break
            if time.time() - self.last_save > 5:
                jr['last_output_at'] = store.iso(self.last_output)
                self.save()
            time.sleep(poll_s())
        thread.join(timeout=1)
        jr['last_output_at'] = store.iso(self.last_output)
        return 'failure' if reason == 'step_timeout' else reason

    # ---- the end ---------------------------------------------------------------------------
    def finalize(self, crashed=None):
        for job in self.run['jobs']:
            if job['state'] in ('pending', 'running'):
                job['state'], job['reason'] = ('cancelled', 'cancelled') if self.cancel_requested else ('failed', 'not_run')
        states = [j['state'] for j in self.run['jobs']]
        if crashed:
            state, self.run['reason'] = 'failed', f'internal_error:{crashed}'
        elif self.cancel_requested:
            state = 'cancelled'
            self.run['reason'] = self.run.get('reason') or 'cancelled'
        elif 'timed_out' in states:
            state = 'timed_out'
        elif 'failed' in states or 'interrupted' in states:
            state = 'failed'
        elif 'unsupported' in states:
            state = 'unsupported'
        else:
            state = 'passed'
        self.run.update(state=state, finished_at=store.iso(), stalled=False)
        self.beat.update(phase='finished', job=None, step=None, child=None)
        self.save()
        self.report()
        if self.plan.get('pr_comment'):
            try:
                github.post_comment(self.rd, self.run)
            except Exception as error:  # noqa: BLE001 - a comment never fails a run
                self.log(f'{self.run_id}: PR comment error {error}')
        store.write_report(self.rd, self.run)
        if not self.request['options'].get('keep'):
            for backend in self.backends.values():
                try:
                    backend.cleanup(self.rd, self.run_id)
                except Exception as error:  # noqa: BLE001
                    self.log(f'{self.run_id}: cleanup failed {error}')
        if self.locked:
            store.release_lock(self.run_id)
        store.remove_ticket(self.run_id)
        keep_awake(False)
        self.stop.set()
        if getattr(self, 'hb_thread', None):
            self.hb_thread.join(timeout=10)  # no heartbeat write may land after the seal
        self.write_beat()
        if self.run.get('launch') == 'schtasks':
            launch.delete_task(self.run_id)
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.flush()
            except (AttributeError, OSError, ValueError):
                pass
        store.seal(self.rd)
        self.log(f'{self.run_id}: finished {state}')


def main(argv):
    rd = pathlib.Path(argv[0]).resolve()
    if sys.stdout is None or sys.stderr is None:  # pythonw (schtasks path): no standard streams
        sys.stdout = open(rd / 'supervisor.out', 'a', encoding='utf-8')
        sys.stderr = open(rd / 'supervisor.err', 'a', encoding='utf-8')
    ignore_console_signals()
    sup = Supervisor(rd)
    try:
        sup.execute()
    except Exception as error:  # noqa: BLE001 - last resort: never leave the run live
        sup.log(f'{sup.run_id}: supervisor crash {traceback.format_exc()}')
        try:
            sup.finalize(crashed=sup.mask(error)[:200])
        except Exception:  # noqa: BLE001
            sup.log(f'{sup.run_id}: finalize after crash failed {traceback.format_exc()}')
            if sup.locked:
                store.release_lock(sup.run_id)
    return 0
