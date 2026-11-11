"""Local workspace caches for actions/cache (spec Phase 1) and hashFiles().

A cache entry is one tar archive plus a small JSON record of its key, kept per project. Entries
are immutable, as on GitHub: saving an existing key is a no-op. Lookup tries the exact key, then
each `restore-keys` prefix, newest entry first. Each project's cache is capped (oldest removed
first); the cap is `cache_limit_gb` in the global config (default 10).

Native jobs keep their cache under %EASYCI_HOME%\\cache\\<project>; WSL jobs keep it inside the
distro under ~/.easyci/cache/<project>, so `~/.julia` or `~/.npm` never cross the /mnt boundary.
"""
import hashlib
import json
import os
import pathlib
import shlex
import shutil
import tarfile
import time

from easyci import store

DEFAULT_LIMIT_GB = 10


def entry_name(key, paths):
    """An entry is its key *and* its paths, as GitHub's cache version is: the same key with other paths
    is a different entry, never an exact hit that restores nothing."""
    return hashlib.sha256('\n'.join([key, *paths]).encode('utf-8')).hexdigest()[:32]


def split_lines(value):
    return [line.strip() for line in str(value or '').splitlines() if line.strip()]


def limit_bytes():
    """`cache_limit_gb` from %EASYCI_HOME%\\config.yml, default 10 GB per project."""
    gb = DEFAULT_LIMIT_GB
    path = store.home() / 'config.yml'
    if path.exists():
        try:
            import yaml
            gb = float((yaml.safe_load(path.read_text(encoding='utf-8')) or {}).get('cache_limit_gb', DEFAULT_LIMIT_GB))
        except (ValueError, TypeError, AttributeError, OSError, yaml.YAMLError):
            gb = DEFAULT_LIMIT_GB
    return int(gb * 2 ** 30)


def pick(entries, key, restore_keys, paths):
    """entries: [{'key', 'paths', 'created'}]. Only entries saved for the same paths count; the exact key
    wins, else the newest entry per prefix in order."""
    entries = [e for e in entries if list(e.get('paths') or []) == list(paths)]
    for entry in entries:
        if entry['key'] == key:
            return entry, True
    for prefix in restore_keys:
        matches = [e for e in entries if e['key'].startswith(prefix)]
        if matches:
            return max(matches, key=lambda e: e['created']), False
    return None, False


# ---- native -------------------------------------------------------------------------------

