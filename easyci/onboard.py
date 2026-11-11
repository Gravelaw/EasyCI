"""`easyci setup`: get any repository ready for EasyCI (spec section 7, Phase 2).

It detects the project's stacks from files at the repository root, picks the backend that has the
tools (a WSL distro first, then the Windows host), and, only when asked, writes a starter
`.github/workflows/ci.yml` (if the repository has none) and an `easyci.yml`. It never overwrites a
file and never commits unless `--commit` is given, and then only the files it wrote.

The starter workflow is ordinary GitHub Actions YAML inside what EasyCI understands, so the same
file also runs on GitHub if hosted Actions are ever turned on.
"""
import json
import os
import pathlib
import re
import shutil

from easyci import snapshot

STACK_TOOLS = {'node': ['node', 'npm'], 'python': ['python'], 'julia': ['julia'], 'go': ['go'],
               'rust': ['cargo'], 'maven': ['java', 'mvn'], 'gradle': ['java'], 'dotnet': ['dotnet']}
LABELS = {'node': 'Node.js', 'python': 'Python', 'julia': 'Julia', 'go': 'Go', 'rust': 'Rust',
          'maven': 'Java (Maven)', 'gradle': 'Java (Gradle)', 'dotnet': '.NET'}


def read(path):
    try:
        return pathlib.Path(path).read_text(encoding='utf-8', errors='replace')
    except OSError:
        return ''


def major(text, digits=1):
    match = re.search(r'(\d+(?:\.\d+){0,%d})' % (digits - 1), text or '')
    return match.group(1) if match else None


# ---- detection ----------------------------------------------------------------------------

def detect(repo):
    """[{stack, files, ...details}] for the stacks found at the repository root."""
    root, found = pathlib.Path(repo), []
    if (root / 'package.json').exists():
        try:
            pkg = json.loads(read(root / 'package.json') or '{}')
        except ValueError:
            pkg = {}
        manager = ('pnpm' if (root / 'pnpm-lock.yaml').exists() else 'yarn' if (root / 'yarn.lock').exists()
                   else 'npm')
        version = (read(root / '.nvmrc').strip() or read(root / '.node-version').strip() or
                   str((pkg.get('engines') or {}).get('node', '')))
        found.append({'stack': 'node', 'manager': manager, 'scripts': sorted((pkg.get('scripts') or {})),
                      'locked': any((root / f).exists() for f in ('package-lock.json', 'pnpm-lock.yaml', 'yarn.lock')),
                      'version': major(version)})
    py_files = [f for f in ('pyproject.toml', 'setup.py', 'setup.cfg', 'requirements.txt') if (root / f).exists()]
    if py_files:
        text = ' '.join(read(root / f) for f in (*py_files, 'requirements-dev.txt', 'requirements-test.txt',
                                                  'tox.ini', 'pytest.ini'))
        pyproject = read(root / 'pyproject.toml')
        extras = [e for e in ('test', 'tests', 'dev') if re.search(rf'^\s*{e}\s*=\s*\[', pyproject, re.M)]
        found.append({'stack': 'python', 'requirements': [f for f in ('requirements.txt', 'requirements-dev.txt',
                                                                        'requirements-test.txt') if (root / f).exists()],
                      'installable': bool(re.search(r'^\[project\]|^\[tool\.poetry\]', pyproject, re.M))
                      or (root / 'setup.py').exists(),
                      'extras': extras[:1], 'pytest': 'pytest' in text or (root / 'conftest.py').exists()
                      or (root / 'tests' / 'conftest.py').exists(),
                      'version': major(read(root / '.python-version'), 2) or
                      major(re.search(r'requires-python\s*=\s*"[^"]*"', pyproject).group(0)
                            if re.search(r'requires-python\s*=\s*"[^"]*"', pyproject) else '', 2)})
    if (root / 'Project.toml').exists():
        manifest = read(root / 'Manifest.toml')
        jv = re.search(r'julia_version\s*=\s*"(\d+\.\d+)', manifest)
        found.append({'stack': 'julia', 'version': jv.group(1) if jv else None,
                      'tests': (root / 'test' / 'runtests.jl').exists()})
    if (root / 'go.mod').exists():
        gv = re.search(r'^go\s+(\d+\.\d+)', read(root / 'go.mod'), re.M)
        found.append({'stack': 'go', 'version': gv.group(1) if gv else None})
    if (root / 'Cargo.toml').exists():
        found.append({'stack': 'rust', 'locked': (root / 'Cargo.lock').exists()})
    if (root / 'pom.xml').exists():
        found.append({'stack': 'maven', 'wrapper': (root / 'mvnw').exists()})
    elif any((root / f).exists() for f in ('build.gradle', 'build.gradle.kts')):
        found.append({'stack': 'gradle', 'wrapper': (root / 'gradlew').exists()})
    if any(root.glob('*.sln')) or any(root.glob('*.csproj')) or any(root.glob('*.fsproj')):
        found.append({'stack': 'dotnet'})
    return found


