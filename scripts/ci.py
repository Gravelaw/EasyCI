"""Trusted local snapshot ingestion and two small adapters. Python stdlib only.

This is administrative infrastructure. Checksums establish byte identity, not trust.
Run reviewed source only; a snapshot may execute arbitrary code on its assigned agent.
"""
import argparse
import hashlib
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = pathlib.Path(__file__).resolve().parents[1]
ID = re.compile(r'[a-z][a-z0-9-]{0,79}\Z')
SHA = re.compile(r'[0-9a-f]{40}\Z')
HASH = re.compile(r'[0-9a-f]{64}\Z')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identifier(value):
    require(isinstance(value, str) and bool(ID.fullmatch(value)), 'Invalid identifier')
    return value


def sha(value):
    require(isinstance(value, str) and bool(SHA.fullmatch(value)), 'Full lowercase commit SHA required')
    return value


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def write_json(path, data):
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + '\n', encoding='utf-8')


def digest(path):
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def confined(root, relative):
    root = pathlib.Path(root).resolve(strict=True)
    require(isinstance(relative, str) and relative and '\\' not in relative,
            'Invalid relative path')
    parts = pathlib.PurePosixPath(relative)
    require(not parts.is_absolute() and ':' not in relative and all(p not in ('..', '.') for p in relative.split('/')),
            'Invalid relative path')
    current = root
    for part in parts.parts:
        current = current / part
        require(not current.is_symlink() and not getattr(current, 'is_junction', lambda: False)(), 'Symlink/junction rejected')
    resolved = current.resolve(strict=True)
    require(resolved.is_relative_to(root), 'Path escapes root')
    return resolved


def command(args, cwd, timeout=120, capture=True):
    # No shell interpretation or user-selected executable/command strings.
    result = subprocess.run(args, cwd=cwd, check=False, timeout=timeout,
                            text=True, stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None)
    require(result.returncode == 0, f'{pathlib.Path(args[0]).name} failed ({result.returncode})')
    return result.stdout.strip() if capture else ''


def git(args, cwd):
    # Ignore global/system git config, hooks, credentials and protocol overrides.
    return command(['git', '-c', 'core.hooksPath=', '-c', 'credential.helper=', *args], cwd)


def config(project):
    p = read_json(ROOT / 'projects' / f'{identifier(project)}.json')
    required = {'schemaVersion', 'id', 'adapter', 'platform', 'agentLabel', 'heavyLock',
                'profile', 'cache', 'timeoutMinutes', 'reviewer', 'qualification'}
    require(set(p) == required and p['schemaVersion'] == 1 and p['id'] == project, 'Invalid project contract')
    for key in ('id', 'agentLabel', 'reviewer'):
        require(len(identifier(p[key])) <= 40, 'Project identifier too long')
    require(p['heavyLock'] == 'local-heavy' and p['cache'] in ('cold', 'warm'), 'Invalid execution policy')
    require(p['adapter'] != 'juliaflow-wsl' or p['cache'] == 'cold', 'WSL adapter requires run-isolated cold caches')
    require(type(p['timeoutMinutes']) is int and 1 <= p['timeoutMinutes'] <= 800, 'Invalid timeout')
    require(isinstance(p['qualification'], str) and len(p['qualification']) <= 160, 'Invalid qualification')
    require((p['adapter'] == 'demo' and p['platform'] == 'linux-container' and p['profile'] == 'demo') or
            (p['adapter'] == 'juliaflow-wsl' and p['platform'] == 'windows-wsl' and p['profile'] in ('pr', 'push', 'release-candidate')),
            'Unsupported adapter/platform/profile combination')
    return p


def ingest(repo, project, commit, base, snapshots, snapshot_id):
    config(project)
    sha(commit); sha(base); identifier(snapshot_id)
    repo = pathlib.Path(repo).resolve(strict=True)
    snapshots = pathlib.Path(snapshots).resolve(strict=True)
    require(not snapshots.is_relative_to(repo), 'Snapshots must be outside the source repository')
    require(git(['rev-parse', '--verify', f'{commit}^{{commit}}'], repo) == commit, 'Commit mismatch')
    require(git(['rev-parse', '--verify', f'{base}^{{commit}}'], repo) == base, 'Base mismatch')
    target = snapshots / snapshot_id
    require(not target.exists(), 'Snapshot already exists')
    # Write an atomic owned snapshot; never change source refs or export dirty files.
    with tempfile.TemporaryDirectory(prefix='.ingest-', dir=snapshots) as staging:
        stage = pathlib.Path(staging)
        bare = stage / 'source.git'
        git(['init', '--bare', str(bare)], stage)
        git(['fetch', '--no-tags', str(repo), f'{commit}:refs/heads/candidate', f'{base}:refs/heads/baseline'], bare)
        bundle = stage / 'source.bundle'
        git(['bundle', 'create', str(bundle), 'refs/heads/candidate', 'refs/heads/baseline'], bare)
        tree = git(['rev-parse', f'{commit}^{{tree}}'], bare)
        # TemporaryDirectory cleans its own bare repository, including Git's
        # read-only object files on Windows; do not delete source repository files.
        write_json(stage / 'snapshot.json', {'schemaVersion': 1, 'project': project, 'id': snapshot_id,
                   'commit': commit, 'base': base, 'tree': tree, 'bundleSha256': digest(bundle)})
        # Rename a separate subdirectory so TemporaryDirectory retains its own root.
        ready = stage / 'ready'; ready.mkdir()
        bundle.rename(ready / 'source.bundle'); (stage / 'snapshot.json').rename(ready / 'snapshot.json')
        ready.rename(target)
    return target


