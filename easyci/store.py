"""On-disk layout under EASYCI_HOME: run ids, atomic JSON, queue lock, reaper, sealing."""
import datetime as dt
import hashlib
import json
import math
import os
import pathlib
import re
import secrets
import shutil
import socket
import stat
import sys
import time

from easyci import SCHEMA, VERSION

LIVE = ('queued', 'preparing', 'running')
TERMINAL = ('passed', 'failed', 'unsupported', 'timed_out', 'cancelled', 'interrupted')


def home():
    return pathlib.Path(os.environ.get('EASYCI_HOME') or pathlib.Path.home() / 'EasyCI').absolute()


def setting(name, default):
    """Numeric tuning knob (tests shorten intervals); production uses the spec defaults."""
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def heartbeat_s():
    return setting('EASYCI_HEARTBEAT_S', 10)


def stale_s():
    return setting('EASYCI_STALE_S', 60)


def iso(t=None):
    return dt.datetime.fromtimestamp(time.time() if t is None else t, dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_iso(text):
    if not text:
        return None
    return dt.datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=dt.timezone.utc).timestamp()


def write_json(path, data):
    path = pathlib.Path(path)
    tmp = path.with_name(f'.{path.name}.{os.getpid()}.{secrets.token_hex(3)}.tmp')
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    for _ in range(100):  # Windows refuses replace while a reader holds the file open
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.02)
    os.replace(tmp, path)


def read_json(path, default=None):
    for _ in range(50):
        try:
            return json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
        except FileNotFoundError:
            return default
        except (PermissionError, json.JSONDecodeError):
            time.sleep(0.02)
    return default


def digest(path):
    with open(path, 'rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def log(message):
    try:
        path = home() / 'easyci.log'
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > 10 * 2 ** 20:
            os.replace(path, path.with_name('easyci.log.1'))
        with path.open('a', encoding='utf-8') as handle:
            handle.write(f'{iso()} [{os.getpid()}] {message}\n')
    except OSError:
        pass


def pid_alive(pid, image=None):
    """True if the process exists (and, when image is given, its executable name contains it)."""
    if not pid:
        return False
    if os.name != 'nt':
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.restype = wintypes.HANDLE
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    handle = k.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() == 5  # access denied: exists, owned by someone else
    try:
        code = wintypes.DWORD()
        k.GetExitCodeProcess(wintypes.HANDLE(handle), ctypes.byref(code))
        if code.value != 259:  # STILL_ACTIVE
            return False
        if image:
            buf, size = ctypes.create_unicode_buffer(1024), wintypes.DWORD(1024)
            if k.QueryFullProcessImageNameW(wintypes.HANDLE(handle), 0, buf, ctypes.byref(size)):
                return image in os.path.basename(buf.value).lower()
        return True
    finally:
        k.CloseHandle(wintypes.HANDLE(handle))


def make_writable_and_remove(path):
    """rmtree that clears read-only bits (git pack files on Windows). Works on Python 3.11, which
    only has `onerror`; 3.12 added `onexc` and deprecated `onerror`."""
    def retry(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)
    if pathlib.Path(path).exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=retry)


# ---- projects and runs -------------------------------------------------------------------

def project_slug(repo):
    path = str(pathlib.Path(repo).resolve())
    key = path.lower() if os.name == 'nt' else path
    name = re.sub(r'[^A-Za-z0-9._-]+', '-', pathlib.Path(path).name).strip('-') or 'repo'
    return f'{name}-{hashlib.sha256(key.encode()).hexdigest()[:6]}'


def project_dir(slug):
    return home() / 'projects' / slug


def new_run_id():
    return time.strftime('%Y%m%d-%H%M%S') + '-' + secrets.token_hex(2)


def run_dirs(slug=None):
    base = home() / 'projects'
    pattern = f'{slug}/runs/*' if slug else '*/runs/*'
    dirs = [d for d in base.glob(pattern) if (d / 'run.json').exists()] if base.exists() else []
    return sorted(dirs, key=lambda d: d.name)


