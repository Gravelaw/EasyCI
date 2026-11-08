"""`easyci promote`: copy a passed run's artifact into a durable release folder (spec section 12, Phase 3).

Re-implemented from the legacy scaffold's fail-closed promotion: only a passed, sealed run whose
evidence verifies can be promoted; every file is checked against the hash recorded when the run
was sealed, both before and after copying; the release folder is assembled in a staging folder and
renamed into place in one step; an existing release is never overwritten; a lock folder makes
concurrent promotions fail instead of interleave. The release gets a `receipt.json` naming the run,
commit, tree, artifact and every file's sha256, and becomes read-only. `easyci verify <folder>`
re-checks a release against its receipt.
"""
import os
import pathlib
import re
import shutil
import socket
import stat
import tempfile

from easyci import SCHEMA, VERSION, store


class PromoteError(Exception):
    def __init__(self, code, message, fix):
        super().__init__(message)
        self.code, self.message, self.fix = code, message, fix


def artifact_files(rd, name):
    """{relative path: (absolute path, sealed sha256)} for an artifact of a run, from the seal."""
    inventory = (store.read_json(rd / 'sha256.json') or {}).get('files', {})
    found = {}
    for job_dir in sorted((rd / 'jobs').glob('*')):
        prefix = f'jobs/{job_dir.name}/artifacts/{name}/'
        for rel, sha in inventory.items():
            if rel.startswith(prefix):
                found[rel[len(prefix):]] = (rd / rel, sha)
        pointer = job_dir / 'artifacts' / f'{name}.pointer.json'
        if f'jobs/{job_dir.name}/artifacts/{name}.pointer.json' in inventory and pointer.exists():
            info = store.read_json(pointer) or {}
            for rel, sha in (info.get('files') or {}).items():
                found[rel] = (pathlib.Path(info['location']) / rel, sha)
    return found


def promote(rd, name, dest_root, version=None):
    rd, dest_root = pathlib.Path(rd), pathlib.Path(dest_root)
    run = store.read_json(rd / 'run.json') or {}
    if run.get('state') != 'passed':
        raise PromoteError('NOT_PASSED', f'Run {rd.name} is {run.get("state")}; only a passed run can be promoted.',
                           'Promote a run that passed.')
    mismatches = store.verify(rd)
    if mismatches is None:
        raise PromoteError('NOT_SEALED', 'The run is not sealed yet.', 'Wait for it to finish.')
    if mismatches:
        raise PromoteError('TAMPERED', 'The run\'s evidence changed after sealing; it cannot be promoted.',
                           'Re-run it and promote the new run.')
    files = artifact_files(rd, name)
    if not files:
        raise PromoteError('NO_ARTIFACT', f'Run {rd.name} has no artifact named {name}.',
                           'Check the upload-artifact name (easyci status shows the jobs).')
    for rel, (path, sha) in files.items():
        if not path.is_file() or path.is_symlink() or store.digest(path) != sha:
            raise PromoteError('ARTIFACT_CHANGED', f'{name}/{rel} no longer matches the tested file.',
                               'Re-run it and promote the new run.')
    label = version or run['commit'][:12]
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,80}', label):
        raise PromoteError('BAD_VERSION', f'{label!r} is not a usable release name.', 'Use letters, digits, . _ or -.')
    dest_root.mkdir(parents=True, exist_ok=True)
    destination = dest_root / f'{run.get("project", "project")}-{name}-{label}'
    lock = dest_root / '.easyci-promote-lock'
    try:
        lock.mkdir()
    except FileExistsError:
        raise PromoteError('PROMOTE_BUSY', 'Another promotion into this folder is running (or crashed).',
                           f'Wait, or delete {lock} if no promotion is running.') from None
    try:
        if destination.exists():
            raise PromoteError('RELEASE_EXISTS', f'{destination.name} already exists; releases are never overwritten.',
                               'Promote with a different --version.')
        with tempfile.TemporaryDirectory(prefix='.promote-', dir=dest_root) as staging:
            ready = pathlib.Path(staging) / 'ready'
            ready.mkdir()
            for rel, (path, sha) in sorted(files.items()):
                target = ready / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(path, target)
                if store.digest(target) != sha:
                    raise PromoteError('COPY_MISMATCH', f'Copy of {rel} does not match the tested file.', 'Try again.')
            receipt = {'schema': SCHEMA, 'kind': 'easyci-release', 'release': destination.name, 'artifact': name,
                       'run_id': rd.name, 'project': run.get('project'), 'commit': run.get('commit'),
                       'tree': run.get('tree'), 'base': run.get('base'), 'profile': run.get('profile'),
                       'evidence_sha256': store.digest(rd / 'sha256.json'), 'promoted_at': store.iso(),
                       'host': socket.gethostname(), 'easyci_version': VERSION,
                       'files': {rel: sha for rel, (_, sha) in sorted(files.items())}}
            store.write_json(ready / 'receipt.json', receipt)
            os.replace(ready, destination)
        for path in destination.rglob('*'):
            if path.is_file():
                os.chmod(path, stat.S_IREAD)
        return {'release': str(destination), 'files': len(files), 'receipt': receipt}
    finally:
        lock.rmdir()


def verify_release(folder):
    """[{'path', 'problem'}] for a release folder against its receipt, or None if it has no receipt."""
    folder = pathlib.Path(folder)
    receipt = store.read_json(folder / 'receipt.json')
    if not receipt or receipt.get('kind') != 'easyci-release':
        return None
    expected, problems = receipt.get('files', {}), []
    present = {p.relative_to(folder).as_posix(): p for p in folder.rglob('*') if p.is_file()}
    for rel, sha in sorted(expected.items()):
        if rel not in present:
            problems.append({'path': rel, 'problem': 'missing'})
        elif store.digest(present[rel]) != sha:
            problems.append({'path': rel, 'problem': 'changed'})
    for rel in sorted(set(present) - set(expected) - {'receipt.json'}):
        problems.append({'path': rel, 'problem': 'extra'})
    return problems
