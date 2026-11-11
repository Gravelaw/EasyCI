"""`wsl:<distro>`: steps run with `wsl -d <distro> --exec setsid --wait bash` on the distro's own ext4.

`--wait` matters: under `wsl --exec` the caller is a group leader, so a bare `setsid` forks and
returns 0 immediately, which would report every step as passed before it ran.

Environment crosses via WSLENV (values never touch disk); the workspace lives under
~/.easyci/work/<run-id>/<job> inside the distro; kill is by process group.
"""
import os
import pathlib
import re
import shlex
import subprocess
import time

from easyci.backends import NO_WINDOW, Proc, collect, find_metrics, kill_tree, run_quiet

PROBE_TOOLS = {'python': 'python3'}


def wsl_exe():
    return os.environ.get('EASYCI_WSL') or 'wsl'


def distros():
    """[{name, state, version, default}] from `wsl -l -v`, or None when WSL is unavailable."""
    code, text = run_quiet([wsl_exe(), '-l', '-v'], timeout=30, env={**os.environ, 'WSL_UTF8': '1'})
    if code != 0 or not text:
        return None
    out = []
    for line in text.splitlines()[1:]:
        default = line.lstrip().startswith('*')
        parts = line.replace('*', ' ').split()
        if len(parts) >= 3:
            out.append({'name': ' '.join(parts[:-2]), 'state': parts[-2], 'version': parts[-1], 'default': default})
    return out


def default_distro():
    for item in distros() or []:
        if item['default']:
            return item['name']
    return None


def to_linux(win_path):
    path = str(pathlib.Path(win_path).resolve()).replace('\\', '/')
    match = re.match(r'([A-Za-z]):/(.*)', path)
    if not match:
        raise ValueError(f'{win_path} is not on a Windows drive WSL can mount')
    return f'/mnt/{match.group(1).lower()}/{match.group(2)}'


def wsl_run(distro, script, timeout=120):
    return run_quiet([wsl_exe(), '-d', distro, '--exec', 'bash', '-c', script], timeout=timeout,
                     env={**os.environ, 'WSL_UTF8': '1'})


def kill_group(distro, pgid_file):
    try:
        pgid = pathlib.Path(pgid_file).read_text().strip() if pgid_file else ''
    except OSError:
        pgid = ''
    if not pgid.isdigit():
        return
    wsl_run(distro, f'kill -TERM -- -{pgid} 2>/dev/null; true', timeout=30)
    deadline = time.time() + 15
    while time.time() < deadline:
        code, _ = wsl_run(distro, f'kill -0 -- -{pgid} 2>/dev/null', timeout=30)
        if code != 0:
            return
        time.sleep(1)
    wsl_run(distro, f'kill -KILL -- -{pgid} 2>/dev/null; true', timeout=30)