# ---- backend choice -------------------------------------------------------------------------

def candidates():
    """[(backend_id, probe_function)] in order of preference: WSL distros, then the host."""
    from easyci.backends import native, wsl
    if os.environ.get('EASYCI_FAKE_PROBES'):  # tests and dry runs
        return [('fake', native.Fake().probe)]
    out = []
    for item in wsl.distros() or []:
        if not item['name'].lower().startswith('docker-desktop'):
            backend = wsl.Wsl(item['name'])
            out.append((f'wsl:{item["name"]}', backend.probe))
    out.append(('native', native.Native().probe))
    return out


def stack_tools(stack):
    """Tools the generated steps call. Without a wrapper script, Gradle itself must be installed."""
    tools = list(STACK_TOOLS[stack['stack']])
    if stack['stack'] == 'gradle' and not stack.get('wrapper'):
        tools.append('gradle')
    return tools


def choose_backend(stacks, probes=None):
    """{'backend', 'have': {tool: version}, 'missing': [tool]} for the best candidate."""
    tools = sorted({t for s in stacks for t in stack_tools(s)} | {'git'})
    best = None
    for backend_id, probe in (probes if probes is not None else candidates()):
        try:
            have = {k: v for k, v in probe(tools).items() if v}
        except Exception:  # noqa: BLE001 - an unreachable distro is just not a candidate
            continue
        missing = [t for t in tools if t not in have]
        if best is None or len(missing) < len(best['missing']):
            best = {'backend': backend_id, 'have': have, 'missing': missing}
        if not missing:
            break
    return best or {'backend': 'native', 'have': {}, 'missing': tools}


# ---- templates --------------------------------------------------------------------------------

def tool_major(have, tool, digits=1):
    return major(have.get(tool) or '', digits)