def load_snapshot(snapshots, snapshot_id, project, expected_hash):
    identifier(snapshot_id); config(project)
    require(bool(HASH.fullmatch(expected_hash)), 'Snapshot metadata SHA-256 required')
    folder = confined(snapshots, snapshot_id)
    metadata_path = confined(folder, 'snapshot.json')
    require(digest(metadata_path) == expected_hash, 'Snapshot metadata checksum mismatch')
    m = read_json(metadata_path)
    require(set(m) == {'schemaVersion', 'project', 'id', 'commit', 'base', 'tree', 'bundleSha256'} and
            m['schemaVersion'] == 1 and m['project'] == project and m['id'] == snapshot_id, 'Snapshot identity mismatch')
    for key in ('commit', 'base', 'tree'):
        sha(m[key])
    require(bool(HASH.fullmatch(m['bundleSha256'])), 'Invalid bundle hash')
    require(digest(confined(folder, 'source.bundle')) == m['bundleSha256'], 'Source bundle checksum mismatch')
    return folder, m


def prepare(snapshots, snapshot_id, project, expected_hash, work):
    folder, m = load_snapshot(snapshots, snapshot_id, project, expected_hash)
    work = pathlib.Path(work).absolute()
    require(not work.exists(), 'Run workspace already exists; resume/rebuild requires review')
    work.mkdir(parents=True)
    write_json(work / 'owner.json', {'schemaVersion': 1, 'project': project, 'snapshot': m,
                                   'metadataSha256': expected_hash})
    source = work / 'source'
    git(['clone', '--no-hardlinks', '--branch', 'candidate', str(folder / 'source.bundle'), str(source)], work)
    git(['checkout', '--detach', m['commit']], source)
    require(git(['rev-parse', 'HEAD'], source) == m['commit'] and
            git(['rev-parse', 'HEAD^{tree}'], source) == m['tree'], 'Checkout identity mismatch')
    require(git(['rev-parse', f'{m["base"]}^{{commit}}'], source) == m['base'], 'Baseline absent')
    # No tracked symlinks or gitlinks: keep extraction/runtime paths inside the snapshot.
    entries = git(['ls-tree', '-r', 'HEAD'], source).splitlines()
    require(not any(line.startswith(('120000 ', '160000 ')) for line in entries), 'Symlinks and submodules are unsupported')
    return work


def owner(work, project):
    raw = pathlib.Path(work).absolute()
    require(not raw.is_symlink() and not getattr(raw, 'is_junction', lambda: False)(), 'Unsafe workspace')
    work = raw.resolve(strict=True)
    o = read_json(confined(work, 'owner.json'))
    require(o['project'] == project and o['snapshot']['project'] == project, 'Workspace owner mismatch')
    source = confined(work, 'source')
    require(git(['rev-parse', 'HEAD'], source) == o['snapshot']['commit'], 'Source candidate drift')
    require(not git(['status', '--porcelain', '--untracked-files=no'], source), 'Tracked source drift')
    return work, o, source


def juliaflow_command(p, m):
    return ['node', 'scripts/ci/wsl.mjs', '--ref', m['commit'], '--base', m['base'],
            '--profile', p['profile'], '--cache', p['cache']]


def verify_inventory(folder):
    hashes = read_json(confined(folder, 'sha256.json'))
    require(isinstance(hashes, dict) and hashes, 'Missing evidence inventory')
    for path, expected in hashes.items():
        require(isinstance(expected, str) and bool(HASH.fullmatch(expected)), 'Invalid evidence hash')
        file = confined(folder, path)
        require(file.is_file() and digest(file) == expected, f'Evidence checksum mismatch: {path}')
    return hashes