def find_run(run_id):
    """Run directory for a full id or a unique prefix; None if absent or ambiguous."""
    if not run_id or any(c in run_id for c in '/\\*?[]:'):
        return None
    matches = [d for d in run_dirs() if d.name == run_id] or [d for d in run_dirs() if d.name.startswith(run_id)]
    return matches[0] if len(matches) == 1 else None


# ---- queue -------------------------------------------------------------------------------

def lock_dir():
    return home() / 'queue.lock'


def ticket_dir():
    return home() / 'queue'


def add_ticket(run_id):
    ticket_dir().mkdir(parents=True, exist_ok=True)
    path = ticket_dir() / f'{time.time_ns():020d}-{run_id}'
    path.write_text(run_id, encoding='utf-8')
    return path


def tickets():
    folder = ticket_dir()
    return sorted(folder.iterdir()) if folder.exists() else []


def ticket_run(path):
    return path.name.split('-', 1)[1]


def remove_ticket(run_id):
    for path in tickets():
        if ticket_run(path) == run_id:
            path.unlink(missing_ok=True)


def try_lock(run_id, pid):
    try:
        lock_dir().mkdir(parents=True)
    except FileExistsError:
        return False
    write_json(lock_dir() / 'owner.json', {'run_id': run_id, 'pid': pid, 'taken_at': iso()})
    return True


def lock_owner():
    return read_json(lock_dir() / 'owner.json') if lock_dir().exists() else None


def release_lock(run_id):
    owner = lock_owner()
    if owner and owner.get('run_id') == run_id:
        (lock_dir() / 'owner.json').unlink(missing_ok=True)
        try:
            lock_dir().rmdir()
        except OSError:
            pass


def busy():
    """Run id currently holding or waiting for the machine, or None."""
    owner = lock_owner()
    if owner:
        return owner.get('run_id') or 'unknown'
    if lock_dir().exists():
        return 'unknown'
    for path in tickets():
        return ticket_run(path)
    return None


# ---- reaper ------------------------------------------------------------------------------

def is_orphaned(rd, run):
    """A live run, or a finished run that was never sealed, whose supervisor is gone."""
    unsealed = run.get('state') in TERMINAL and not (pathlib.Path(rd) / 'sha256.json').exists()
    if run.get('state') not in LIVE and not unsealed:
        return False
    beat = read_json(rd / 'heartbeat.json') or {}
    pid = beat.get('pid') or run.get('supervisor_pid')
    last = parse_iso(beat.get('at')) or parse_iso(run.get('created_at')) or 0
    return time.time() - last > stale_s() and not pid_alive(pid, 'python')


def reap(exclude=None):
    """Mark live runs whose supervisor died as interrupted; release locks and tickets they held."""
    reaped = []
    for rd in run_dirs():
        if rd.name == exclude:
            continue
        run = read_json(rd / 'run.json') or {}
        if is_orphaned(rd, run) and mark_interrupted(rd):
            reaped.append(rd.name)
    owner = lock_owner()
    if owner:
        rd = find_run(owner.get('run_id', ''))
        state = (read_json(rd / 'run.json') or {}).get('state') if rd else None
        if state not in LIVE:
            release_lock(owner.get('run_id'))
    elif lock_dir().exists() and time.time() - lock_dir().stat().st_mtime > stale_s():
        try:
            lock_dir().rmdir()
        except OSError:
            pass
    for path in tickets():
        rd = find_run(ticket_run(path))
        if not rd or (read_json(rd / 'run.json') or {}).get('state') not in LIVE:
            path.unlink(missing_ok=True)
    return reaped


