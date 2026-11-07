"""Git access with hygiene settings, repository facts and commit-pinned snapshots.

The bundle/clone/tree-check logic is re-implemented from scripts/ci.py (ingest/prepare).
"""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile

SHA = re.compile(r'[0-9a-f]{40}\Z')


class GitError(Exception):
    pass


def git_env():
    env = dict(os.environ)
    env.update({'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
                'GIT_OPTIONAL_LOCKS': '0'})
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CEILING_DIRECTORIES'):
        env.pop(name, None)
    return env


def git_exe():
    return os.environ.get('EASYCI_GIT') or shutil.which('git') or 'git'


def git(args, cwd, check=True, binary=False, timeout=300):
    cmd = [git_exe(), '-c', 'core.hooksPath=', '-c', 'credential.helper=', '-c', 'core.autocrlf=false', *args]
    result = subprocess.run(cmd, cwd=cwd, env=git_env(), capture_output=True, timeout=timeout,
                            stdin=subprocess.DEVNULL)
    if check and result.returncode != 0:
        raise GitError(f'git {args[0]} failed: {result.stderr.decode(errors="replace").strip()[:300]}')
    if binary:
        return result.stdout if result.returncode == 0 else None
    return result.stdout.decode('utf-8', errors='replace').strip() if result.returncode == 0 else None


def repo_root(path):
    out = git(['rev-parse', '--show-toplevel'], path, check=False)
    return pathlib.Path(out).resolve() if out else None


def resolve(repo, ref):
    out = git(['rev-parse', '--verify', '--quiet', f'{ref}^{{commit}}'], repo, check=False)
    return out if out and SHA.fullmatch(out) else None


def current_branch(repo):
    return git(['symbolic-ref', '--quiet', '--short', 'HEAD'], repo, check=False)


def default_branch(repo):
    head = git(['symbolic-ref', '--quiet', '--short', 'refs/remotes/origin/HEAD'], repo, check=False)
    if head and '/' in head:
        return head.split('/', 1)[1]
    for name in ('main', 'master'):
        if resolve(repo, f'refs/remotes/origin/{name}') or resolve(repo, f'refs/heads/{name}'):
            return name
    return current_branch(repo) or 'main'


def origin_url(repo):
    return git(['remote', 'get-url', 'origin'], repo, check=False)


def merge_base(repo, commit, branch):
    for ref in (f'refs/remotes/origin/{branch}', f'refs/heads/{branch}'):
        if resolve(repo, ref):
            out = git(['merge-base', commit, ref], repo, check=False)
            if out:
                return out
    return commit


def show(repo, commit, path):
    """File bytes at a commit, or None."""
    return git(['show', f'{commit}:{path}'], repo, check=False, binary=True)


def list_tree(repo, commit, path=None):
    args = ['ls-tree', '-r', '--name-only', commit] + ([path] if path else [])
    out = git(args, repo, check=False)
    return out.splitlines() if out else []


def make_bundle(repo, commit, base, dest):
    """Write a bundle holding exactly commit (refs/heads/candidate) and base; return its tree id."""
    dest = pathlib.Path(dest)
    with tempfile.TemporaryDirectory(prefix='.bundle-', dir=dest.parent) as staging:
        bare = pathlib.Path(staging) / 'source.git'
        git(['init', '--bare', '--quiet', str(bare)], staging)
        git(['fetch', '--quiet', '--no-tags', str(repo), f'{commit}:refs/heads/candidate',
             f'{base}:refs/heads/baseline'], bare)
        git(['bundle', 'create', '--quiet', str(dest), 'refs/heads/candidate', 'refs/heads/baseline'], bare)
        return git(['rev-parse', f'{commit}^{{tree}}'], bare)


def clone_bundle(bundle, dest, commit, tree):
    """Clean clone of the bundle at commit; verifies identity and rejects symlinks and submodules."""
    dest = pathlib.Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(['clone', '--quiet', '--no-hardlinks', '--no-checkout', str(bundle), str(dest)], dest.parent)
    git(['checkout', '--quiet', '--detach', commit], dest)
    if git(['rev-parse', 'HEAD'], dest) != commit or git(['rev-parse', 'HEAD^{tree}'], dest) != tree:
        raise GitError('checkout identity mismatch')
    return dest


def links_in_tree(repo, commit):
    """Paths of tracked symlinks (120000) and submodules (160000) at commit."""
    out = git(['ls-tree', '-r', commit], repo, check=False) or ''
    return [line.split('\t', 1)[1] for line in out.splitlines() if line.startswith(('120000 ', '160000 '))]
