"""Execution backends: turn "run this script with this env in this directory" into a process.

Backends never decide pass/fail; the supervisor does.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys

NO_WINDOW = 0x08000000 if os.name == 'nt' else 0

# Host variables a native step may see; everything else is constructed, not inherited.
HOST_ENV = ('PATH', 'PATHEXT', 'SYSTEMROOT', 'SYSTEMDRIVE', 'WINDIR', 'COMSPEC', 'TEMP', 'TMP', 'USERPROFILE',
            'HOMEDRIVE', 'HOMEPATH', 'HOME', 'APPDATA', 'LOCALAPPDATA', 'PROGRAMDATA', 'PROGRAMFILES',
            'PROGRAMFILES(X86)', 'PROGRAMW6432', 'COMMONPROGRAMFILES', 'COMPUTERNAME', 'USERNAME', 'USERDOMAIN',
            'NUMBER_OF_PROCESSORS', 'PROCESSOR_ARCHITECTURE', 'OS', 'LANG', 'TZ', 'SHELL', 'USER', 'LOGNAME',
            'WSL_UTF8')


class Proc:
    def __init__(self, popen, info):
        self.popen, self.info = popen, info


def host_env():
    upper = {k.upper(): v for k, v in os.environ.items()}
    return {k: upper[k] for k in HOST_ENV if k in upper}


def run_quiet(cmd, timeout=30, env=None):
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL, env=env,
                                creationflags=NO_WINDOW)
    except (OSError, subprocess.TimeoutExpired) as error:
        return None, str(error)
    out = result.stdout + result.stderr
    text = out.decode('utf-16-le', errors='replace') if b'\x00' in out else out.decode('utf-8', errors='replace')
    return result.returncode, text.replace('\x00', '').strip()


def job_limit_struct():
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [('a', ctypes.c_int64), ('b', ctypes.c_int64), ('LimitFlags', wintypes.DWORD),
                    ('c', ctypes.c_size_t), ('d', ctypes.c_size_t), ('e', wintypes.DWORD), ('f', ctypes.c_size_t),
                    ('g', wintypes.DWORD), ('h', wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6), ('limits', ctypes.c_size_t * 4)]
    return Extended()


class WinJob:
    """A Job object holding one step's whole process tree (MSYS re-parents children, so taskkill /T
    misses them). KILL_ON_JOB_CLOSE also ends the tree if the supervisor itself dies."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.k = k = ctypes.WinDLL('kernel32', use_last_error=True)
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise OSError(ctypes.get_last_error(), 'CreateJobObject failed')
        info = job_limit_struct()
        info.basic.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        k.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info))

    def adopt(self, popen):
        """Assign a CREATE_SUSPENDED process, then let it run; children inherit the job."""
        import ctypes
        from ctypes import wintypes
        ok = self.k.AssignProcessToJobObject(self.handle, wintypes.HANDLE(int(popen._handle)))
        ntdll = ctypes.WinDLL('ntdll')
        ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        ntdll.NtResumeProcess(wintypes.HANDLE(int(popen._handle)))
        return bool(ok)

    def terminate(self):
        if self.handle:
            self.k.TerminateJobObject(self.handle, 1)

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


def kill_tree(pid):
    if not pid:
        return
    if os.name == 'nt':
        run_quiet(['taskkill', '/T', '/F', '/PID', str(pid)], timeout=30)
    else:
        import signal
        try:
            os.killpg(int(pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


def get(backend_id):
    from easyci.backends import native, wsl
    if backend_id == 'native':
        return native.Native()
    if backend_id == 'fake':
        return native.Fake()
    if backend_id.startswith('wsl'):
        return wsl.Wsl(backend_id.partition(':')[2] or None)
    raise ValueError(f'unknown backend {backend_id}')


def kill_leftover(info):
    """Kill a step process tree recorded in a heartbeat (used by the reaper)."""
    if info.get('backend', '').startswith('wsl') and info.get('distro'):
        from easyci.backends import wsl
        wsl.kill_group(info['distro'], info.get('pgid_file'))
    kill_tree(info.get('pid'))


def collect(root, patterns, dest):
    """Copy files matching workspace-relative globs into dest; return copied relative paths."""
    root, dest, copied = pathlib.Path(root), pathlib.Path(dest), []
    resolved_root = root.resolve()
    for pattern in patterns:
        pattern = str(pattern).strip().replace('\\', '/')
        if not pattern or pattern.startswith('!') or pattern.startswith('/') or '..' in pattern.split('/'):
            continue
        for match in sorted(root.glob(pattern)):
            files = [match] if match.is_file() else sorted(p for p in match.rglob('*') if p.is_file())
            base = match.parent if match.is_file() else match
            if '*' in pattern:
                base = root
            for path in files:
                if path.is_symlink() or not path.resolve().is_relative_to(resolved_root):
                    continue
                rel = path.relative_to(base).as_posix()
                target = dest / rel
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                copied.append(rel)
    return copied


def find_metrics(root):
    for folder, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '.venv')]
        if 'easyci-metrics.json' in files:
            path = pathlib.Path(folder) / 'easyci-metrics.json'
            if path.stat().st_size <= 64 * 1024:
                try:
                    data = json.loads(path.read_text(encoding='utf-8'))
                except (OSError, ValueError):
                    return None
                return data if isinstance(data, dict) else None
    return None


def python_name():
    return 'python' if sys.platform == 'win32' else 'python3'


def version_matches(requested, actual):
    """Prefix match by the components given: 25 ~ 25.9.0, 1.12 ~ 1.12.7, 1.12.7 exact."""
    found = re.search(r'(\d+(?:\.\d+)*)', actual or '')
    if not found:
        return False
    have = found.group(1).split('.')
    want = str(requested).strip().lstrip('v').split('.')
    return len(have) >= len(want) and all(w in ('x', '*') or w == h for w, h in zip(want, have))
