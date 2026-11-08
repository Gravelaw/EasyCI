"""The detached per-run supervisor: queue, snapshot, jobs, heartbeat, stall/timeout/cancel, seal.

It is the only process that writes run.json while the run is live (the reaper writes it only
after this process is gone).
"""
import os
import pathlib
import posixpath
import re
import signal
import sys
import threading
import time
import traceback

from easyci import backends, expr, github, launch, snapshot, store

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
                store.log(f'{self.run_id}: heartbeat write failed: {error}')

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
            store.log(f'{self.run_id}: reporting error {error}')
        self.save()

    def redact(self, data):
        for value in self.secrets:
            if value:
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

    def prepare(self):
        repo, commit, base = self.request['repo'], self.run['commit'], self.run['base']
        self.beat['step'] = 'snapshot'
        try:
            self.run['tree'] = snapshot.make_bundle(repo, commit, base, self.rd / 'source.bundle')
            self.run['bundle_sha256'] = store.digest(self.rd / 'source.bundle')
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
        status = 'success' if all(n['result'] == 'success' for n in needs.values()) else 'failure'
        if job['when'] != 'always':
            ctx = {'github': self.plan['github'], 'needs': needs, 'status': status, 'env': job['env'],
                   'runner': {'os': 'Windows' if job['backend'] == 'native' else 'Linux'}}
            if not expr.condition(job['if'], ctx):
                return 'skipped', 'condition_false' if job['if'] else 'dependency_not_passed', None
        probe = self.probes.get(job['backend'], {})
        if 'error' in probe:
            return 'failed', f'backend_unavailable:{probe["error"][:120]}', 'Run easyci doctor.'
        missing = [n for n in job['required_env'] if n not in os.environ]
        if missing:
            return 'unsupported', f'missing_env:{missing[0]}', f'Set {missing[0]} before running easyci run.'
        where = job['backend'].replace('wsl:', 'WSL distro ')
        for tool in job['toolchains']:
            actual = probe.get(tool['tool'])
            if not actual:
                return ('unsupported', f'toolchain_missing:{tool["tool"]}',
                        f'Install {tool["tool"]} on {where}, or set toolchains: {{{tool["tool"]}: ignore}} for job {job["id"]} in easyci.yml.')
            if tool['requested'] and not backends.version_matches(tool['requested'], actual):
                have = re.search(r'\d+(?:\.\d+)*', actual).group(0)
                return ('unsupported', f'toolchain_mismatch:{tool["tool"]} {have} != {tool["requested"]}',
                        f'{where} has {tool["tool"]} {have} but the workflow asks for {tool["requested"]}. Install '
                        f'{tool["tool"]} {tool["requested"]} there, pick another backend, or set toolchains: '
                        f'{{{tool["tool"]}: ignore}} for job {job["id"]} in easyci.yml.')
        if self.plan.get('gpu') == 'required' and not probe.get('nvidia-smi'):
            return 'unsupported', 'gpu_missing', f'nvidia-smi was not found on {where}; install the GPU driver.'
        return None

    def run_job(self, job):
        jid, jr = job['id'], self.job_rec(job['id'])
        needs = {n: {'result': RESULT.get(self.job_rec(n)['state'], 'failure'), 'outputs': self.outputs.get(n, {})}
                 for n in job['needs']}
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
        self.last_output = time.time()
        deadline = time.time() + float(job['timeout_minutes']) * 60
        ws = backend.workspace(self.rd, self.run_id, jid)
        try:
            self.marker(f'job {jid} on {job["backend"]} ({self.run.get("platforms", {}).get(job["backend"], "")})')
            if job['command']:
                self.marker('cloning the run commit into a clean workspace')
                backend.checkout(ws, self.rd / 'source.bundle', self.run['commit'], self.run['tree'])
            else:
                backend.mkdirs(ws)
            outcome = self.run_steps(job, jr, backend, ws, job_dir, needs, deadline, steps_record)
        except Exception as error:  # noqa: BLE001 - becomes a job failure with a reason
            store.log(f'{self.run_id}: job {jid} error {traceback.format_exc()}')
            self.marker(f'EasyCI error: {error}')
            outcome = outcome or ('failed', f'internal_error:{str(error)[:120]}')
        finally:
            backend.end_job()
        if self.kill_reason and job.get('on_kill') and self.step_env is not None:
            self.run_on_kill(job, jr, backend, ws, job_dir)
        try:
            if job['evidence']:
                copied = backend.collect(ws, job['evidence'], job_dir / 'artifacts' / 'evidence')
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
        self.log_handle.write(f'##[easyci] {text}\n'.encode())
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
        ctx = {'github': gh, 'needs': needs, 'runner': {'os': runner_os}, 'steps': {}, 'env': dict(job['env'])}
        job_env = expr.interpolate(job['env'], ctx)
        extra_env, status, total = {}, 'success', len(job['steps'])
        jr['steps_total'] = total
        failed = None
        for step in job['steps']:
            index = step['index']
            rec = {'index': index, 'name': step['name'], 'kind': step['kind'], 'started_at': store.iso()}
            record.append(rec)
            ctx.update(status=status, env={**job_env, **extra_env})
            if self.cancelled():
                rec['outcome'] = 'cancelled'
                return 'cancelled', 'cancelled'
            if step['kind'] == 'skipped' or not expr.condition(step.get('if'), ctx):
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
                                       {**base_env, **job_env, **extra_env}, tmp, index, jr)
            rec.update(outcome='success' if ok else result.get('outcome', 'failure'), exit_code=result.get('exit_code'),
                       finished_at=store.iso(), elapsed_s=round(time.time() - started, 1))
            code = '' if rec['exit_code'] is None else f' (exit {rec["exit_code"]})'
            self.marker(f'step {index} {rec["outcome"]}{code} after {rec["elapsed_s"]}s')
            ctx['steps'][step.get('id') or f'__{index}'] = {'outputs': result.get('outputs', {}),
                                                             'outcome': rec['outcome']}
            extra_env.update(result.get('env', {}))
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
        ctx.update(status=status)
        self.outputs[job['id']] = {k: expr.to_str(expr.interpolate(v, ctx)) for k, v in job['outputs'].items()}
        jr['outputs'] = {k: self.redact(v.encode('utf-8')).decode('utf-8', errors='replace')
                         for k, v in self.outputs[job['id']].items()}
        return ('passed', None) if status == 'success' else ('failed', 'step_failed')

    def run_step(self, step, ctx, backend, ws, job, job_dir, join, deadline, env, tmp, index, jr):
        kind = step['kind']
        if kind == 'checkout':
            target = join(ws, step['path']) if step.get('path') else ws
            backend.checkout(target, self.rd / 'source.bundle', self.run['commit'], self.run['tree'])
            self.marker(f'checked out {self.run["commit"]} (tree {self.run["tree"]})')
            return True, {}
        if kind == 'toolchain':
            actual = self.probes.get(job['backend'], {}).get(step['tool'])
            self.marker(f'{step["tool"]} {actual} verified (requested {step["requested"] or "any"})')
            return True, {}
        if kind == 'cache':
            self.marker('cache step skipped (local)')
            return True, {}
        if kind == 'upload-artifact':
            with_ = expr.interpolate(step['with'], ctx)
            name = re.sub(r'[^A-Za-z0-9._-]', '_', with_.get('name') or 'artifact')
            paths = [p for p in str(with_.get('path', '')).splitlines() if p.strip()]
            copied = backend.collect(ws, paths, job_dir / 'artifacts' / name)
            self.marker(f'artifact {name}: {len(copied)} file(s)')
            if not copied and with_.get('if-no-files-found', 'warn') == 'error':
                self.marker(f'no files found for {paths} (if-no-files-found: error)')
                return False, {'exit_code': 1}
            return True, {}
        files = {n: tmp / f'{n.lower()}_{index}' for n in ('GITHUB_OUTPUT', 'GITHUB_ENV', 'GITHUB_STEP_SUMMARY')}
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
                  'env': parse_kv(self.read(files['GITHUB_ENV'], raw=True))}
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
                store.log(f'{self.run_id}: could not mask {path.name}: {error}')

    def stream(self, proc):
        """Copy a process's output into the job log (redacted) on a daemon thread; return the thread."""
        handle = self.log_handle

        def reader():
            try:
                for raw in iter(proc.popen.stdout.readline, b''):
                    handle.write(self.redact(raw.rstrip(b'\r\n')) + b'\n')
                    handle.flush()
                    self.last_output = time.time()
            except (OSError, ValueError):
                pass  # the job ended while a background child still held the pipe
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
                    store.log(f'{self.run_id}: job {job["id"]} stalled ({int(silent)} s silent)')
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
        store.write_report(self.rd, self.run)
        if not self.request['options'].get('keep'):
            for backend in self.backends.values():
                try:
                    backend.cleanup(self.rd, self.run_id)
                except Exception as error:  # noqa: BLE001
                    store.log(f'{self.run_id}: cleanup failed {error}')
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
        store.log(f'{self.run_id}: finished {state}')


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
        store.log(f'{sup.run_id}: supervisor crash {traceback.format_exc()}')
        try:
            sup.finalize(crashed=str(error)[:200])
        except Exception:  # noqa: BLE001
            store.log(f'{sup.run_id}: finalize after crash failed {traceback.format_exc()}')
            if sup.locked:
                store.release_lock(sup.run_id)
    return 0