def mark_interrupted(rd):
    guard = rd / '.reaping'
    try:
        guard.mkdir()
    except FileExistsError:
        return False
    try:
        run = read_json(rd / 'run.json') or {}
        if not is_orphaned(rd, run):
            return False
        finished = run.get('state') in TERMINAL  # died while finishing: keep its verdict, finish the rest
        beat = read_json(rd / 'heartbeat.json') or {}
        from easyci import backends, github, launch
        if beat.get('child'):
            try:
                backends.kill_leftover(beat['child'])
            except Exception as error:  # noqa: BLE001 - best effort, recorded
                log(f'reaper could not kill leftover {beat["child"]}: {error}')
        try:  # containers and services outlive the supervisor; stop them before the machine is reused
            backends.remove_leftover_containers(run)
        except Exception as error:  # noqa: BLE001
            log(f'reaper could not remove containers of {rd.name}: {error}')
        if not finished:
            run['state'], run['finished_at'] = 'interrupted', iso()
            run['reason'] = 'supervisor_died'
            for job in run.get('jobs', []):
                if job.get('state') in ('pending', 'running'):
                    job['state'], job['reason'] = 'interrupted', 'supervisor_died'
        write_json(rd / 'run.json', run)
        github.sync(rd, run)
        write_json(rd / 'run.json', run)
        write_report(rd, run)
        release_lock(run.get('run_id'))
        remove_ticket(run.get('run_id'))
        if run.get('launch') == 'schtasks':
            launch.delete_task(run.get("run_id"))
        request = read_json(rd / 'request.json') or {}
        if not request.get('options', {}).get('keep'):
            make_writable_and_remove(rd / 'workspace')
            remove_backend_workspaces(rd, run)
        log(f'reaped {rd.name}: supervisor pid {beat.get("pid")} gone'
            f'{" while finishing; sealed it" if finished else ""}')
    finally:
        guard.rmdir()
    seal(rd)
    return True


# ---- report and seal ---------------------------------------------------------------------

def write_report(rd, run):
    request = read_json(rd / 'request.json') or {}
    plan = read_json(rd / 'plan.json') or {}
    started, finished = parse_iso(run.get('started_at')), parse_iso(run.get('finished_at'))
    report = {
        'schema': SCHEMA, 'run_id': run.get('run_id'), 'result': run.get('state'), 'reason': run.get('reason'),
        'duration_s': round(finished - started) if started and finished else None,
        'jobs': run.get('jobs', []), 'toolchains': run.get('toolchains', {}), 'platforms': run.get('platforms', {}),
        'github': run.get('github', {}), 'launch': run.get('launch'),
        'decisions': [{'job': j['id'], 'step': s['index'], 'kind': s['kind'], 'decision': s.get('decision')}
                      for j in plan.get('jobs', []) for s in j.get('steps', [])],
        'reproducibility': {**request.get('repro', {}), 'tree': run.get('tree'), 'easyci_version': VERSION,
                            'host': socket.gethostname(), 'data': run.get('data', {})},
    }
    write_json(rd / 'report.json', report)
    return report


def seal_paths(rd):
    for path in sorted(pathlib.Path(rd).rglob('*')):
        rel = path.relative_to(rd).as_posix()
        if path.is_file() and rel != 'sha256.json' and not rel.startswith('workspace/'):
            yield rel, path


def seal(rd):
    rd = pathlib.Path(rd)
    inventory = rd / 'sha256.json'
    if inventory.exists():
        os.chmod(inventory, stat.S_IWRITE)
    files = {}
    for rel, path in seal_paths(rd):
        files[rel] = digest(path)
    write_json(inventory, {'schema': SCHEMA, 'algorithm': 'sha256', 'sealed_at': iso(), 'files': files})
    for rel, path in seal_paths(rd):
        os.chmod(path, stat.S_IREAD)
    os.chmod(inventory, stat.S_IREAD)


def unseal_for_update(rd, names):
    for name in names:
        path = pathlib.Path(rd) / name
        if path.exists():
            os.chmod(path, stat.S_IWRITE)


def verify(rd):
    inventory = read_json(pathlib.Path(rd) / 'sha256.json')
    if not inventory:
        return None
    expected, mismatches = inventory.get('files', {}), []
    actual = dict(seal_paths(rd))
    for rel, value in sorted(expected.items()):
        if rel not in actual:
            mismatches.append({'path': rel, 'problem': 'missing'})
        elif digest(actual[rel]) != value:
            mismatches.append({'path': rel, 'problem': 'changed'})
    for rel in sorted(set(actual) - set(expected)):
        mismatches.append({'path': rel, 'problem': 'extra'})
    for rel in sorted(r for r in expected if r.endswith('.pointer.json') and r in actual):  # offloaded artifacts
        info = read_json(actual[rel]) or {}
        location = pathlib.Path(info.get('location', ''))
        for name, value in sorted((info.get('files') or {}).items()):
            target = location / name
            if not target.is_file():
                mismatches.append({'path': f'{rel}:{name}', 'problem': 'missing'})
            elif digest(target) != value:
                mismatches.append({'path': f'{rel}:{name}', 'problem': 'changed'})
    return mismatches


