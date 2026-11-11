"""The `podman:<distro>` backend (spec Phase 3): GitHub `container:` and `services:` through Podman in WSL.

Per job: a private Podman network; each service container joins it under its service name, with
its `env`, published `ports` (on 127.0.0.1) and, when `--health-*` options are given, a wait until
it reports healthy. With a job `container:`, every step runs inside it via `podman exec`; the job
workspace and EasyCI's step folder are mounted at the same paths as in the distro, so paths in
`$GITHUB_WORKSPACE`, `$GITHUB_OUTPUT` and the like need no translation. Without one, steps run in
the distro itself (as on the `wsl` backend) and reach services through their published ports
(`${{ job.services.<id>.ports['5432'] }}`). Everything is removed when the job ends.
"""
import json
import posixpath
import re
import shlex

from easyci.backends.wsl import Wsl, to_linux, wsl_run

HEALTH = re.compile(r'--(health-cmd|health-interval|health-timeout|health-retries|health-start-period)[ =]'
                    r'(?:"([^"]*)"|\'([^\']*)\'|(\S+))')


def parse_options(text):
    """GitHub's `options:` string -> podman flags. Only health checks are understood; anything else
    raises ValueError (the planner reports it as unsupported)."""
    text = str(text or '').strip()
    flags, rest = [], HEALTH.sub(' ', text).strip()
    if rest:
        raise ValueError(rest)
    for match in HEALTH.finditer(text):
        flags += [f'--{match.group(1)}', match.group(2) or match.group(3) or match.group(4)]
    return flags


def last_error(text):
    """The podman error line, not the image-pull progress before it."""
    lines = [line for line in text.strip().splitlines() if line.strip()]
    errors = [line for line in lines if line.lower().startswith(('error', 'fatal')) or 'Error:' in line]
    return (errors[-1] if errors else (lines[-1] if lines else 'no output'))[:300]


def parse_ports(ports):
    """['5432:5432', 6379, '8080/tcp'] -> [(container_port, host_port or None)]."""
    out = []
    for item in ports or []:
        text = str(item).split('/')[0]
        if ':' in text:
            host, _, container = text.rpartition(':')
            out.append((container, host or None))
        else:
            out.append((text, None))
    return out


