"""`native`: steps run directly on Windows (Git for Windows bash, pwsh/powershell/cmd when declared)."""
import json
import os
import pathlib
import platform
import shutil
import subprocess

from easyci import snapshot, store
from easyci.backends import NO_WINDOW, Proc, WinJob, collect, find_metrics, host_env, kill_tree, run_quiet

PROBES = {'node': ['node', '--version'], 'npm': ['npm', '--version'], 'python': ['python', '--version'],
          'julia': ['julia', '--version'], 'java': ['java', '-version'], 'go': ['go', 'version'],
          'git': ['git', '--version'], 'pwsh': ['pwsh', '-NoProfile', '-Command', '$PSVersionTable.PSVersion.ToString()'],
          'nvidia-smi': ['nvidia-smi', '-L']}


def git_bash():
    """Git for Windows bash; never System32\\bash.exe (that is WSL)."""
    if os.environ.get('EASYCI_BASH'):
        return os.environ['EASYCI_BASH']
    if os.name != 'nt':
        return shutil.which('bash') or '/bin/bash'
    git = shutil.which('git')
    candidates = []
    if git:
        root = pathlib.Path(git).resolve().parent.parent
        candidates += [root / 'bin' / 'bash.exe', root / 'usr' / 'bin' / 'bash.exe', root.parent / 'bin' / 'bash.exe']
    candidates += [pathlib.Path(os.environ.get('PROGRAMFILES', r'C:\Program Files')) / 'Git' / 'bin' / 'bash.exe']
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def shell_command(shell, path):
    p = str(path).replace('\\', '/') if shell in ('bash', 'sh') else str(path)
    if shell == 'bash':
        return [git_bash() or 'bash', '--noprofile', '--norc', '-eo', 'pipefail', p]
    if shell == 'sh':
        return [git_bash() or 'sh', '--posix', '-e', p]
    if shell in ('pwsh', 'powershell'):
        exe = 'pwsh' if shell == 'pwsh' else 'powershell'
        return [exe, '-NoLogo', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', f". '{p}'"]
    if shell == 'cmd':
        return f'cmd /D /E:ON /V:OFF /S /C "CALL "{p}""'  # a string: list quoting would break /S /C
    raise ValueError(shell)


def script_text(shell, body):
    if shell in ('pwsh', 'powershell'):
        return ("$ErrorActionPreference = 'stop'\n" + body +
                "\nif ((Test-Path -LiteralPath variable:\\LASTEXITCODE)) { exit $LASTEXITCODE }\n")
    if shell == 'cmd':
        return body.replace('\r\n', '\n').replace('\n', '\r\n')
    return body.replace('\r\n', '\n')


EXT = {'bash': '.sh', 'sh': '.sh', 'pwsh': '.ps1', 'powershell': '.ps1', 'cmd': '.cmd'}


class Native:
    id = 'native'

    def label(self):
        return f'windows-{platform.release().lower()}:native' if os.name == 'nt' else f'{platform.system().lower()}:native'

    def probe(self, tools):
        env = host_env()
        out = {}
        for tool in tools:
            cmd = list(PROBES.get(tool, [tool, '--version']))
            exe = shutil.which(cmd[0], path=env.get('PATH'))
            if not exe:
                out[tool] = None
                continue
            code, text = run_quiet([exe, *cmd[1:]], timeout=30, env=env)
            out[tool] = text.splitlines()[0] if code == 0 and text else None
        if 'julia' in tools:  # recorded for reproducibility (spec section 13)
            out['julia_depot'] = os.environ.get('JULIA_DEPOT_PATH') or str(pathlib.Path.home() / '.julia')
        return out

    def to_backend_path(self, win_path):
        return str(win_path)

    def workspace(self, rd, run_id, job):
        return str(pathlib.Path(rd) / 'workspace' / job)

    def checkout(self, ws, bundle, commit, tree):
        dest = pathlib.Path(ws)
        if dest.exists():
            store.make_writable_and_remove(dest)
        snapshot.clone_bundle(bundle, dest, commit, tree)

    def mkdirs(self, path):
        pathlib.Path(path).mkdir(parents=True, exist_ok=True)

    def exists(self, path):
        return pathlib.Path(path).is_dir()

    def start(self, shell, body, env, cwd, job_dir, index):
        steps = pathlib.Path(job_dir) / 'steps'
        steps.mkdir(parents=True, exist_ok=True)
        path = steps / f'{index}{EXT[shell]}'
        path.write_bytes(script_text(shell, body).encode('utf-8'))
        full_env = {**host_env(), **env}
        if os.name == 'nt' and getattr(self, 'winjob', None) is None:
            self.winjob = WinJob()
        job = getattr(self, 'winjob', None)
        kwargs = {'start_new_session': True} if os.name != 'nt' else {'creationflags': NO_WINDOW | 0x4}  # SUSPENDED
        popen = subprocess.Popen(shell_command(shell, path), cwd=cwd, env=full_env, stdin=subprocess.DEVNULL,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True, **kwargs)
        if job and not job.adopt(popen):
            store.log(f'could not assign step {index} to a job object; kill falls back to taskkill /T')
        return Proc(popen, {'backend': self.id, 'pid': popen.pid})

    def kill(self, proc):
        if getattr(self, 'winjob', None):
            self.winjob.terminate()
        kill_tree(proc.popen.pid)

    def end_job(self):
        """Like GitHub, processes a job left running (e.g. `server &`) end with the job."""
        job, self.winjob = getattr(self, 'winjob', None), None
        if job:
            job.terminate()
            job.close()

    def collect(self, ws, patterns, dest, layout='artifact'):
        return collect(ws, patterns, dest, layout)

    def metrics(self, ws):
        return find_metrics(ws)

    def cleanup(self, rd, run_id):
        store.make_writable_and_remove(pathlib.Path(rd) / 'workspace')


class Fake(Native):
    """Native execution with toolchain probes taken from EASYCI_FAKE_PROBES (tests and dry runs)."""
    id = 'fake'

    def label(self):
        return 'fake:native'

    def probe(self, tools):
        data = json.loads(os.environ.get('EASYCI_FAKE_PROBES') or '{}')
        return {tool: data.get(tool) for tool in tools}