def artifact_store(project):
    return home() / 'artifacts' / project


def data_hashes(repo, patterns):
    """{path: {sha256 | dvc_md5, size}} for the data a run used (spec section 13).

    Paths are relative to the repository folder (data usually lives outside git). A `.dvc` file
    contributes the md5 DVC recorded for its output instead of hashing the data again."""
    root, out = pathlib.Path(repo), {}
    for pattern in patterns:
        for path in sorted(root.glob(str(pattern).replace('\\', '/'))):
            if not path.is_file() or path.is_symlink():
                continue
            rel = path.relative_to(root).as_posix()
            if path.suffix == '.dvc':
                import yaml
                try:
                    outs = (yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('outs') or []
                except (OSError, yaml.YAMLError):
                    outs = []
                out[rel] = {'dvc_md5': [o.get('md5') for o in outs if isinstance(o, dict)], 'size': path.stat().st_size}
            else:
                out[rel] = {'sha256': digest(path), 'size': path.stat().st_size}
    return out


def busy_file(repo, patterns, stale_s):
    """Repository-relative path of a busy runner summary matching patterns, or None."""
    root, now = pathlib.Path(repo), time.time()
    for pattern in patterns:
        for path in sorted(root.glob(str(pattern).replace('\\', '/'))):
            try:
                if not path.is_file() or json.loads(path.read_text(encoding='utf-8')).get('status') != 'running':
                    continue
                newest = max([p.stat().st_mtime for p in path.parent.rglob('*') if p.is_file()] or [0])
            except (OSError, ValueError, AttributeError):
                continue
            if now - newest < stale_s:
                return path.relative_to(root).as_posix()
    return None


def remove_backend_workspaces(rd, run):
    """Remove workspaces kept outside the run directory (WSL jobs work inside the distro, under
    ~/.easyci/work/<run-id>). Best effort: an unavailable distro is logged, never fatal."""
    from easyci import backends
    for backend_id in sorted({j.get('backend') for j in run.get('jobs', []) if backends.is_linux(j.get('backend'))}):
        try:
            backends.get(backend_id).cleanup(rd, run.get('run_id') or rd.name)
        except Exception as error:  # noqa: BLE001
            log(f'could not remove {backend_id} workspace of {rd.name}: {error}')


def clean(keep=None, default=20):
    """Delete old terminal runs. `keep` (from --keep) overrides everything; otherwise each project
    keeps its own `retention` from easyci.yml (recorded in project.json), else `default`."""
    deleted, kept = [], {}
    for project in (home() / 'projects').glob('*') if (home() / 'projects').exists() else []:
        limit = keep or (read_json(project / 'project.json') or {}).get('retention') or default
        kept[project.name] = limit
        runs = [(rd, read_json(rd / 'run.json') or {}) for rd in run_dirs(project.name)]
        protected = {}
        for rd, run in runs:  # newest passed run per profile survives
            if run.get('state') == 'passed':
                protected[run.get('profile') or run.get('event')] = rd.name
        terminal = [(rd, run) for rd, run in runs if run.get('state') in TERMINAL]
        for rd, run in terminal[:max(0, len(terminal) - limit)]:
            if rd.name not in protected.values():
                remove_backend_workspaces(rd, run)
                make_writable_and_remove(rd)
                make_writable_and_remove(artifact_store(project.name) / rd.name)
                deleted.append(rd.name)
    return deleted, kept


def elapsed(run):
    start = parse_iso(run.get('started_at') or run.get('created_at'))
    end = parse_iso(run.get('finished_at')) or time.time()
    return max(0, int(end - start)) if start else 0


def minutes(seconds):
    return int(math.floor((seconds or 0) / 60))