class Podman(Wsl):
    def __init__(self, distro):
        super().__init__(distro)
        self.id = f'podman:{self.distro}'
        self.container = None
        self.objects = []

    def label(self):
        code, text = wsl_run(self.distro, 'podman --version', timeout=60)
        return f'{self.id}:{text.strip()}' if code == 0 else self.id

    def probe(self, tools):
        found = super().probe([t for t in tools if t != 'podman'])
        if 'podman' in tools:
            code, text = wsl_run(self.distro, 'podman --version', timeout=60)
            found['podman'] = text.strip() if code == 0 else None
        return found

    def podman(self, args, timeout=600):
        return wsl_run(self.distro, 'podman ' + ' '.join(shlex.quote(str(a)) for a in args), timeout=timeout)

    # ---- per job ----------------------------------------------------------------------------
    def start_job(self, run_id, job_id, ws, job_dir, container=None, services=None):
        """Create the network, start services and the job container. Returns the job.services context."""
        name = re.sub(r'[^a-z0-9-]+', '-', f'easyci-{run_id}-{job_id}'.lower())[:60]
        net = name
        self.network, self.container, self.objects = net, None, []
        code, text = self.podman(['network', 'create', net])
        if code != 0:
            raise RuntimeError(f'podman network create failed: {text.strip()[-300:]}')
        context = {}
        for sid, spec in (services or {}).items():
            cname = f'{name}-{sid}'[:63]
            args = ['run', '-d', '--name', cname, '--network', net, '--network-alias', sid]
            for key, value in (spec.get('env') or {}).items():
                args += ['-e', f'{key}={value}']
            for cport, hport in parse_ports(spec.get('ports')):
                args += ['-p', f'127.0.0.1:{hport}:{cport}' if hport else f'127.0.0.1::{cport}']
            args += parse_options(spec.get('options')) + [spec['image']]
            code, text = self.podman(args, timeout=1800)  # first use pulls the image
            if code != 0:
                raise RuntimeError(f'service {sid} did not start: {last_error(text)}')
            self.objects.append(cname)
            if spec.get('options') and '--health-cmd' in str(spec['options']):
                self.wait_healthy(cname, sid)
            context[sid] = {'id': cname, 'network': net, 'ports': self.ports(cname)}
        if container:
            cname = f'{name}-job'[:63]
            self.mkdirs(ws)
            # Mount the run's work folder, not the workspace itself: checkout deletes and re-creates the
            # workspace, and a bind mount of the old folder would keep pointing at the deleted one.
            mounts = []
            for path in {posixpath.dirname(ws), to_linux(job_dir)}:
                mounts += ['-v', f'{path}:{path}']
            args = ['run', '-d', '--name', cname, '--network', net, *mounts, '-w', ws, '--entrypoint', 'tail']
            for key, value in (container.get('env') or {}).items():
                args += ['-e', f'{key}={value}']
            args += parse_options(container.get('options')) + [container['image'], '-f', '/dev/null']
            code, text = self.podman(args, timeout=1800)
            if code != 0:
                raise RuntimeError(f'job container did not start: {last_error(text)}')
            self.objects.append(cname)
            self.container = cname
        return context

    def wait_healthy(self, cname, sid, limit_s=300):
        import time
        deadline = time.time() + limit_s
        while time.time() < deadline:
            code, text = self.podman(['inspect', '--format', '{{.State.Health.Status}}', cname], timeout=60)
            status = text.strip()
            if status == 'healthy':
                return
            if code == 0 and status == 'unhealthy':
                break
            self.podman(['healthcheck', 'run', cname], timeout=120)
            time.sleep(2)
        raise RuntimeError(f'service {sid} did not become healthy')

    def ports(self, cname):
        code, text = self.podman(['inspect', '--format', '{{json .NetworkSettings.Ports}}', cname], timeout=60)
        out = {}
        try:
            for key, binds in (json.loads(text) or {}).items():
                if binds:
                    out[key.split('/')[0]] = binds[0].get('HostPort')
        except ValueError:
            pass
        return out

    def start(self, shell, body, env, cwd, job_dir, index):
        if not self.container:
            return super().start(shell, body, env, cwd, job_dir, index)
        # Inside the job container: the same wrapper, but the script runs via `podman exec`.
        names = sorted(k for k in env if re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k))
        exec_env = ' '.join(f'-e {k}' for k in names)
        script_path = shlex.quote(to_linux(f'{job_dir}/steps/{index}.sh'))
        # $GITHUB_PATH entries (EASYCI_PATH_PREPEND) go in front of the image's own PATH.
        prepend = 'if [ -n "$EASYCI_PATH_PREPEND" ]; then PATH="$EASYCI_PATH_PREPEND:$PATH"; export PATH; fi; '
        if shell == 'bash':  # as on GitHub: bash when the image has it, otherwise sh
            run = ("sh -c '" + prepend + "if command -v bash >/dev/null 2>&1; then "
                   "exec bash --noprofile --norc -eo pipefail \"$0\"; else exec sh -e \"$0\"; fi' " + script_path)
        else:
            run = "sh -c '" + prepend + "exec sh -e \"$0\"' " + script_path
        wrapped = f'exec podman exec -i {exec_env} -w {shlex.quote(cwd)} {self.container} {run}'
        return super().start(shell, body, env, cwd, job_dir, index, command=wrapped)

    def kill(self, proc):
        if self.container:  # stop everything the step started inside the container, but keep the
            # container itself (restarted, mounts intact) so on_kill and cleanup still run in it
            self.podman(['restart', '-t', '0', self.container], timeout=120)
        super().kill(proc)

    def probe_in_container(self, tools):
        """Tool versions inside the job container (setup-* steps are checked against the image)."""
        code, text = self.podman(['exec', self.container, 'sh', '-c', self.probe_script(tools)], timeout=120)
        found = dict(line.split('=', 1) for line in text.splitlines() if '=' in line) if code == 0 else {}
        return {tool: (found.get(tool) or '').strip() or None for tool in tools}

    def end_job(self):
        super().end_job()
        for name in reversed(self.objects):
            self.podman(['rm', '-f', '-t', '5', name], timeout=120)
        if getattr(self, 'network', None):
            self.podman(['network', 'rm', '-f', self.network], timeout=60)
        self.objects, self.container, self.network = [], None, None

    def cleanup(self, rd, run_id):
        super().cleanup(rd, run_id)
        self.remove_containers(run_id)

    def remove_containers(self, run_id):
        prefix = re.sub(r'[^a-z0-9-]+', '-', f'easyci-{run_id}'.lower())
        wsl_run(self.distro, f'podman ps -aq --filter name=^{prefix} | xargs -r podman rm -f >/dev/null 2>&1; '
                             f'podman network ls -q --filter name=^{prefix} | xargs -r podman network rm -f >/dev/null 2>&1',
                timeout=300)