def run(work, project):
    p = config(project); work, o, source = owner(work, project)
    require(not (work / 'result.json').exists(), 'Run result already exists')
    m = o['snapshot']
    if p['adapter'] == 'demo':
        message = read_json(confined(source, 'message.json'))
        require(set(message) == {'message', 'version'} and isinstance(message['message'], str) and
                bool(re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+', message['version'])), 'Invalid demo input')
        artifact = work / 'demo-artifact.json'
        payload = {'message': message['message'].upper(), 'version': message['version'], 'commit': m['commit']}
        write_json(artifact, payload)  # Build exactly once.
        # Test the serialized artifact itself, then preserve its hash for promotion.
        actual = read_json(artifact)
        require(actual == payload and actual['message'] == message['message'].upper(), 'Demo artifact test failed')
        result = {'schemaVersion': 1, 'project': project, 'commit': m['commit'], 'status': 'passed',
                  'artifact': artifact.name, 'artifactSha256': digest(artifact), 'promotable': True,
                  'kind': 'demo-artifact', 'version': message['version']}
    else:
        require(sys.platform == 'win32', 'JuliaFlow WSL adapter requires a native Windows agent')
        command(juliaflow_command(p, m), source, timeout=p['timeoutMinutes'] * 60, capture=False)
        runs = confined(source, 'artifacts/local-ci')
        matches = [d for d in runs.iterdir() if d.name.startswith('jfci-') and d.is_dir()]
        require(len(matches) == 1, 'Expected exactly one isolated runner evidence directory')
        evidence = confined(runs, matches[0].name)
        report = read_json(confined(evidence, 'summary.json'))
        require(report['head'] == m['commit'] and report['tree'] == m['tree'] and report['base'] == m['base'] and
                report['status'] == 'passed' and report['completeProfile'] is True and
                report['cleanupStatus'] == 'passed' and report['evidenceFinalization'] == 'passed', 'Runner result gate failed')
        hashes = verify_inventory(evidence)
        require('summary.json' in hashes, 'Summary not covered by checksum inventory')
        shutil.copyfile(evidence / 'summary.json', work / 'runner-summary.json')
        result = {'schemaVersion': 1, 'project': project, 'commit': m['commit'], 'status': 'passed',
                  'runId': evidence.name, 'promotable': p['profile'] == 'release-candidate',
                  'evidenceManifestSha256': digest(evidence / 'sha256.json')}
        if result['promotable']:
            shutil.copyfile(confined(evidence, 'release-candidate/release-verification/latest/summary.json'), work / 'release-summary.json')
            metadata = read_json(confined(evidence, 'release-candidate/delivery/package.json'))
            write_json(work / 'package-metadata.json', metadata)
            relative = 'release-candidate/delivery/' + metadata['name']
            require(relative in hashes and metadata['head'] == m['commit'] and metadata['tree'] == m['tree'] and
                    digest(confined(evidence, relative)) == metadata['sha256'], 'Package identity gate failed')
            result.update({'artifact': relative, 'artifactSha256': metadata['sha256'], 'version': metadata['version'],
                           'kind': 'source-and-built-web'})
    write_json(work / 'result.json', result)
    # Review this digest; changes to the result during approval cannot redirect promotion.
    (work / 'result.sha256').write_text(digest(work / 'result.json') + '\n', encoding='ascii')
    return result


def promote(work, project, releases, reviewed_result_hash, reviewer):
    p = config(project); work, o, source = owner(work, project)
    require(reviewer == p['reviewer'], 'Approved reviewer identity required')
    require(bool(HASH.fullmatch(reviewed_result_hash)) and digest(confined(work, 'result.json')) == reviewed_result_hash,
            'Reviewed result changed')
    result = read_json(work / 'result.json')
    require(result['project'] == project and result['commit'] == o['snapshot']['commit'] and
            result['status'] == 'passed' and result['promotable'] is True, 'Promotion requires complete validated artifact')
    releases = pathlib.Path(releases).resolve(strict=True)
    lock = releases / '.promotion-lock'
    lock.mkdir()  # Cross-process fail-closed lock, including non-Jenkins invocations.
    try:
        if p['adapter'] == 'demo':
            artifact = confined(work, result['artifact'])
            require(digest(artifact) == result['artifactSha256'], 'Artifact changed after testing')
            destination = releases / identifier(f'{project}-{result["version"].replace(".", "-")}-{result["commit"][:12]}')
            require(not destination.exists(), 'Release already exists; no overwrite')
            with tempfile.TemporaryDirectory(prefix='.promote-', dir=releases) as staging:
                ready = pathlib.Path(staging) / 'ready'; ready.mkdir()
                shutil.copyfile(artifact, ready / artifact.name)
                require(digest(ready / artifact.name) == result['artifactSha256'], 'Copied artifact mismatch')
                write_json(ready / 'receipt.json', {**result, 'reviewer': reviewer, 'reviewedResultSha256': reviewed_result_hash,
                           'localOnly': True, 'certifiesRelease': False})
                ready.rename(destination)
        else:
            evidence = confined(source, f'artifacts/local-ci/{result["runId"]}')
            require(digest(confined(evidence, 'sha256.json')) == result['evidenceManifestSha256'], 'Evidence inventory changed')
            verify_inventory(evidence)
            require(digest(confined(evidence, result['artifact'])) == result['artifactSha256'], 'Tested package changed')
            package_name = pathlib.PurePosixPath(result['artifact']).name
            require(bool(re.fullmatch(r'juliaflow-[a-zA-Z0-9.-]+\.tar\.gz', package_name)), 'Invalid package name')
            require(not (releases / package_name.removesuffix('.tar.gz')).exists(), 'Durable release already exists')
            # Keep all original 20-phase, cleanup and checksum gates in the repository command.
            command(['node', 'scripts/ci/delivery.mjs', '--run', result['runId']], source, capture=False)
            pointer = read_json(confined(source, 'artifacts/local-releases/current.json'))
            # Existing runner receipts are copied unchanged into durable project storage.
            built = confined(source, 'artifacts/local-releases')
            dirs = [d for d in built.iterdir() if d.is_dir() and not d.name.startswith('.')]
            require(len(dirs) == 1, 'Ambiguous promotion output')
            release = confined(built, dirs[0].name)
            receipt = read_json(confined(release, 'receipt.json'))
            require(receipt['sha256'] == result['artifactSha256'] and receipt['head'] == result['commit'], 'Promoted identity mismatch')
            verify_inventory(release)
            require(all(not f.is_symlink() and not getattr(f, 'is_junction', lambda: False)() for f in release.rglob('*')),
                    'Links in promoted release are unsupported')
            destination = releases / release.name
            require(not destination.exists(), 'Durable release already exists')
            with tempfile.TemporaryDirectory(prefix='.promote-', dir=releases) as staging:
                ready = pathlib.Path(staging) / 'ready'
                shutil.copytree(release, ready, symlinks=False)
                require(digest(confined(ready, receipt['name'])) == result['artifactSha256'], 'Durable package checksum mismatch')
                write_json(ready / 'jenkins-review.json', {'reviewer': reviewer, 'reviewedResultSha256': reviewed_result_hash,
                           'artifactSha256': result['artifactSha256'], 'localOnly': True, 'runnerPointer': pointer})
                ready.rename(destination)
        write_json(work / 'promotion.json', {'destination': destination.name, 'artifactSha256': result['artifactSha256'],
                   'reviewer': reviewer, 'localOnly': True})
        return destination
    finally:
        lock.rmdir()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='operation', required=True)
    ingest_p = sub.add_parser('ingest')
    for flag in ('repo', 'project', 'commit', 'base', 'snapshots', 'snapshot-id'):
        ingest_p.add_argument('--' + flag, required=True)
    prep = sub.add_parser('prepare')
    for flag in ('snapshots', 'snapshot-id', 'project', 'metadata-sha256', 'work'):
        prep.add_argument('--' + flag, required=True)
    run_p = sub.add_parser('run')
    run_p.add_argument('--work', required=True); run_p.add_argument('--project', required=True)
    prom = sub.add_parser('promote')
    for flag in ('work', 'project', 'releases', 'reviewed-result-sha256'):
        prom.add_argument('--' + flag, required=True)
    args = parser.parse_args()
    if args.operation == 'ingest':
        output = ingest(args.repo, args.project, args.commit, args.base, args.snapshots, args.snapshot_id)
        print(json.dumps({'snapshot': args.snapshot_id, 'metadataSha256': digest(output / 'snapshot.json')}))
    elif args.operation == 'prepare':
        prepare(args.snapshots, args.snapshot_id, args.project, args.metadata_sha256, args.work)
        print('Commit-pinned snapshot prepared')
    elif args.operation == 'run':
        print(json.dumps(run(args.work, args.project)))
    else:
        print(promote(args.work, args.project, args.releases, args.reviewed_result_sha256, os.environ.get('CI_REVIEWER', '')))


if __name__ == '__main__':
    # Candidate code is trusted, but inherited git options/credential helpers are not required.
    os.environ['GIT_CONFIG_NOSYSTEM'] = '1'
    os.environ['GIT_CONFIG_GLOBAL'] = os.devnull
    os.environ['GIT_TERMINAL_PROMPT'] = '0'
    try:
        main()
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print(f'Local CI rejected: {error}', file=sys.stderr)
        sys.exit(1)