def job_for(stack, have, linux):
    s, kind = stack, stack['stack']
    steps = ['      - uses: actions/checkout@v4']
    py = 'python3' if linux else 'python'
    if kind == 'node':
        version = s.get('version') or tool_major(have, 'node') or '20'
        steps.append(f'      - uses: actions/setup-node@v4\n        with:\n          node-version: {version}')
        if s['manager'] == 'npm':
            install = 'npm ci' if s['locked'] else 'npm install'
            run = 'npm run'
        else:
            install = f'corepack enable && {s["manager"]} install' + (' --frozen-lockfile' if s['locked'] else '')
            run = f'{s["manager"]} run'
        steps.append(f'      - run: {install}')
        for script in ('lint', 'build'):
            if script in s['scripts']:
                steps.append(f'      - run: {run} {script}')
        if 'test' in s['scripts']:
            steps.append(f'      - run: {run} test')
    elif kind == 'python':
        version = s.get('version') or tool_major(have, 'python', 2) or '3.12'
        steps.append(f'      - uses: actions/setup-python@v5\n        with:\n          python-version: "{version}"')
        activate = '. .venv/bin/activate' if linux else '. .venv/Scripts/activate'
        lines = [f'{py} -m venv .venv', activate]
        lines += [f'pip install -r {r}' for r in s['requirements']]
        if s['installable']:
            lines.append('pip install -e ".' + (f'[{s["extras"][0]}]' if s['extras'] else '') + '"')
        if s['pytest']:
            lines += ['pip install pytest', 'python -m pytest']
        else:
            lines.append('python -m unittest discover -v')
        steps.append('      - name: Install and test\n        run: |\n' + ''.join(f'          {line}\n' for line in lines).rstrip('\n'))
    elif kind == 'julia':
        version = s.get('version') or tool_major(have, 'julia', 2) or '1'
        steps.append(f'      - uses: julia-actions/setup-julia@v2\n        with:\n          version: "{version}"')
        steps.append("      - run: julia --project=. -e 'using Pkg; Pkg.instantiate()'")
        if s['tests']:
            steps.append("      - run: julia --project=. -e 'using Pkg; Pkg.test()'")
    elif kind == 'go':
        version = s.get('version') or tool_major(have, 'go', 2) or '1.22'
        steps.append(f'      - uses: actions/setup-go@v5\n        with:\n          go-version: "{version}"')
        steps += ['      - run: go build ./...', '      - run: go test ./...']
    elif kind == 'rust':
        locked = ' --locked' if s['locked'] else ''
        steps += [f'      - run: cargo build{locked}', f'      - run: cargo test{locked}']
    elif kind in ('maven', 'gradle'):
        version = tool_major(have, 'java') or '21'
        steps.append(f'      - uses: actions/setup-java@v4\n        with:\n          distribution: temurin\n'
                     f'          java-version: "{version}"')
        if kind == 'maven':
            steps.append('      - run: ' + ('./mvnw' if s['wrapper'] else 'mvn') + ' -B verify')
        else:
            steps.append('      - run: ' + ('./gradlew' if s['wrapper'] else 'gradle') + ' build')
    elif kind == 'dotnet':
        steps += ['      - run: dotnet build', '      - run: dotnet test']
    runs_on = 'ubuntu-latest' if linux else 'windows-latest'
    shell = '' if linux else '    defaults:\n      run:\n        shell: bash\n'
    return f'  {kind}:\n    runs-on: {runs_on}\n{shell}    steps:\n' + '\n'.join(steps) + '\n'


def workflow_text(stacks, have, linux, default_branch='main'):
    jobs = ''.join(job_for(s, have, linux) for s in stacks)
    return ('# Written by `easyci setup`. Ordinary GitHub Actions YAML: EasyCI runs it on this PC, and\n'
            '# GitHub runs the same file if hosted Actions are turned on. Edit it freely.\n'
            'name: ci\n\non:\n  push:\n    branches: [' + default_branch + ']\n  pull_request:\n\njobs:\n' + jobs)


def easyci_yml_text(stacks, backend):
    lines = ['# Written by `easyci setup`. All keys are explained in docs/usage.md of EasyCI.',
             'version: 1', f'backend: {backend}', 'report: true']
    if any(s['stack'] == 'julia' for s in stacks):
        lines += ['jobs:', '  julia:', '    stall_minutes: 45   # Pkg.instantiate and precompile are quiet']
    return '\n'.join(lines) + '\n'


# ---- the whole step -----------------------------------------------------------------------------

def plan(repo, probes=None):
    """What setup would do, without writing anything."""
    root = pathlib.Path(repo)
    commit = snapshot.resolve(root, 'HEAD')
    workflows = sorted(p.name for p in (root / '.github' / 'workflows').glob('*.y*ml')) \
        if (root / '.github' / 'workflows').is_dir() else []
    stacks = detect(root)
    choice = choose_backend(stacks, probes) if stacks or not workflows else None
    linux = bool(choice) and choice['backend'].startswith('wsl')
    default = snapshot.default_branch(root) if commit else 'main'
    files = []
    if not workflows and stacks:
        files.append({'path': '.github/workflows/ci.yml', 'text': workflow_text(stacks, choice['have'], linux, default)})
    if not (root / 'easyci.yml').exists() and choice:
        files.append({'path': 'easyci.yml', 'text': easyci_yml_text(stacks, choice['backend'])})
    return {'repo': str(root), 'has_commits': bool(commit), 'workflows': workflows,
            'stacks': [{**s, 'label': LABELS[s['stack']]} for s in stacks], 'backend': choice,
            'files': files}