def restore_symlink(linkname, dest, root):
    """Recreate a cached symbolic link. Windows may refuse (no Developer Mode); then a link that points
    inside the cached folder is restored as a copy of what it points to."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    linkname = linkname.replace('/', os.sep)  # tar stores `/`; a Windows link with `/` does not resolve
    pointed = (dest.parent / linkname).resolve()
    if dest.is_symlink():
        if os.readlink(dest) == linkname:
            return  # restoring over the same link (a persistent cache folder, a repeated restore)
        dest.unlink()
    elif dest.is_dir():
        shutil.rmtree(dest)
    elif dest.exists():
        dest.unlink()
    try:
        os.symlink(linkname, dest, target_is_directory=pointed.is_dir())
        return
    except OSError:
        pass
    try:
        pointed.relative_to(pathlib.Path(root).resolve())
    except ValueError:
        return  # outside the cached folder: nothing safe to copy
    if pointed.is_dir():
        shutil.copytree(pointed, dest, dirs_exist_ok=True)
    elif pointed.is_file():
        shutil.copyfile(pointed, dest)


class NativeCache:
    def __init__(self, project):
        self.dir = store.home() / 'cache' / project

    def entries(self):
        out = []
        for meta in self.dir.glob('*.json') if self.dir.exists() else []:
            record = store.read_json(meta) or {}
            if record.get('key') and (meta.with_suffix('.tar')).exists():
                out.append({**record, 'name': meta.stem})
        return out

    def resolve(self, ws, path):
        path = os.path.expanduser(path)
        return pathlib.Path(path) if os.path.isabs(path) else pathlib.Path(ws) / path

    def restore(self, ws, paths, key, restore_keys):
        entry, exact = pick(self.entries(), key, restore_keys, paths)
        if not entry:
            return None, False
        archive = self.dir / f'{entry["name"]}.tar'
        with tarfile.open(archive) as tar:
            for i, path in enumerate(entry['paths']):
                if path not in paths:
                    continue
                target = self.resolve(ws, path)
                prefix = f'p{i}'
                members = [m for m in tar.getmembers() if m.name == prefix or m.name.startswith(prefix + '/')]
                links = []
                for member in members:
                    rel = member.name[len(prefix):].lstrip('/')
                    dest = target / rel if rel else target
                    if member.isdir():
                        dest.mkdir(parents=True, exist_ok=True)
                    elif member.isfile():
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with tar.extractfile(member) as src, open(dest, 'wb') as out:
                            out.write(src.read())
                    elif member.islnk() and (member.linkname == prefix or member.linkname.startswith(prefix + '/')):
                        # a hard link to a file stored earlier in this path: restore it as a copy
                        source = target / member.linkname[len(prefix):].lstrip('/')
                        if source.is_file():
                            if dest.exists() and os.path.samefile(source, dest):
                                continue  # restoring over the same hard link: already in place
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            if dest.is_symlink() or dest.is_file():
                                dest.unlink()
                            shutil.copyfile(source, dest)
                    elif member.issym():
                        links.append((member, dest))
                for member, dest in links:  # after the files and folders they may point to
                    restore_symlink(member.linkname, dest, target)
        os.utime(archive)  # recently used entries survive pruning
        return entry['key'], exact

    def save(self, ws, paths, key):
        name = entry_name(key, paths)
        if (self.dir / f'{name}.json').exists():
            return False
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f'{name}.tar.partial'
        stored = False
        with tarfile.open(tmp, 'w') as tar:
            for i, path in enumerate(paths):
                source = self.resolve(ws, path)
                if source.exists() and not source.is_symlink():
                    tar.add(source, arcname=f'p{i}')
                    stored = True
        if not stored:
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, self.dir / f'{name}.tar')
        store.write_json(self.dir / f'{name}.json', {'key': key, 'paths': list(paths), 'created': time.time()})
        self.prune()
        return True

    def prune(self):
        archives = sorted(self.dir.glob('*.tar'), key=lambda p: p.stat().st_mtime)
        total = sum(p.stat().st_size for p in archives)
        while archives and total > limit_bytes():
            oldest = archives.pop(0)
            total -= oldest.stat().st_size
            oldest.unlink(missing_ok=True)
            oldest.with_suffix('.json').unlink(missing_ok=True)


# ---- WSL ----------------------------------------------------------------------------------

class WslCache:
    """The same store inside a WSL distro, driven with tar and a tiny JSON index per entry."""

    def __init__(self, backend, project):
        self.backend, self.project = backend, project

    @property
    def dir(self):
        return f'{self.backend.home()}/.easyci/cache/{self.project}'

    def run(self, script, timeout=1800):
        from easyci.backends.wsl import wsl_run
        return wsl_run(self.backend.distro, script, timeout=timeout)

    def resolve(self, ws, path):
        if path.startswith('~/') or path == '~':
            return self.backend.home() + path[1:]
        return path if path.startswith('/') else f'{ws}/{path}'

    def entries(self):
        q = shlex.quote
        code, text = self.run(f'cd {q(self.dir)} 2>/dev/null || exit 0; for f in *.json; do [ -f "$f" ] && '
                              f'[ -f "${{f%.json}}.tar" ] && {{ printf "%s\\t" "${{f%.json}}"; cat "$f"; echo; }}; done',
                              timeout=60)
        out = []
        for line in text.splitlines() if code == 0 else []:
            name, _, raw = line.partition('\t')
            try:
                record = json.loads(raw)
            except ValueError:
                continue
            if record.get('key'):
                out.append({**record, 'name': name})
        return out

    def restore(self, ws, paths, key, restore_keys):
        entry, exact = pick(self.entries(), key, restore_keys, paths)
        if not entry:
            return None, False
        q = shlex.quote
        lines = ['set -e', f'tmp=$(mktemp -d)', f'tar -xf {q(self.dir)}/{entry["name"]}.tar -C "$tmp"']
        for i, path in enumerate(entry['paths']):
            if path in paths:
                target = self.resolve(ws, path)
                # A directory is merged into the target; a single file only needs its parent folder.
                lines.append(f'if [ -d "$tmp/p{i}" ]; then mkdir -p {q(target)}; cp -a "$tmp/p{i}/." {q(target)}/; '
                             f'elif [ -e "$tmp/p{i}" ]; then mkdir -p "$(dirname {q(target)})"; '
                             f'cp -a "$tmp/p{i}" {q(target)}; fi')
        lines += ['rm -rf "$tmp"', f'touch {q(self.dir)}/{entry["name"]}.tar']
        code, text = self.run('\n'.join(lines))
        if code != 0:
            raise RuntimeError(f'cache restore failed: {text[-300:]}')
        return entry['key'], exact

    def save(self, ws, paths, key):
        q, name = shlex.quote, entry_name(key, paths)
        record = json.dumps({'key': key, 'paths': list(paths), 'created': time.time()})
        links = ' '.join(f'[ -e {q(self.resolve(ws, p))} ] && ln -s {q(self.resolve(ws, p))} "$stage/p{i}";'
                         for i, p in enumerate(paths))
        script = (f'set -e; d={q(self.dir)}; mkdir -p "$d"; [ -f "$d/{name}.json" ] && exit 3; '
                  f'stage=$(mktemp -d); {links} '
                  f'if [ -z "$(ls -A "$stage")" ]; then rm -rf "$stage"; exit 4; fi; '
                  f'tar -chf "$d/{name}.tar.partial" -C "$stage" .; rm -rf "$stage"; '
                  f'mv "$d/{name}.tar.partial" "$d/{name}.tar"; printf %s {q(record)} > "$d/{name}.json"; '
                  # prune oldest archives beyond the limit
                  f'limit={limit_bytes()}; total=$(du -cb "$d"/*.tar | tail -1 | cut -f1); '
                  f'for f in $(ls -tr "$d"/*.tar); do [ "$total" -le "$limit" ] && break; '
                  f'size=$(stat -c %s "$f"); rm -f "$f" "${{f%.tar}}.json"; total=$((total - size)); done')
        code, text = self.run(script)
        if code in (3, 4):
            return False
        if code != 0:
            raise RuntimeError(f'cache save failed: {text[-300:]}')
        return True
