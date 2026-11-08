"""`easyci doctor`: a checklist in English; every problem carries a fix sentence."""
import os
import shutil
import sys

from easyci import github, store
from easyci.backends import native, run_quiet, wsl


def check(id_, ok, detail, fix=None, required=True):
    return {'id': id_, 'ok': bool(ok), 'required': required, 'detail': detail, 'fix': None if ok else fix}


def run_checks(repo_info=None):
    checks = [check('python', sys.version_info >= (3, 11), f'Python {sys.version.split()[0]} at {sys.executable}',
                    'Install Python 3.11 or newer from python.org.')]
    try:
        import yaml
        checks.append(check('pyyaml', True, f'PyYAML {yaml.__version__}'))
    except ImportError:
        checks.append(check('pyyaml', False, 'PyYAML is not installed', 'Run: python -m pip install PyYAML'))
    git = shutil.which('git')
    code, text = run_quiet([git, '--version']) if git else (None, '')
    checks.append(check('git', code == 0, text or 'git not found', 'Install Git for Windows from git-scm.com.'))
    if os.name == 'nt':
        bash = native.git_bash()
        checks.append(check('bash', bool(bash), f'Git Bash: {bash}' if bash else 'Git Bash not found',
                            'Install Git for Windows (it includes bash.exe), or set EASYCI_BASH.'))
    auth = github.auth_status()
    if not auth['installed']:
        checks.append(check('gh', False, 'GitHub CLI (gh) not found',
                            'Install it from cli.github.com, then run: gh auth login   (or pass --no-report to run without GitHub statuses)'))
    elif not auth['logged_in']:
        checks.append(check('gh', False, 'gh is installed but not logged in',
                            'Run: gh auth login   (or pass --no-report to run without GitHub statuses)'))
    else:
        scope = '' if auth['repo_scope'] else '; token has no repo scope'
        checks.append(check('gh', auth['repo_scope'], f'gh logged in as {auth["account"]}{scope}',
                            'Run: gh auth refresh -s repo'))
    wanted = sorted({b for b in (repo_info or {}).get('backends', []) if b.startswith('wsl')})
    found = wsl.distros() if os.name == 'nt' else None
    if found is None:
        checks.append(check('wsl', False, 'WSL not found', 'Install WSL: wsl --install (needed only for wsl backends)',
                            required=bool(wanted)))
    else:
        listing = ', '.join(f'{d["name"]} ({d["state"]}{", default" if d["default"] else ""})' for d in found)
        checks.append(check('wsl', True, f'WSL distros: {listing or "none"}', required=bool(wanted)))
        names = {d['name'] for d in found}
        default = next((d['name'] for d in found if d['default']), None)
        for backend in wanted:
            distro = backend.partition(':')[2] or default
            if distro not in names:
                checks.append(check(f'wsl:{distro}', False, f'WSL distro {distro} not found',
                                    f'Create it, or change backend: in easyci.yml (found: {", ".join(sorted(names))}).'))
                continue
            versions = []
            for tool in ('node', 'julia'):
                code, text = run_quiet([wsl.wsl_exe(), '-d', distro, '--exec', tool, '--version'], timeout=60)
                versions.append(f'{tool} {text.splitlines()[0] if code == 0 and text else "not found"}')
            code, user = run_quiet([wsl.wsl_exe(), '-d', distro, '--exec', 'whoami'], timeout=60)
            note = '; default user is root' if user == 'root' else ''
            checks.append(check(f'wsl:{distro}', True, f'WSL distro {distro}: found, {", ".join(versions)}{note}'))
    docker = shutil.which('docker')
    checks.append(check('docker', True, 'docker: found (not needed)' if docker else 'docker: absent (optional)',
                        required=False))
    try:
        store.home().mkdir(parents=True, exist_ok=True)
        probe = store.home() / '.doctor-write-test'
        probe.write_text('ok')
        probe.unlink()
        checks.append(check('easyci_home', True, f'EasyCI folder: {store.home()}'))
    except OSError as error:
        checks.append(check('easyci_home', False, f'cannot write {store.home()}: {error}',
                            'Set EASYCI_HOME to a folder you can write to.'))
    if repo_info:
        checks.extend(repo_info.get('checks', []))
    busy = store.busy()
    checks.append(check('queue', True, f'machine busy with run {busy}' if busy else 'machine free', required=False))
    return checks


def sentence(item):
    mark = 'OK' if item['ok'] else ('PROBLEM' if item['required'] else 'note')
    return f'[{mark}] {item["detail"]}' + (f'\n       Fix: {item["fix"]}' if item.get('fix') else '')