def write(repo, files):
    """Write the planned files; never overwrite. Return the paths written."""
    written = []
    for item in files:
        path = pathlib.Path(repo) / item['path']
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(item['text'].encode('utf-8'))
        written.append(item['path'])
    return written


SETUP_PATHS = ('.github/workflows/ci.yml', 'easyci.yml')


def uncommitted(repo):
    """Files setup writes that exist but are not tracked yet, e.g. after `setup --write`."""
    import subprocess
    out = []
    for path in SETUP_PATHS:
        if (pathlib.Path(repo) / path).is_file():
            tracked = subprocess.run(['git', 'ls-files', '--error-unmatch', '--', path], cwd=repo,
                                     capture_output=True, stdin=subprocess.DEVNULL).returncode == 0
            if not tracked:
                out.append(path)
    return out


class CommitError(Exception):
    pass


def commit(repo, paths):
    """Commit only `paths`, as the user (their own git identity and config, unlike EasyCI's snapshots)."""
    import subprocess
    if not paths:
        return None
    for args in (['add', '--', *paths], ['commit', '-q', '-m', 'Add EasyCI checks', '--', *paths]):
        result = subprocess.run(['git', *args], cwd=repo, capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            raise CommitError(result.stderr.strip().splitlines()[-1] if result.stderr.strip() else 'git failed')
    return snapshot.resolve(repo, 'HEAD')


def desktop_dir():
    if os.environ.get('EASYCI_DESKTOP'):  # tests, or a different launcher folder
        return pathlib.Path(os.environ['EASYCI_DESKTOP'])
    if os.name == 'nt':
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buf) == 0:  # CSIDL_DESKTOPDIRECTORY
            return pathlib.Path(buf.value)
    return pathlib.Path.home() / 'Desktop'


def write_launcher(repo, python_exe, folder=None):
    """A double-click `Run checks - <project>.cmd` that runs `easyci run --wait` and keeps the window open."""
    name = re.sub(r'[^A-Za-z0-9 ._-]+', '-', pathlib.Path(repo).name) or 'project'
    target = pathlib.Path(folder or desktop_dir()) / f'Run checks - {name}.cmd'
    target.write_text('@echo off\r\n'
                      f'title EasyCI - {name}\r\n'
                      f'cd /d "{repo}"\r\n'
                      f'"{python_exe}" -m easyci run --wait\r\n'
                      'echo.\r\n'
                      'pause\r\n', encoding='utf-8')
    return str(target)


def write_dashboard_launcher(python_exe, folder=None):
    """A double-click `EasyCI dashboard.cmd` that opens the dashboard in the browser."""
    target = pathlib.Path(folder or desktop_dir()) / 'EasyCI dashboard.cmd'
    target.write_text('@echo off\r\ntitle EasyCI dashboard\r\n'
                      f'"{python_exe}" -m easyci dashboard --open\r\n'
                      'pause\r\n', encoding='utf-8')
    return str(target)


def sentences(result):
    """Plain-English lines for a person (spec section 7)."""
    out = []
    stacks = result['stacks']
    out.append('Found: ' + (', '.join(s['label'] for s in stacks) if stacks else 'no project type EasyCI recognises') + '.')
    if result['workflows']:
        out.append(f'Workflows already here: {", ".join(result["workflows"])}. EasyCI will run them as they are.')
    choice = result['backend']
    if choice:
        where = choice['backend'].replace('wsl:', 'the WSL distro ').replace('native', 'Windows itself')
        out.append(f'Tests will run on {where}.')
        if choice['missing']:
            out.append(f'Missing there: {", ".join(choice["missing"])}. Fix: install them in {where}, then run '
                       '`easyci setup` again.')
    for item in result['files']:
        out.append(f'{"Wrote" if item.get("written") else "Would write"} {item["path"]}.')
    if not result['files'] and not result['workflows']:
        out.append('Nothing to write: add a test command to the project first, or write a workflow by hand.')
    return out


def which_python():
    return shutil.which('python') or shutil.which('py') or 'python'