class Wsl:
    def __init__(self, distro):
        self.distro = distro or default_distro()
        if not self.distro:
            raise RuntimeError('no default WSL distro')
        self.id = f'wsl:{self.distro}'
        self._home = None

    def home(self):
        if self._home is None:
            code, text = wsl_run(self.distro, 'printf %s "$HOME"', timeout=60)
            if code != 0 or not text.startswith('/'):
                raise RuntimeError(f'cannot reach WSL distro {self.distro}: {text}')
            self._home = text
        return self._home

    def label(self):
        code, text = wsl_run(self.distro, '. /etc/os-release; printf "%s-%s" "$ID" "$VERSION_ID"', timeout=60)
        return f'{self.id}:{text}' if code == 0 else self.id

    def probe(self, tools):
        code, text = wsl_run(self.distro, self.probe_script(tools), timeout=120)
        found = dict(line.split('=', 1) for line in text.splitlines() if '=' in line) if code == 0 else {}
        keys = [*tools, 'julia_depot'] if 'julia' in tools else tools
        return {tool: (found.get(tool) or '').strip() or None for tool in keys}

    @staticmethod
    def probe_script(tools):
        """A POSIX sh script printing `tool=<first version line>` for each tool."""
        lines = []
        for tool in tools:
            exe = PROBE_TOOLS.get(tool, tool)
            flag = '-version' if tool == 'java' else 'version' if tool == 'go' else '-L' if tool == 'nvidia-smi' else '--version'
            lines.append(f'printf "%s=" {shlex.quote(tool)}; if command -v {shlex.quote(exe)} >/dev/null; then '
                         f'{shlex.quote(exe)} {flag} 2>&1 | grep -m 1 .; else echo; fi')  # first non-empty line (gradle starts blank)
        if 'julia' in tools:
            lines.append('printf "julia_depot=%s\\n" "${JULIA_DEPOT_PATH:-$HOME/.julia}"')
        return '\n'.join(lines)

    def to_backend_path(self, win_path):
        return to_linux(win_path)

    def workspace(self, rd, run_id, job):
        return f'{self.home()}/.easyci/work/{run_id}/{job}'

    def checkout(self, ws, bundle, commit, tree, refs=None):
        q = shlex.quote
        update = ''.join(f'; git update-ref {q(ref)} {q(sha)}' for ref, sha in (refs or {}).items()
                         if re.fullmatch(r'refs/[A-Za-z0-9._/-]+', ref) and re.fullmatch(r'[0-9a-f]{40}', sha))
        script = (f'set -e; rm -rf {q(ws)}; mkdir -p "$(dirname {q(ws)})"; '
                  f'export GIT_CONFIG_NOSYSTEM=1 GIT_CONFIG_GLOBAL=/dev/null GIT_TERMINAL_PROMPT=0; '
                  f'git -c core.hooksPath= -c credential.helper= clone --quiet --no-hardlinks --no-checkout '
                  f'{q(to_linux(bundle))} {q(ws)}; cd {q(ws)}; git -c core.hooksPath= checkout --quiet --detach {commit}; '
                  f'test "$(git rev-parse HEAD^{{tree}})" = {tree}{update}')
        code, text = wsl_run(self.distro, script, timeout=1800)
        if code != 0:
            raise RuntimeError(f'clone inside {self.distro} failed: {text[-300:]}')

    def mkdirs(self, path):
        wsl_run(self.distro, f'mkdir -p {shlex.quote(path)}')

    def exists(self, path):
        return wsl_run(self.distro, f'test -d {shlex.quote(path)}')[0] == 0

    def start(self, shell, body, env, cwd, job_dir, index, command=None):
        steps = pathlib.Path(job_dir) / 'steps'
        steps.mkdir(parents=True, exist_ok=True)
        script, wrapper, pgid = steps / f'{index}.sh', steps / f'{index}.wrap.sh', steps / f'{index}.pgid'
        script.write_bytes(body.replace('\r\n', '\n').encode('utf-8'))
        run = 'bash --noprofile --norc -eo pipefail' if shell == 'bash' else 'sh -e'
        q = shlex.quote
        last = command or f'exec {run} {q(to_linux(script))}'
        wrapper.write_bytes((f'echo $$ > {q(to_linux(pgid))}\ncd {q(cwd)} || exit 1\n'
                             '[ -n "${EASYCI_PATH_PREPEND:-}" ] && export PATH="$EASYCI_PATH_PREPEND:$PATH"\n'
                             f'{last}\n').encode('utf-8'))
        names = [k for k in env if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)]
        full_env = {**os.environ, **{k: env[k] for k in names}, 'WSLENV': ':'.join(names)}
        popen = subprocess.Popen([wsl_exe(), '-d', self.distro, '--exec', 'setsid', '--wait', 'bash', to_linux(wrapper)],
                                 env=full_env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, creationflags=NO_WINDOW, close_fds=True)
        self.groups = [*getattr(self, 'groups', []), str(pgid)]
        return Proc(popen, {'backend': self.id, 'distro': self.distro, 'pid': popen.pid, 'pgid_file': str(pgid)})

    def kill(self, proc):
        kill_group(self.distro, proc.info.get('pgid_file'))
        kill_tree(proc.popen.pid)

    def end_job(self):
        """Like GitHub (and the native Job object), anything a step left running, such as a service
        started with `&`, ends with the job: every step's process group is signalled."""
        groups, self.groups = getattr(self, 'groups', []), []
        for pgid_file in groups:
            kill_group(self.distro, pgid_file)

    def unc(self, path):
        return pathlib.Path(rf'\\wsl.localhost\{self.distro}' + path.replace('/', '\\'))

    def collect(self, ws, patterns, dest, layout='artifact'):
        return collect(self.unc(ws), patterns, dest, layout)

    def hash_files(self, ws, patterns):
        from easyci.backends import hash_files
        return hash_files(self.unc(ws), patterns)

    def copy_in(self, src_dir, ws, rel):
        from easyci.backends import copy_in
        return copy_in(src_dir, self.unc(f'{ws}/{rel}' if rel else ws))

    def cache(self, project):
        from easyci.cache import WslCache
        return WslCache(self, project)

    def metrics(self, ws):
        return find_metrics(self.unc(ws))

    # Services live under /var/tmp/easyci/<run>/<job> (readable by the postgres user, unlike /root).
    # As root, PostgreSQL runs as the distro's postgres user (initdb refuses root); as a normal
    # default user it runs as that user, which needs no privileges.
    AS_PG = ('if [ "$(id -u)" = 0 ]; then as_pg() { runuser -u postgres -- "$@"; }; pg_owner=postgres; '
             'else as_pg() { "$@"; }; pg_owner=$(id -un); fi')

    @staticmethod
    def kill_pg(folder, pid_files):
        # As root, every leftover process of the postgres user under that folder. As a normal user,
        # `pkill -f` would also match this very shell (its command line names the folder), so the
        # postmasters are found through their pid files instead.
        return (f'if [ "$pg_owner" = postgres ]; then pkill -u postgres -f -- {folder}/; '
                f'else for f in {pid_files}; do [ -f "$f" ] && kill -9 "$(head -1 "$f")"; done; fi 2>/dev/null')

    def service_dir(self, run_id, job):
        return f'/var/tmp/easyci/{run_id}/{job}'

    def start_services(self, services, run_id, job):
        """Start a private PostgreSQL for one job; return the env it exports."""
        import secrets as secrets_
        if set(services) != {'postgres'}:
            raise RuntimeError(f'unsupported services {sorted(services)}')
        version = str((services.get('postgres') or {}).get('version') or '')
        port = 30000 + secrets_.randbelow(20000)
        password = secrets_.token_hex(16)
        d = self.service_dir(run_id, job)
        q = shlex.quote
        script = f'''set -e
{self.AS_PG}
bin=$(ls -d /usr/lib/postgresql/{q(version) if version else "*"}/bin 2>/dev/null | sort -V | tail -1)
[ -x "$bin/initdb" ] || {{ echo "PostgreSQL {version or ''} is not installed in this distro (apt install postgresql)" >&2; exit 7; }}
[ "$pg_owner" != postgres ] || id postgres >/dev/null 2>&1 || {{ echo "no postgres user in this distro" >&2; exit 7; }}
if [ "$pg_owner" = postgres ]; then install -d -m 1777 /var/tmp/easyci; fi  # shared by every user, like /tmp
rm -rf {q(d)}; mkdir -p {q(d)}/pg {q(d)}/socket || {{ echo "cannot create {d} as $(id -un)" >&2; exit 7; }}
printf %s {q(password)} > {q(d)}/pw
chown -R "$pg_owner" {q(d)}; chmod 700 {q(d)}/pg; chmod 600 {q(d)}/pw
as_pg "$bin/initdb" -D {q(d)}/pg -U easyci --pwfile={q(d)}/pw --auth-host=scram-sha-256 --auth-local=trust --no-instructions >{q(d)}/init.log 2>&1
as_pg "$bin/pg_ctl" -D {q(d)}/pg -l {q(d)}/postgres.log -o "-p {port} -h 127.0.0.1 -k {d}/socket" -w -t 60 start >{q(d)}/start.log 2>&1
as_pg "$bin/createdb" -h {q(d)}/socket -p {port} -U easyci easyci
echo "$bin" > {q(d)}/bindir'''
        code, text = wsl_run(self.distro, script, timeout=180)
        if code != 0:
            raise RuntimeError(f'postgres service did not start: {text.strip()[-400:]}')
        url = f'postgresql://easyci:{password}@127.0.0.1:{port}/easyci'
        return {'DATABASE_URL': url, 'PGHOST': '127.0.0.1', 'PGPORT': str(port), 'PGUSER': 'easyci',
                'PGPASSWORD': password, 'PGDATABASE': 'easyci'}

    def stop_services(self, run_id, job):
        d = shlex.quote(self.service_dir(run_id, job))
        wsl_run(self.distro, f'{self.AS_PG}; [ -f {d}/bindir ] && as_pg "$(cat {d}/bindir)/pg_ctl" -D {d}/pg '
                             f'-m immediate -w -t 30 stop >/dev/null 2>&1; {self.kill_pg(d, f"{d}/pg/postmaster.pid")}; '
                             f'rm -rf {d}', timeout=120)

    def cleanup(self, rd, run_id):
        wsl_run(self.distro, f'rm -rf {shlex.quote(f"{self.home()}/.easyci/work/{run_id}")}', timeout=600)
        services = shlex.quote(f'/var/tmp/easyci/{run_id}')
        wsl_run(self.distro, f'[ -d {services} ] || exit 0; {self.AS_PG}; {self.kill_pg(services, f"{services}/*/pg/postmaster.pid")}; sleep 1; '
                             f'rm -rf {services}', timeout=120)
